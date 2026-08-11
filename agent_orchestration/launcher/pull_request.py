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
from dataclasses import dataclass
from typing import Final, Protocol


# 멱등 판정이 이 키에 걸려 있다. 바꾸면 이미 PR을 만든 실험을 다시 만든다.
# `experiment_metadata.key`가 max_length=64다.
PULL_REQUEST_METADATA_KEY: Final = "pull_request_number"

# 실험 브랜치가 향하는 곳. `main` 반영은 이 계약 밖이다(#509).
BASE_BRANCH: Final = "dev"

# GitHub PR 제목 상한. 넘기면 생성이 422로 거부된다.
_TITLE_LIMIT: Final = 256
_TITLE_PREFIX: Final = "[AR] "


class SkipReason(enum.Enum):
    """PR을 만들지 않고 넘어가는 **정상** 상황. 오류가 아니다."""

    ALREADY_RECORDED = "already_recorded"
    REPORT_MISSING = "report_missing"
    BRANCH_MISSING = "branch_missing"
    NO_CHANGES = "no_changes"

    @property
    def permanent(self) -> bool:
        """다시 볼 필요가 없는가.

        영구 skip은 기록으로 남겨 재시도 대상에서 뺀다. 그러지 않으면 그 실험이 매
        주기 돌아와 아무 일도 하지 않고 조회만 반복한다.

        리포트와 브랜치는 나중에 도착할 수 있으므로 굳히지 않는다.
        """
        return self in {SkipReason.ALREADY_RECORDED, SkipReason.NO_CHANGES}


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
    lines = [report, "", "---", ""]
    if experiment.issue_number is not None:
        lines.append(f"실험 이슈: #{experiment.issue_number}")
    if experiment.candidate_sha:
        lines.append(f"candidate: `{experiment.candidate_sha}`")
    lines.append(
        "이 PR은 실험이 **완주**해 자동으로 열렸습니다. "
        "가설의 성패는 위 리포트가 서술하며, 머지 여부는 사람이 정합니다."
    )
    return "\n".join(lines)
