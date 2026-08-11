"""PASSED 실험 PR 생성의 오케스트레이션·멱등 기록 계약을 검증한다(#689).

[파이프라인] 완주한 실험 목록을 읽어 PR을 만들고 그 번호를 기록하는 한 주기를
담당한다. 판정 규칙(`test_experiment_pull_request.py`)과 REST 경계
(`test_github_pull_requests.py`)는 각각 별도로 검증한다.

[비책임] 실험 실행·상태 전이, 리포트 생성, App 권한 부여는 다루지 않는다.
"""

from __future__ import annotations

from pathlib import Path
import sys
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_orchestration.github_pull_requests import (  # noqa: E402
    GitHubPullRequestError,
)
from agent_orchestration.launcher.pull_request import (  # noqa: E402
    PullRequestState,
    open_pull_requests_once,
)


class _Experiment:
    def __init__(
        self,
        *,
        experiment_id: uuid.UUID | None = None,
        issue_branch: str | None = "exp/689-demo",
        candidate_sha: str | None = "a" * 40,
        report_markdown: str | None = "## 결론",
    ) -> None:
        self.id = experiment_id or uuid.uuid4()
        self.issue_branch = issue_branch
        self.issue_number = 689
        self.issue_title = "데모 가설"
        self.candidate_sha = candidate_sha
        self.report_markdown = report_markdown


class _FakeStore:
    def __init__(self, experiments, states=None) -> None:
        self._experiments = experiments
        self._states = states or {}
        self.recorded: list[tuple[uuid.UUID, int]] = []
        self.skipped: list[tuple[uuid.UUID, str]] = []

    def list_passed_experiments(self):
        return self._experiments

    def pull_request_state(self, experiment_id):
        return self._states.get(experiment_id, PullRequestState(None, False))

    def record_number(self, experiment_id, number: int) -> None:
        self.recorded.append((experiment_id, number))

    def record_skip(self, experiment_id, reason: str) -> None:
        self.skipped.append((experiment_id, reason))


class _FakeOpener:
    def __init__(self, *, number=7, raises=None, found=None) -> None:
        self._number = number
        self._raises = raises
        self._found = found
        self.created: list[dict] = []
        self.lookups: list[str] = []

    def create(self, *, head, base, title, body) -> int:
        self.created.append({"head": head, "base": base, "title": title, "body": body})
        if self._raises is not None:
            raise self._raises
        return self._number

    def find_open(self, *, head) -> int | None:
        self.lookups.append(head)
        return self._found


def test_a_completed_experiment_gets_a_pull_request_and_a_record() -> None:
    experiment = _Experiment()
    store = _FakeStore([experiment])
    opener = _FakeOpener(number=7)

    problems = open_pull_requests_once(store, opener)

    assert problems == []
    assert opener.created[0]["head"] == "exp/689-demo"
    assert opener.created[0]["base"] == "dev"
    assert store.recorded == [(experiment.id, 7)]


def test_a_recorded_experiment_is_not_opened_again() -> None:
    """기록이 있으면 GitHub을 호출하지도 않는다."""
    experiment = _Experiment()
    store = _FakeStore(
        [experiment], states={experiment.id: PullRequestState(7, False)}
    )
    opener = _FakeOpener()

    problems = open_pull_requests_once(store, opener)

    assert problems == []
    assert opener.created == []
    assert store.recorded == []


def test_a_skipped_experiment_is_not_revisited() -> None:
    """영구 skip으로 기록된 실험은 다시 판정하지 않는다."""
    experiment = _Experiment()
    store = _FakeStore(
        [experiment], states={experiment.id: PullRequestState(None, True)}
    )
    opener = _FakeOpener()

    problems = open_pull_requests_once(store, opener)

    assert problems == []
    assert opener.created == []


def test_nothing_committed_is_recorded_so_it_stops_coming_back() -> None:
    """`candidate_sha`가 없으면 기록해 재시도 대상에서 뺀다."""
    experiment = _Experiment(candidate_sha=None)
    store = _FakeStore([experiment])
    opener = _FakeOpener()

    problems = open_pull_requests_once(store, opener)

    assert problems == []
    assert opener.created == []
    assert store.skipped == [(experiment.id, "no_changes")]


def test_a_missing_report_is_not_recorded_so_it_retries() -> None:
    """리포트는 나중에 도착한다 — 굳히면 그 실험은 영영 PR을 못 얻는다."""
    experiment = _Experiment(report_markdown=None)
    store = _FakeStore([experiment])
    opener = _FakeOpener()

    problems = open_pull_requests_once(store, opener)

    assert problems == []
    assert store.skipped == []
    assert store.recorded == []


def test_an_existing_pull_request_is_recovered_by_lookup() -> None:
    """만들고 기록 전에 죽은 흔적이다 — 중복을 만들지 않고 번호만 채운다."""
    experiment = _Experiment()
    store = _FakeStore([experiment])
    opener = _FakeOpener(
        raises=GitHubPullRequestError("pull_request_exists", status_code=422),
        found=11,
    )

    problems = open_pull_requests_once(store, opener)

    assert problems == []
    assert opener.lookups == ["exp/689-demo"]
    assert store.recorded == [(experiment.id, 11)]


