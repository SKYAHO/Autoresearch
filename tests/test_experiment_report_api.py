"""실험 리포트 본문의 적재·정규화·조회 계약을 검증한다.

전체 파이프라인에서 executor가 완주 보고에 실은 `report.md` 본문이 DB에 적재되고
워크벤치가 그것을 별도 endpoint로 읽어 가는 구간의 service·HTTP 경계를 검증한다.
markdown → HTML 변환과 화면 렌더링은 `tests/test_agent_orchestration_ui_report.py`가
담당한다.
"""

from __future__ import annotations

from collections.abc import Iterator
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from applications.experiment_platform.api import main as main_module
from applications.experiment_platform.api.config import ServiceSettings
from applications.experiment_platform.api.database import Base
from applications.experiment_platform.api.experiments.models import Experiment, ExperimentStatus
from applications.experiment_platform.api.experiments.repository import find_experiment_report
from applications.experiment_platform.api.experiments.schemas import (
    MAX_REPORT_MARKDOWN_BYTES,
    CandidateReportRequest,
    ExecutorResultReportRequest,
    ExperimentCreate,
)
from applications.experiment_platform.api.experiments.service import (
    create_experiment,
    normalize_report_markdown,
    record_candidate,
    record_experiment_result,
)

API_TOKEN = "a" * 32
AUTH_HEADERS = {"X-Orch-Token": API_TOKEN}

