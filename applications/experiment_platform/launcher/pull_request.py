"""완주한 실험의 exp 브랜치를 dev로 향하는 PR로 올리는 경계(#689).

[파이프라인] 실험이 `PASSED`가 된 뒤부터 Pull Request가 열리기까지의 구간을 담당한다.
executor가 커밋·push하고 `report.md`까지 쓴 뒤 비어 있던 자리다. 머지와 `PROMOTED`
전이는 사람이 한다.

[기능] 어떤 실험이 대상인지 판정하고, 제목·본문을 조립하며, 같은 실험에 PR이 두 번
생기지 않게 하는 멱등 규칙을 제공한다.

[비책임] 실험 실행과 상태 전이(`app.experiments.service`), 리포트 생성(executor의
Codex #2), App 권한 부여와 배포(`SKYAHO/Autoresearch-infra`)는 담당하지 않는다.

**launcher의 책임이 아니지만 이 패키지에 둔다.** launcher 이미지가 `app` 패키지와 DB
세션을 이미 포함해 진입점만 다른 프로세스로 띄울 수 있고, `log_collector`(#559)가
같은 이유로 여기에 있다. 이 패키지는 "launcher 이미지에 실리는 것들"이다.

정본 계약: `docs/specs/2026-08-11-passed-experiment-pull-request.md`
"""

from __future__ import annotations

import enum
import logging
import uuid
from dataclasses import dataclass
from typing import Final, Protocol

from applications.experiment_platform.shared.github_app import GitHubAppError
from applications.experiment_platform.shared.github_pull_requests import (
    GitHubPullRequestError,
    GitHubPullRequests,
)


_LOGGER = logging.getLogger(__name__)


# 멱등 판정이 이 키에 걸려 있다. 바꾸면 이미 PR을 만든 실험을 다시 만든다.
# `experiment_metadata.key`가 max_length=64다.
PULL_REQUEST_METADATA_KEY: Final = "pull_request_number"

# 실험 브랜치가 향하는 곳. `main` 반영은 이 계약 밖이다(#509).
BASE_BRANCH: Final = "dev"

# GitHub PR 제목 상한. 넘기면 생성이 422로 거부된다.
_TITLE_LIMIT: Final = 256
_TITLE_PREFIX: Final = "[AR] "

# GitHub PR body 상한이다. `ExperimentLogCreate`가 아니라 GitHub 쪽 제약이며,
# `report_markdown`은 262,144자까지 허용되므로(`schemas.MAX_REPORT_MARKDOWN_CHARS`)
# 실제로 넘을 수 있다. 넘긴 채 보내면 422가 나고 그 사유는 재시도로 낫지 않는다.
_BODY_LIMIT: Final = 65_536
_TRUNCATION_NOTICE: Final = (
    "\n\n> **리포트가 길어 본문이 잘렸습니다.** 전문은 실험 산출물의 "
    "`report.md`에 있습니다."
)


class SkipReason(enum.Enum):
    """PR을 만들지 않고 넘어가는 **정상** 상황. 오류가 아니다."""

    ALREADY_RECORDED = "already_recorded"
    REPORT_MISSING = "report_missing"
    BRANCH_MISSING = "branch_missing"
    NO_CHANGES = "no_changes"
    # 사람이 닫은 PR이 있어 다시 열지 않는다. `already_recorded`와 가른 이유는
    # 운영자가 "왜 굳었는가"를 로그·DB만 보고 알 수 있어야 하기 때문이다.
    CLOSED_BY_HUMAN = "closed_by_human"
    # 본문을 잘라 보냈는데도 GitHub 상한을 넘었다. 재시도로 낫지 않는다.
    BODY_TOO_LONG = "body_too_long"

    @property
    def permanent(self) -> bool:
        """다시 볼 필요가 없는가.

        영구 skip은 기록으로 남겨 재시도 대상에서 뺀다. 그러지 않으면 그 실험이 매
        주기 돌아와 아무 일도 하지 않고 조회만 반복한다.

        리포트와 브랜치는 나중에 도착할 수 있으므로 굳히지 않는다.
        """
        return self in {
            SkipReason.ALREADY_RECORDED,
            SkipReason.NO_CHANGES,
            SkipReason.CLOSED_BY_HUMAN,
            SkipReason.BODY_TOO_LONG,
        }