def test_no_commits_between_is_recorded_as_no_changes() -> None:
    """브랜치와 base가 같다 — 조회해도 소용없고 다시 볼 필요도 없다."""
    experiment = _Experiment()
    store = _FakeStore([experiment])
    opener = _FakeOpener(
        raises=GitHubPullRequestError("pull_request_no_commits", status_code=422)
    )

    problems = open_pull_requests_once(store, opener)

    assert problems == []
    assert opener.lookups == []
    assert store.skipped == [(experiment.id, "no_changes")]


def test_missing_permission_surfaces_its_own_reason_and_is_not_recorded() -> None:
    """App 권한이 없는 상태다. 기록하면 권한을 준 뒤에도 영영 안 만든다."""
    experiment = _Experiment()
    store = _FakeStore([experiment])
    opener = _FakeOpener(
        raises=GitHubPullRequestError("pull_request_forbidden", status_code=403)
    )

    problems = open_pull_requests_once(store, opener)

    assert problems == ["pull_request_forbidden"]
    assert store.skipped == []
    assert store.recorded == []


class _ExplodingStore(_FakeStore):
    """첫 실험의 상태 조회에서만 분류 안 된 예외를 던진다."""

    def __init__(self, experiments, bad_id) -> None:
        super().__init__(experiments)
        self._bad_id = bad_id

    def pull_request_state(self, experiment_id):
        if experiment_id == self._bad_id:
            raise RuntimeError("transient failure")
        return PullRequestState(None, False)


def test_one_failing_experiment_does_not_stop_the_rest() -> None:
    """실험 단위로 격리한다 — A가 계속 실패하면 B가 영영 PR을 못 얻는다."""
    bad = _Experiment()
    good = _Experiment()
    store = _ExplodingStore([bad, good], bad.id)
    opener = _FakeOpener(number=9)

    problems = open_pull_requests_once(store, opener)

    assert problems == ["experiment_promotion_failed"]
    assert store.recorded == [(good.id, 9)]


class _AppErrorOpener(_FakeOpener):
    """token 발급 단계에서 실패하는 opener — `GitHubAppError`는 REST 오류가 아니다."""

    def create(self, *, head, base, title, body) -> int:
        from agent_orchestration.github_app import GitHubAppError

        raise GitHubAppError("private_key_unavailable")


def test_token_failure_has_its_own_reason() -> None:
    """정본 계약 §오류 분류의 `pull_request_token_failed`가 실제로 나와야 한다.

    키 마운트 경로가 틀린 초기 롤아웃에서 가장 먼저 걸리는 경로다. 일반 예외로 뭉치면
    "DB가 끊겼다"와 구분되지 않는다.
    """
    experiment = _Experiment()
    store = _FakeStore([experiment])

    problems = open_pull_requests_once(store, _AppErrorOpener())

    assert problems == ["pull_request_token_failed"]
    assert store.recorded == []
    assert store.skipped == []


class _RecordFailingStore(_FakeStore):
    def record_number(self, experiment_id, number: int) -> None:
        raise RuntimeError("db down")


def test_record_failure_has_its_own_reason() -> None:
    """PR은 만들어졌는데 기록만 실패한 상태다 — 일반 실패와 구분되어야 한다.

    다음 주기에 `exists`로 회복되므로 중복은 생기지 않지만, 사유가 뭉개지면 그
    회복이 도는 중인지 알 수 없다.
    """
    experiment = _Experiment()
    store = _RecordFailingStore([experiment])

    problems = open_pull_requests_once(store, _FakeOpener(number=7))

    assert problems == ["pull_request_record_failed"]


def test_body_too_long_is_recorded_so_it_stops_retrying() -> None:
    """재시도해도 낫지 않는다 — 기록하지 않으면 매 주기 같은 422를 되풀이한다."""
    experiment = _Experiment()
    store = _FakeStore([experiment])
    opener = _FakeOpener(
        raises=GitHubPullRequestError("pull_request_body_too_long", status_code=422)
    )

    problems = open_pull_requests_once(store, opener)

    assert problems == ["pull_request_body_too_long"]
    assert store.skipped == [(experiment.id, "body_too_long")]


def test_a_closed_pull_request_is_recorded_under_its_own_reason() -> None:
    """"이미 존재하는데 열린 것이 없다" = 사람이 닫았다.

    `already_recorded`로 뭉치면 운영자가 왜 굳었는지 알 수 없다.
    """
    experiment = _Experiment()
    store = _FakeStore([experiment])
    opener = _FakeOpener(
        raises=GitHubPullRequestError("pull_request_exists", status_code=422),
        found=None,
    )

    problems = open_pull_requests_once(store, opener)

    assert problems == []
    assert store.skipped == [(experiment.id, "closed_by_human")]