ISSUE_NUMBER = 647
ISSUE_BRANCH = "exp/647"
BASE_DEV_SHA = "a" * 40
CANDIDATE_SHA = "b" * 40
SNAPSHOT = {"contract_version": "experiment-metric-snapshot-v1", "primary_metric": "roc_auc"}


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    """PostgreSQL UUID 함수를 재현하는 in-memory SQLAlchemy engine을 제공한다."""
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def register_uuid_function(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function(
            "gen_random_uuid", 0, lambda: uuid.uuid4().hex
        )

    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def db_session(sqlite_engine: Engine) -> Iterator[Session]:
    """요청 단위 Session을 제공한다."""
    factory = sessionmaker(bind=sqlite_engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


def test_report_markdown_is_deferred_from_the_default_select(db_session: Session) -> None:
    """목록 질의가 리포트 본문을 끌어오지 않는다.

    `find_experiments`는 `select(Experiment)`로 전체 컬럼을 읽으므로, 평범한 컬럼으로
    두면 목록 한 번이 최대 100행 × 64KB를 전송한다. deferred가 그것을 막는 유일한
    장치라 계약으로 고정한다.
    """
    experiment = Experiment(hypothesis="가설", report_markdown="# 리포트")
    db_session.add(experiment)
    db_session.commit()
    db_session.expunge_all()

    loaded = db_session.get(Experiment, experiment.id)
    assert "report_markdown" in inspect(loaded).unloaded


def test_find_experiment_report_loads_the_body(db_session: Session) -> None:
    """조회 경로는 `undefer`로 본문을 함께 싣는다."""
    experiment = Experiment(hypothesis="가설", report_markdown="# 리포트")
    db_session.add(experiment)
    db_session.commit()
    db_session.expunge_all()

    loaded = find_experiment_report(db_session, experiment.id)
    assert loaded is not None
    assert "report_markdown" not in inspect(loaded).unloaded
    assert loaded.report_markdown == "# 리포트"


def test_find_experiment_report_returns_none_for_a_missing_experiment(
    db_session: Session,
) -> None:
    """없는 실험은 None이다 — 예외를 올리는 것은 service의 몫이다."""
    assert find_experiment_report(db_session, uuid.uuid4()) is None


def _evaluating_experiment(session: Session) -> uuid.UUID:
    """candidate까지 보고된 EVALUATING 실험 하나를 만든다.

    `record_candidate`는 이미 봉인된 이슈 좌표(issue_number/issue_branch/base_dev_sha)와
    RUNNING 상태를 전제한다(`tests/test_experiment_candidate_api.py`의
    `_running_experiment`와 같은 전제). candidate 보고 전에 여기서 좌표를 봉인해 둔다.
    """
    experiment = create_experiment(session, ExperimentCreate(hypothesis="가설"))
    experiment.issue_number = ISSUE_NUMBER
    experiment.issue_branch = ISSUE_BRANCH
    experiment.base_dev_sha = BASE_DEV_SHA
    experiment.status = ExperimentStatus.RUNNING.value
    session.commit()
    record_candidate(
        session,
        experiment.id,
        CandidateReportRequest(
            idempotency_key=f"executor-candidate:{experiment.id}",
            issue_number=ISSUE_NUMBER,
            issue_branch=ISSUE_BRANCH,
            base_dev_sha=BASE_DEV_SHA,
            candidate_sha=CANDIDATE_SHA,
        ),
    )
    return experiment.id


def _result_request(experiment_id: uuid.UUID, **overrides: object) -> ExecutorResultReportRequest:
    """완주 보고 요청을 만든다."""
    values: dict[str, object] = {
        "idempotency_key": f"executor-result:{experiment_id}",
        "candidate_sha": CANDIDATE_SHA,
        "metric_snapshot": SNAPSHOT,
    }
    values.update(overrides)
    return ExecutorResultReportRequest.model_validate(values)


def test_result_report_without_a_report_still_passes(db_session: Session) -> None:
    """리포트 없는 기존 보고 경로가 그대로 성립한다 (회귀)."""
    experiment_id = _evaluating_experiment(db_session)
    record_experiment_result(db_session, experiment_id, _result_request(experiment_id))

    stored = find_experiment_report(db_session, experiment_id)
    assert stored is not None
    assert stored.status == ExperimentStatus.PASSED.value
    assert stored.report_markdown is None


def test_result_report_stores_the_report_body(db_session: Session) -> None:
    """리포트를 실으면 본문이 적재되고 지표 전이도 그대로 일어난다."""
    experiment_id = _evaluating_experiment(db_session)
    record_experiment_result(
        db_session,
        experiment_id,
        _result_request(experiment_id, report_markdown="# 결론\n\n올랐다."),
    )

    stored = find_experiment_report(db_session, experiment_id)
    assert stored is not None
    assert stored.status == ExperimentStatus.PASSED.value
    assert stored.report_markdown == "# 결론\n\n올랐다."


def test_report_write_failure_leaves_the_metric_commit_in_place(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """리포트 적재가 터져도 지표 커밋은 남고, 반환된 객체도 살아 있다.

    PostgreSQL의 NUL 거부와 배포 순서 어긋남(`UndefinedColumn`)은 SQLite에서 재현되지
    않으므로 같은 자리에 예외를 주입해 성질만 고정한다 — 검증 대상은 실패의 종류가
    아니라 **지표가 살아남는가**다.

    `_store_report_markdown`이 요청 `session`을 직접 쓰면, 그 안의 `with
    session.begin()`이 예외로 rollback할 때 요청 세션에 로드된 모든 객체가
    expire된다 — `record_experiment_result`가 반환한 `experiment`도 포함된다.
    그러면 호출자(`executor_router.py`)가 `ExperimentResponse.model_validate`로
    속성을 읽는 순간 새 SELECT가 나가고, 리포트 쓰기가 연결 끊김으로 실패했다면
    그 SELECT도 같은 이유로 실패해 이미 커밋된 200 응답이 500으로 바뀐다. 그래서
    반환값이 expired가 아님을 직접 단언한다 — 이전 버전의 이 테스트는 반환값을
    검사하지 않아 이 귀결을 놓쳤다.
    """
    import applications.experiment_platform.api.experiments.service as service_module

    experiment_id = _evaluating_experiment(db_session)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(service_module, "find_experiment_report", explode)
    returned = record_experiment_result(
        db_session,
        experiment_id,
        _result_request(experiment_id, report_markdown="# 결론"),
    )

    assert inspect(returned).expired is False

    db_session.expunge_all()
    stored = find_experiment_report(db_session, experiment_id)
    assert stored is not None
    assert stored.status == ExperimentStatus.PASSED.value
    assert stored.report_markdown is None


def test_retry_with_a_different_report_keeps_the_first_and_warns(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """재시도가 다른 본문을 실어도 첫 보고가 정본이고, 조용히 버리지 않는다."""
    experiment_id = _evaluating_experiment(db_session)
    record_experiment_result(
        db_session, experiment_id, _result_request(experiment_id, report_markdown="첫 번째")
    )
    with caplog.at_level("WARNING"):
        record_experiment_result(
            db_session,
            experiment_id,
            _result_request(experiment_id, report_markdown="두 번째"),
        )

    stored = find_experiment_report(db_session, experiment_id)
    assert stored is not None
    assert stored.report_markdown == "첫 번째"
    assert "already set, mismatch on retry" in caplog.text
    assert "두 번째" not in caplog.text


def test_normalize_strips_nul_and_truncates_without_rejecting() -> None:
    """정규화는 거절하지 않는다 — NUL을 지우고 상한을 넘으면 자른다."""
    assert normalize_report_markdown("가\x00나") == "가나"

    oversized = "가" * MAX_REPORT_MARKDOWN_BYTES
    normalized = normalize_report_markdown(oversized)
    assert len(normalized.encode("utf-8")) <= MAX_REPORT_MARKDOWN_BYTES
    assert normalized.endswith("\n")
    assert "잘렸습니다" in normalized


def test_normalize_keeps_a_body_within_the_limit_untouched() -> None:
    """상한 안이면 그대로 둔다 — 문구가 붙지 않는다."""
    assert normalize_report_markdown("# 결론") == "# 결론"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """일반 API 토큰 경계로 리포트 조회 endpoint를 SQLite에서 실행한다.

    `db_session`이 만드는 engine과는 별도의 in-memory DB라 함께 쓰면 서로 다른
    데이터베이스가 된다(`tests/test_experiment_candidate_api.py`의 `executor_client`와
    같은 이유). 데이터 준비는 `client.app.state.experiment_session_factory`를 거친다.
    """
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def register_uuid_function(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)

    Base.metadata.create_all(engine)
    settings = ServiceSettings(
        openai_api_key=None,
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
        api_token=API_TOKEN,
        github_token="x" * 40,
        github_repository="SKYAHO/Autoresearch",
        gh_timeout_sec=30,
        issue_daily_limit=20,
    )
    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(main_module, "ensure_schema", lambda *_args: None)
    monkeypatch.setattr(main_module, "create_database_engine", lambda *_args: engine)

    app = main_module.create_app()
    with TestClient(app) as test_client:
        yield test_client
    Base.metadata.drop_all(engine)
    engine.dispose()


def _create_evaluating_experiment_for_http(client: TestClient) -> uuid.UUID:
    """`client`의 session factory로 candidate까지 보고된 EVALUATING 실험을 만든다.

    `db_session`을 함께 받지 않는다 — `client`가 물린 engine과 다른 DB라 보이지 않는다.
    """
    factory = client.app.state.experiment_session_factory
    with factory() as session:
        return _evaluating_experiment(session)


def test_report_endpoint_returns_the_body(client: TestClient) -> None:
    """리포트가 있으면 본문을 돌려준다."""
    experiment_id = _create_evaluating_experiment_for_http(client)
    factory = client.app.state.experiment_session_factory
    with factory() as session:
        record_experiment_result(
            session, experiment_id, _result_request(experiment_id, report_markdown="# 결론")
        )

    response = client.get(f"/experiments/{experiment_id}/report", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["report_markdown"] == "# 결론"
    assert response.json()["experiment_id"] == str(experiment_id)


def test_report_endpoint_returns_null_when_there_is_no_report(client: TestClient) -> None:
    """실험은 있고 리포트가 없으면 404가 아니라 200 + null이다.

    404로 만들면 UI가 "실험이 사라졌다"와 "아직 리포트가 없다"를 구별할 수 없다.
    후자는 오류가 아니라 정상 상태다.
    """
    experiment_id = _create_evaluating_experiment_for_http(client)

    response = client.get(f"/experiments/{experiment_id}/report", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["report_markdown"] is None


def test_report_endpoint_404s_for_a_missing_experiment(client: TestClient) -> None:
    """없는 실험은 404다."""
    response = client.get(f"/experiments/{uuid.uuid4()}/report", headers=AUTH_HEADERS)
    assert response.status_code == 404


def test_report_endpoint_requires_the_api_token(client: TestClient) -> None:
    """토큰 없이 리포트를 읽을 수 없다."""
    experiment_id = _create_evaluating_experiment_for_http(client)
    assert client.get(f"/experiments/{experiment_id}/report").status_code == 401


def test_executor_and_api_share_the_same_report_size_limit() -> None:
    """executor와 API의 상한이 갈리면 지표가 죽는다 — 두 값을 고정한다.

    `executor`는 `app` 패키지를 import하지 않으므로 상수를 공유할 수 없다. 드리프트를
    막는 것은 이 테스트뿐이다.
    """
    from applications.experiment_platform.executor.report import (
        MAX_REPORT_MARKDOWN_BYTES as EXECUTOR_LIMIT,
    )

    assert EXECUTOR_LIMIT == MAX_REPORT_MARKDOWN_BYTES


def test_truncate_keeps_a_body_within_the_limit_untouched() -> None:
    """상한 안이면 그대로다."""
    from applications.experiment_platform.executor.report import truncate_report_markdown

    assert truncate_report_markdown("# 결론") == "# 결론"


def test_truncate_cuts_on_a_character_boundary() -> None:
    """멀티바이트 문자가 상한에 걸쳐도 깨진 문자를 남기지 않는다."""
    from applications.experiment_platform.executor.report import (
        MAX_REPORT_MARKDOWN_BYTES as LIMIT,
        truncate_report_markdown,
    )

    truncated = truncate_report_markdown("가" * LIMIT)

    assert len(truncated.encode("utf-8")) <= LIMIT
    assert "�" not in truncated
    assert truncated.endswith("\n")
    assert "executor에서 잘렸습니다" in truncated


def test_read_report_markdown_absorbs_a_missing_file(tmp_path) -> None:
    """본문 읽기 실패는 None이다 — 지표 보고를 막지 않는다."""
    from applications.experiment_platform.executor.report import read_report_markdown

    assert read_report_markdown(tmp_path / "없음.md") is None


def test_read_report_markdown_treats_a_blank_report_as_absent(tmp_path) -> None:
    """공백뿐인 리포트는 없는 것으로 본다."""
    from applications.experiment_platform.executor.report import read_report_markdown

    path = tmp_path / "report.md"
    path.write_text("   \n\n", encoding="utf-8")
    assert read_report_markdown(path) is None