class _Experiment(Protocol):
    """`pull_request_plan`이 읽는 최소 Experiment 속성."""

    issue_branch: str | None
    issue_number: int | None
    issue_title: str | None
    candidate_sha: str | None
    report_markdown: str | None


@dataclass(frozen=True)
class PullRequestPlan:
    """한 실험에 대해 무엇을 할지 정한 결과."""

    head: str | None
    base: str
    skip_reason: SkipReason | None


def pull_request_plan(
    experiment: _Experiment,
    *,
    recorded_number: int | None,
) -> PullRequestPlan:
    """실험 하나가 PR 대상인지 판정한다.

    **지표로 거르지 않는다.** `PASSED`는 "가설이 맞았다"가 아니라 "완주했다"이고
    (정본 계약 §결정 1·6), 여기서 결과로 걸러내면 승격 관문에서 제거한 통계 게이트를
    다른 이름으로 되살리는 것이 된다. 성패는 `report.md`가 서술하고 머지는 사람이
    정한다.

    `recorded_number`는 `experiment_metadata`에 남은 PR 번호다. GitHub에 "열린 PR이
    있는지" 묻지 않는다 — 사람이 PR을 닫으면 다시 만들게 되고, 그건 닫은 의도를
    되돌리는 것이다.
    """
    if recorded_number is not None:
        return PullRequestPlan(None, BASE_BRANCH, SkipReason.ALREADY_RECORDED)
    if not experiment.candidate_sha:
        # 커밋이 없었다. 올릴 변경 자체가 없으므로 다시 보지 않는다.
        return PullRequestPlan(None, BASE_BRANCH, SkipReason.NO_CHANGES)
    if not experiment.issue_branch:
        return PullRequestPlan(None, BASE_BRANCH, SkipReason.BRANCH_MISSING)
    if not experiment.report_markdown:
        # 본문 없는 PR보다 기다리는 편이 낫다.
        return PullRequestPlan(None, BASE_BRANCH, SkipReason.REPORT_MISSING)
    return PullRequestPlan(experiment.issue_branch, BASE_BRANCH, None)


def build_pull_request_title(experiment: _Experiment) -> str:
    """PR 목록에서 어느 실험인지 바로 보이는 제목을 만든다.

    이슈 번호를 앞에 두는 이유는 제목이 길어 잘려도 번호는 남기 위해서다.
    """
    number = experiment.issue_number
    hypothesis = (experiment.issue_title or "").strip()
    head = f"{_TITLE_PREFIX}#{number} " if number is not None else _TITLE_PREFIX
    title = f"{head}{hypothesis}".rstrip()
    if len(title) <= _TITLE_LIMIT:
        return title
    # 말줄임표 한 글자까지 상한 안에 들어가야 한다.
    return title[: _TITLE_LIMIT - 1] + "…"


def build_pull_request_body(experiment: _Experiment) -> str:
    """에이전트가 쓴 리포트를 그대로 본문으로 쓴다.

    요약하거나 재서술하지 않는다 — 판정을 한 것은 `report.md`이고, 여기서 다시 쓰면
    사람이 읽는 근거와 실제 산출물이 갈린다.

    **이슈를 닫는 키워드를 넣지 않는다.** PR이 머지돼도 실험 종료 여부는 사람이
    정한다.
    """
    report = (experiment.report_markdown or "").strip()
    lines: list[str] = ["", "---", ""]
    if experiment.issue_number is not None:
        lines.append(f"실험 이슈: #{experiment.issue_number}")
    if experiment.candidate_sha:
        lines.append(f"candidate: `{experiment.candidate_sha}`")
    lines.append(
        "이 PR은 실험이 **완주**해 자동으로 열렸습니다. "
        "가설의 성패는 위 리포트가 서술하며, 머지 여부는 사람이 정합니다."
    )
    footer = "\n".join(lines)
    # 꼬리(이슈 링크·안내)는 항상 남긴다 — 잘리는 것은 리포트 쪽이다.
    room = _BODY_LIMIT - len(footer)
    if len(report) <= room:
        return report + footer
    return report[: room - len(_TRUNCATION_NOTICE)] + _TRUNCATION_NOTICE + footer


