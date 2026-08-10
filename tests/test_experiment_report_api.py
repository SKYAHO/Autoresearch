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
from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from agent_orchestration.app.database import Base
from agent_orchestration.app.experiments.models import Experiment, ExperimentStatus
from agent_orchestration.app.experiments.repository import find_experiment_report
from agent_orchestration.app.experiments.schemas import (
    MAX_REPORT_MARKDOWN_BYTES,
    CandidateReportRequest,
    ExecutorResultReportRequest,
    ExperimentCreate,
)
from agent_orchestration.app.experiments.service import (
    create_experiment,
    normalize_report_markdown,
    record_candidate,
    record_experiment_result,
)

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
    """리포트 적재가 터져도 지표 커밋은 남는다.

    PostgreSQL의 NUL 거부와 배포 순서 어긋남(`UndefinedColumn`)은 SQLite에서 재현되지
    않으므로 같은 자리에 예외를 주입해 성질만 고정한다 — 검증 대상은 실패의 종류가
    아니라 **지표가 살아남는가**다.
    """
    import agent_orchestration.app.experiments.service as service_module

    experiment_id = _evaluating_experiment(db_session)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(service_module, "find_experiment_report", explode)
    record_experiment_result(
        db_session,
        experiment_id,
        _result_request(experiment_id, report_markdown="# 결론"),
    )

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
