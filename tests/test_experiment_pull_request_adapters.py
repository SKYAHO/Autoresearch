"""PR 생성 어댑터(DB store·토큰·설정)의 계약을 검증한다(#689).

[파이프라인] 판정·REST 경계와 실제 DB·GitHub 사이를 잇는 얇은 층을 담당한다.
변환 로직은 거의 없지만 **호출 인자와 권한 범위는 여기서 고정한다** — 틀려도 예외가
아니라 잘못된 성공으로 나타나는 종류다.

[비책임] 판정 규칙·오케스트레이션·REST 응답 해석은 각각 별도 테스트가 다룬다.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import sys
import uuid

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_orchestration.app.database import Base  # noqa: E402
from agent_orchestration.app.experiments.models import (  # noqa: E402
    Experiment,
    ExperimentMetadata,
    ExperimentStatus,
)
from agent_orchestration.launcher.pull_request import (  # noqa: E402
    PULL_REQUEST_METADATA_KEY,
    PULL_REQUEST_PERMISSIONS,
    PULL_REQUEST_SKIP_METADATA_KEY,
    DatabaseExperimentStore,
    PullRequestSettings,
)


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def register_uuid_function(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)

    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def db_session(sqlite_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=sqlite_engine, expire_on_commit=False)
    with factory() as session:
        yield session


def _experiment(session: Session, status: ExperimentStatus) -> Experiment:
    experiment = Experiment(
        hypothesis="데모",
        status=status.value,
        issue_branch="exp/689-demo",
        issue_number=689,
        candidate_sha="a" * 40,
        report_markdown="## 결론",
    )
    session.add(experiment)
    session.commit()
    return experiment


def test_only_passed_experiments_are_listed(db_session: Session) -> None:
    """완주하지 않은 실험까지 걷으면 리포트도 없는 브랜치로 PR을 열게 된다."""
    passed = _experiment(db_session, ExperimentStatus.PASSED)
    _experiment(db_session, ExperimentStatus.RUNNING)
    _experiment(db_session, ExperimentStatus.FAILED)
    store = DatabaseExperimentStore(db_session)

    listed = store.list_passed_experiments()

    assert [item.id for item in listed] == [passed.id]


def test_state_is_empty_before_anything_is_recorded(db_session: Session) -> None:
    experiment = _experiment(db_session, ExperimentStatus.PASSED)
    store = DatabaseExperimentStore(db_session)

    state = store.pull_request_state(experiment.id)

    assert state.number is None
    assert state.skipped is False


def test_recorded_number_round_trips(db_session: Session) -> None:
    experiment = _experiment(db_session, ExperimentStatus.PASSED)
    store = DatabaseExperimentStore(db_session)

    store.record_number(experiment.id, 42)

    assert store.pull_request_state(experiment.id).number == 42


def test_recording_the_same_number_twice_is_not_an_error(db_session: Session) -> None:
    """재시도가 unique 제약에 걸려 죽으면 그 실험이 매 주기 실패로 남는다."""
    experiment = _experiment(db_session, ExperimentStatus.PASSED)
    store = DatabaseExperimentStore(db_session)

    store.record_number(experiment.id, 42)
    store.record_number(experiment.id, 42)

    assert store.pull_request_state(experiment.id).number == 42


def test_skip_is_recorded_under_its_own_key(db_session: Session) -> None:
    """번호와 skip을 같은 키에 섞으면 "번호 없음"과 "건너뜀"이 구분되지 않는다."""
    experiment = _experiment(db_session, ExperimentStatus.PASSED)
    store = DatabaseExperimentStore(db_session)

    store.record_skip(experiment.id, "no_changes")

    state = store.pull_request_state(experiment.id)
    assert state.skipped is True
    assert state.number is None
    keys = {
        entry.key
        for entry in db_session.query(ExperimentMetadata)
        .filter(ExperimentMetadata.experiment_id == experiment.id)
        .all()
    }
    assert keys == {PULL_REQUEST_SKIP_METADATA_KEY}


def test_a_non_numeric_record_does_not_crash_the_tick(db_session: Session) -> None:
    """사람이 손으로 넣은 값이 숫자가 아닐 수 있다 — 없는 것으로 다룬다."""
    experiment = _experiment(db_session, ExperimentStatus.PASSED)
    db_session.add(
        ExperimentMetadata(
            experiment_id=experiment.id,
            key=PULL_REQUEST_METADATA_KEY,
            value="not-a-number",
        )
    )
    db_session.commit()
    store = DatabaseExperimentStore(db_session)

    assert store.pull_request_state(experiment.id).number is None


def test_token_requests_only_the_pull_request_permission() -> None:
    """token은 App이 가진 권한 전부가 아니라 필요한 것만 받는다.

    `Contents: write`까지 함께 받으면 이 프로세스가 코드를 push할 수 있게 된다 —
    executor 밖으로 뺀 이유가 무너진다.
    """
    assert PULL_REQUEST_PERMISSIONS == {"pull_requests": "write"}


def test_settings_read_only_what_this_process_uses(monkeypatch) -> None:
    """`LauncherSettings`를 재사용하지 않는다 — 안 쓰는 값에 배포가 묶인다(#559 선례)."""
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://u:p@h/d")
    monkeypatch.setenv("ORCH_GITHUB_REPOSITORY", "SKYAHO/Autoresearch")
    monkeypatch.setenv("ORCH_GITHUB_APP_SECRET_NAME", "branch-writer")
    monkeypatch.setenv("ORCH_GITHUB_APP_ID", "4502568")
    monkeypatch.setenv("ORCH_GITHUB_APP_INSTALLATION_ID", "151609037")

    settings = PullRequestSettings.from_environment()

    assert settings.database_url == "postgresql://u:p@h/d"
    assert settings.github_repository == "SKYAHO/Autoresearch"
    assert settings.github_app_id == 4502568
    assert settings.pull_request_interval_sec == 60


def test_settings_reject_a_malformed_repository(monkeypatch) -> None:
    """형식 오류를 Pod까지 끌고 가지 않는다 — 기동 시점에 막는다."""
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://u:p@h/d")
    monkeypatch.setenv("ORCH_GITHUB_REPOSITORY", "Autoresearch")
    monkeypatch.setenv("ORCH_GITHUB_APP_SECRET_NAME", "branch-writer")
    monkeypatch.setenv("ORCH_GITHUB_APP_ID", "1")
    monkeypatch.setenv("ORCH_GITHUB_APP_INSTALLATION_ID", "1")

    with pytest.raises(Exception):
        PullRequestSettings.from_environment()


class _FakeClient:
    """`GitHubPullRequests`의 async 인터페이스만 흉내 내는 더블."""

    def __init__(self) -> None:
        self.create_calls: list[dict] = []
        self.find_calls: list[dict] = []

    async def create(self, repository, *, head, base, title, body, token) -> int:
        self.create_calls.append(
            {
                "repository": repository,
                "head": head,
                "base": base,
                "title": title,
                "body": body,
                "token": token,
            }
        )
        return 5

    async def find_open(self, repository, *, head, token) -> int | None:
        self.find_calls.append({"repository": repository, "head": head})
        return 6


class _FakeToken:
    def __init__(self, value: str) -> None:
        self.value = value


def _opener(client):
    from agent_orchestration.launcher.pull_request import GitHubPullRequestOpener

    seen: list[dict] = []

    async def token_factory(credentials, *, permissions):
        seen.append({"credentials": credentials, "permissions": permissions})
        return _FakeToken("minted")

    opener = GitHubPullRequestOpener(
        "SKYAHO/Autoresearch",
        object(),
        client=client,
        token_factory=token_factory,
    )
    return opener, seen


def test_opener_passes_the_repository_and_minted_token() -> None:
    """저장소나 token이 어긋나면 예외가 아니라 잘못된 성공으로 나타난다."""
    client = _FakeClient()
    opener, _ = _opener(client)

    number = opener.create(head="exp/689-demo", base="dev", title="t", body="b")

    assert number == 5
    call = client.create_calls[0]
    assert call["repository"] == "SKYAHO/Autoresearch"
    assert call["head"] == "exp/689-demo"
    assert call["base"] == "dev"
    assert call["token"] == "minted"


def test_opener_requests_only_the_pull_request_permission() -> None:
    """token 발급 시점에 권한 범위가 정해진다 — 여기서 넓히면 상수만으론 못 막는다."""
    client = _FakeClient()
    opener, seen = _opener(client)

    opener.create(head="h", base="dev", title="t", body="b")

    assert seen[0]["permissions"] == {"pull_requests": "write"}
    assert "contents" not in seen[0]["permissions"]


def test_opener_find_open_reaches_the_same_repository() -> None:
    client = _FakeClient()
    opener, _ = _opener(client)

    assert opener.find_open(head="exp/689-demo") == 6
    assert client.find_calls[0]["repository"] == "SKYAHO/Autoresearch"