@dataclass(frozen=True)
class PullRequestState:
    """한 실험에 대해 이미 남아 있는 기록."""

    number: int | None
    skipped: bool


class ExperimentStore(Protocol):
    """완주한 실험을 읽고 처리 결과를 남기는 연산."""

    def list_passed_experiments(self) -> list: ...

    def pull_request_state(self, experiment_id: uuid.UUID) -> PullRequestState: ...

    def record_number(self, experiment_id: uuid.UUID, number: int) -> None: ...

    def record_skip(self, experiment_id: uuid.UUID, reason: str) -> None: ...


class PullRequestOpener(Protocol):
    """PR을 열고 찾는 연산. 구현은 `GitHubPullRequests`를 감싼 얇은 어댑터다."""

    def create(self, *, head: str, base: str, title: str, body: str) -> int: ...

    def find_open(self, *, head: str) -> int | None: ...


def open_pull_requests_once(
    store: ExperimentStore,
    opener: PullRequestOpener,
) -> list[str]:
    """완주한 실험 한 바퀴를 돌며 PR을 열고 사유 코드를 모아 돌려준다.

    **fail-open이다.** 한 실험이 실패해도 나머지를 계속 처리한다 — 실험 단위로
    격리하지 않으면 A가 계속 실패하는 동안 그 뒤의 B·C가 영영 PR을 얻지 못한다.
    """
    problems: list[str] = []
    for experiment in store.list_passed_experiments():
        try:
            state = store.pull_request_state(experiment.id)
            if state.skipped:
                # 영구 skip으로 굳힌 실험이다. 다시 판정하지 않는다.
                continue
            plan = pull_request_plan(experiment, recorded_number=state.number)
            if plan.skip_reason is SkipReason.ALREADY_RECORDED:
                # 이미 번호가 있다. 다시 기록할 것이 없다.
                continue
            if plan.skip_reason is not None:
                if plan.skip_reason.permanent:
                    store.record_skip(experiment.id, plan.skip_reason.value)
                continue
            number = _open(store, opener, experiment, plan.head or "", problems)
            if number is None:
                continue
            try:
                store.record_number(experiment.id, number)
            except Exception:
                # PR은 만들어졌는데 기록만 실패했다. 다음 주기에 `exists`로 회복되므로
                # 중복은 생기지 않지만, 사유가 뭉개지면 그 회복이 도는 중인지 모른다.
                _LOGGER.warning(
                    "pull request record failed reason=pull_request_record_failed "
                    "experiment=%s number=%s",
                    experiment.id,
                    number,
                    exc_info=True,
                )
                problems.append("pull_request_record_failed")
                continue
        except GitHubAppError as error:
            # token 발급 실패는 REST 오류가 아니라 `_open`의 except를 통과한다.
            # 키 마운트 경로가 틀린 초기 롤아웃에서 가장 먼저 걸리는 경로다.
            _LOGGER.warning(
                "pull request token failed reason=pull_request_token_failed "
                "experiment=%s cause=%s",
                getattr(experiment, "id", None),
                error.reason,
            )
            problems.append("pull_request_token_failed")
            continue
        except Exception:
            # 실험 단위로 격리한다. 여기서 새어 나가면 이 실험 하나가 아니라 뒤의
            # 실험 전부가 그 주기에서 날아간다. 예외 타입을 함께 남긴다 — 없으면
            # "키 마운트가 틀렸다"와 "DB가 끊겼다"를 로그만 보고 구분할 수 없다.
            _LOGGER.warning(
                "pull request open failed reason=experiment_promotion_failed "
                "experiment=%s",
                getattr(experiment, "id", None),
                exc_info=True,
            )
            problems.append("experiment_promotion_failed")
            continue
    return problems


def _open(
    store: ExperimentStore,
    opener: PullRequestOpener,
    experiment: _Experiment,
    head: str,
    problems: list[str],
) -> int | None:
    """PR을 열고 번호를 돌려준다. 만들지 않기로 한 경우 `None`이다."""
    try:
        return opener.create(
            head=head,
            base=BASE_BRANCH,
            title=build_pull_request_title(experiment),
            body=build_pull_request_body(experiment),
        )
    except GitHubPullRequestError as error:
        if error.reason == "pull_request_exists":
            # 먼저 만들고 기록 전에 죽은 흔적이다. 중복을 만들지 않고 번호만 채운다.
            found = opener.find_open(head=head)
            if found is not None:
                return found
            # 열린 PR이 없는데 GitHub이 중복이라고 한다 — 같은 head→base에 **닫힌**
            # PR이 있다는 뜻이다(조회가 `base`까지 맞추므로 다른 base의 PR은 아니다).
            # 다시 열지 않는다(닫은 의도를 되돌리지 않는다, 정본 계약 §결정 3).
            #
            # `already_recorded`와 다른 사유로 남긴다 — 뭉치면 운영자가 왜 굳었는지
            # 알 수 없다. 되돌리려면 이 metadata 행을 지운다(정본 계약 §운영).
            store.record_skip(experiment.id, SkipReason.CLOSED_BY_HUMAN.value)
            return None
        if error.reason == "pull_request_no_commits":
            # 올릴 변경이 없다. 조회해도 소용없고 다시 볼 필요도 없다.
            store.record_skip(experiment.id, SkipReason.NO_CHANGES.value)
            return None
        if error.reason == "pull_request_body_too_long":
            # 본문을 잘라 보내는데도 넘었다면 재시도해도 낫지 않는다. 기록하지 않으면
            # 매 주기 같은 422를 되풀이한다.
            store.record_skip(experiment.id, SkipReason.BODY_TOO_LONG.value)
            problems.append(error.reason)
            return None
        # `pull_request_forbidden`(App 권한 부재)을 포함해 기록하지 않는다 —
        # 기록하면 권한을 부여한 뒤에도 그 실험은 영영 PR을 얻지 못한다.
        _LOGGER.warning(
            "pull request create failed reason=%s experiment=%s",
            error.reason,
            experiment.id,
        )
        problems.append(error.reason)
        return None


# 이 프로세스가 받는 권한은 이것뿐이다. App이 가진 `Contents: write`까지 함께 받으면
# 이 프로세스가 코드를 push할 수 있게 되고, executor 밖으로 뺀 이유가 무너진다.
PULL_REQUEST_PERMISSIONS: Final = {"pull_requests": "write"}

# skip은 번호와 다른 키에 남긴다. 같은 키에 섞으면 "번호 없음"과 "건너뜀"이
# 구분되지 않는다.
PULL_REQUEST_SKIP_METADATA_KEY: Final = "pull_request_skipped"


class DatabaseExperimentStore:
    """완주한 실험을 읽고 처리 결과를 `experiment_metadata`에 남기는 어댑터."""

    def __init__(self, session) -> None:
        self._session = session

    def list_passed_experiments(self) -> list:
        """`PASSED`만 걷는다.

        완주하지 않은 실험까지 걷으면 리포트도 없는 브랜치로 PR을 열게 된다.
        """
        from applications.experiment_platform.api.experiments.models import (
            Experiment,
            ExperimentStatus,
        )

        return list(
            self._session.query(Experiment)
            .filter(Experiment.status == ExperimentStatus.PASSED.value)
            .order_by(Experiment.created_at)
            .all()
        )

    def _entries(self, experiment_id: uuid.UUID) -> dict[str, str]:
        from applications.experiment_platform.api.experiments.models import ExperimentMetadata

        rows = (
            self._session.query(ExperimentMetadata)
            .filter(ExperimentMetadata.experiment_id == experiment_id)
            .filter(
                ExperimentMetadata.key.in_(
                    [PULL_REQUEST_METADATA_KEY, PULL_REQUEST_SKIP_METADATA_KEY]
                )
            )
            .all()
        )
        return {row.key: row.value for row in rows}

    def pull_request_state(self, experiment_id: uuid.UUID) -> PullRequestState:
        """이미 남아 있는 기록을 읽는다."""
        entries = self._entries(experiment_id)
        raw = entries.get(PULL_REQUEST_METADATA_KEY)
        number: int | None = None
        if raw is not None:
            try:
                number = int(raw)
            except ValueError:
                # 사람이 손으로 넣은 값일 수 있다. tick을 죽이지 않고 없는 것으로 본다.
                _LOGGER.warning(
                    "ignoring non-numeric pull request record experiment=%s",
                    experiment_id,
                )
        return PullRequestState(number, PULL_REQUEST_SKIP_METADATA_KEY in entries)

    def _upsert(self, experiment_id: uuid.UUID, key: str, value: str) -> None:
        """같은 키를 다시 써도 unique 제약에 걸리지 않게 한다.

        재시도가 여기서 죽으면 그 실험이 매 주기 실패로 남는다.
        """
        from applications.experiment_platform.api.experiments.models import ExperimentMetadata

        existing = (
            self._session.query(ExperimentMetadata)
            .filter(ExperimentMetadata.experiment_id == experiment_id)
            .filter(ExperimentMetadata.key == key)
            .one_or_none()
        )
        if existing is None:
            self._session.add(
                ExperimentMetadata(
                    experiment_id=experiment_id, key=key, value=value
                )
            )
        else:
            existing.value = value
        self._session.commit()

    def record_number(self, experiment_id: uuid.UUID, number: int) -> None:
        self._upsert(experiment_id, PULL_REQUEST_METADATA_KEY, str(number))

    def record_skip(self, experiment_id: uuid.UUID, reason: str) -> None:
        self._upsert(experiment_id, PULL_REQUEST_SKIP_METADATA_KEY, reason)


@dataclass(frozen=True)
class PullRequestSettings:
    """이 프로세스가 실제로 쓰는 설정만 담는다.

    `LauncherSettings`를 재사용하지 않는다 — 그쪽은 executor Job 생성용 값을 필수로
    요구하고 `ORCH_EXECUTOR_IMAGE`는 digest 형식 검증까지 한다. PR을 여는 데 그 값이
    필요 없는데도 executor 릴리스마다 이 매니페스트를 따라 고쳐야 한다(#559 선례).
    """

    database_url: str
    github_repository: str
    github_app_id: int
    github_app_installation_id: int
    # executor의 `ORCH_GITHUB_APP_PRIVATE_KEY_FILE`과 **이름을 공유하지 않는다.**
    # 같은 App의 key지만 이 프로세스는 다른 namespace·다른 mountPath에 둔다. 이름을
    # 겹치면 `.env.example`이 한 변수를 서로 다른 경로로 두 번 정의하게 되고,
    # dotenv류 로더가 뒤엣값을 취해 조용히 어긋난다.
    app_private_key_file: str = "/var/run/github-app/key.pem"
    pull_request_interval_sec: int = 60

    def __post_init__(self) -> None:
        """형식 오류를 Pod까지 끌고 가지 않는다."""
        from applications.experiment_platform.launcher.config import (
            LauncherConfigError,
            _REPOSITORY_PATTERN,
        )

        if _REPOSITORY_PATTERN.fullmatch(self.github_repository) is None:
            raise LauncherConfigError("invalid github_repository")

    @classmethod
    def from_environment(cls) -> "PullRequestSettings":
        """환경 변수에서 설정을 읽는다. 없으면 기동 시점에 막는다."""
        import os

        from applications.experiment_platform.launcher.config import (
            _optional_positive_integer_environment,
            _positive_integer_environment,
            _required_environment,
        )

        return cls(
            database_url=_required_environment("ORCH_DATABASE_URL"),
            github_repository=_required_environment("ORCH_GITHUB_REPOSITORY"),
            github_app_id=_positive_integer_environment("ORCH_GITHUB_APP_ID"),
            github_app_installation_id=_positive_integer_environment(
                "ORCH_GITHUB_APP_INSTALLATION_ID"
            ),
            app_private_key_file=os.environ.get(
                "ORCH_PULL_REQUEST_APP_PRIVATE_KEY_FILE",
                "/var/run/github-app/key.pem",
            ).strip()
            or "/var/run/github-app/key.pem",
            # 워크벤치 폴링과 달리 사람이 기다리는 지연이 아니다. 1분이면 충분하고
            # GitHub API 호출량도 그만큼 줄어든다.
            pull_request_interval_sec=_optional_positive_integer_environment(
                "ORCH_PULL_REQUEST_INTERVAL_SEC",
                default=60,
            ),
        )


class GitHubPullRequestOpener:
    """`GitHubPullRequests`(async)를 동기 호출로 감싸는 어댑터.

    오케스트레이션을 동기로 두는 이유는 DB 세션과 같은 흐름에 두기 위해서다 —
    `log_collector`와 같은 모양이라 두 상주 프로세스를 같은 방식으로 읽을 수 있다.

    **token은 호출 때마다 새로 발급한다.** installation token은 수명이 짧고, 이
    프로세스는 1분 주기라 캐시해서 얻는 것보다 만료 처리를 하지 않아 생기는 실패가
    더 비싸다.
    """

    def __init__(
        self,
        repository: str,
        credentials,
        *,
        client: GitHubPullRequests | None = None,
        token_factory=None,
    ) -> None:
        from applications.experiment_platform.shared.github_app import create_installation_token

        self._repository = repository
        self._credentials = credentials
        self._client = client if client is not None else GitHubPullRequests()
        self._token_factory = (
            token_factory if token_factory is not None else create_installation_token
        )

    def _token(self) -> str:
        import asyncio

        token = asyncio.run(
            self._token_factory(
                self._credentials, permissions=PULL_REQUEST_PERMISSIONS
            )
        )
        return token.value

    def create(self, *, head: str, base: str, title: str, body: str) -> int:
        import asyncio

        return asyncio.run(
            self._client.create(
                self._repository,
                head=head,
                base=base,
                title=title,
                body=body,
                token=self._token(),
            )
        )

    def find_open(self, *, head: str) -> int | None:
        import asyncio

        return asyncio.run(
            self._client.find_open(
                self._repository, head=head, base=BASE_BRANCH, token=self._token()
            )
        )


def main() -> int:
    """상주 PR 생성기 진입점.

    engine은 프로세스당 1회 만들고 세션은 주기마다 연다 — `log_collector`와 같은
    방식이다. private key는 Secret Manager가 아니라 mount된 파일에서 읽으며 값은
    로그에 남기지 않는다.
    """
    import signal
    import time
    from pathlib import Path

    from applications.experiment_platform.api.database import (
        create_database_engine,
        create_session_factory,
    )
    from applications.experiment_platform.shared.github_app import GitHubAppCredentials
    from applications.experiment_platform.launcher.resident import run_forever

    logging.basicConfig(level=logging.INFO)
    settings = PullRequestSettings.from_environment()
    # private key는 이 프로세스가 읽지 않는다 — 경로만 넘기고 서명은
    # `github_app`이 한다. 값이 이 모듈의 변수나 로그에 실릴 자리를 만들지 않는다.
    credentials = GitHubAppCredentials(
        app_id=settings.github_app_id,
        installation_id=settings.github_app_installation_id,
        private_key_path=Path(settings.app_private_key_file),
    )
    opener = GitHubPullRequestOpener(settings.github_repository, credentials)
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    stopping = False

    def _request_stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    def tick() -> list[str]:
        with session_factory() as session:
            return open_pull_requests_once(DatabaseExperimentStore(session), opener)

    try:
        _LOGGER.info(
            "pull request opener started repository=%s interval=%ss",
            settings.github_repository,
            settings.pull_request_interval_sec,
        )
        run_forever(
            tick,
            should_stop=lambda: stopping,
            sleep=time.sleep,
            interval_sec=settings.pull_request_interval_sec,
            label="pull request opener",
        )
    finally:
        engine.dispose()
    _LOGGER.info("pull request opener stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
