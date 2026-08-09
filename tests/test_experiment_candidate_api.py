"""Executor candidate·결과 보고 service의 저장·전이 계약을 검증한다.

전체 파이프라인에서 원격 Git 검증이 끝난 executor가 candidate SHA를 실험 lineage에 한 번
저장하고 평가로 넘기는 구간과, 채점이 끝난 뒤 지표를 보고해 완주를 확정하는 구간의
service 경계를 검증한다. HTTP 인증과 실제 PostgreSQL migration은 이 모듈의 범위가 아니다.
"""

from __future__ import annotations

from collections.abc import Iterator
import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from agent_orchestration.app import main as main_module
from agent_orchestration.app.config import ServiceSettings
from agent_orchestration.app.database import Base
from agent_orchestration.app.experiments.exceptions import (
    CandidateConflictError,
    IdempotencyConflictError,
)
from agent_orchestration.app.experiments.models import (
    Experiment,
    ExperimentEvent,
    ExperimentStatus,
)
from agent_orchestration.app.experiments.schemas import (
    MAX_METRIC_SNAPSHOT_BYTES,
    CandidateReportRequest,
    ExecutorResultReportRequest,
    ExperimentCreate,
)
from agent_orchestration.app.experiments.service import (
    create_experiment,
    record_candidate,
    record_experiment_result,
)


ISSUE_NUMBER = 557
ISSUE_BRANCH = "exp/557-candidate-contract"
BASE_DEV_SHA = "a" * 40
CANDIDATE_SHA = "b" * 40
API_TOKEN = "a" * 32
EXECUTOR_TOKEN = "e" * 32
EXECUTOR_HEADERS = {"X-Orch-Executor-Token": EXECUTOR_TOKEN}


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    """PostgreSQL UUID 함수를 재현하는 in-memory SQLAlchemy engine을 제공한다."""
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
    """테스트마다 독립적인 ORM Session을 제공한다."""
    factory = sessionmaker(bind=sqlite_engine, expire_on_commit=False)
    with factory() as session:
        yield session


def _running_experiment(session: Session) -> Experiment:
    """candidate 보고가 허용되는 봉인 좌표의 RUNNING Experiment를 만든다."""
    experiment = create_experiment(session, ExperimentCreate(hypothesis="candidate report"))
    experiment.issue_number = ISSUE_NUMBER
    experiment.issue_branch = ISSUE_BRANCH
    experiment.base_dev_sha = BASE_DEV_SHA
    experiment.status = ExperimentStatus.RUNNING.value
    session.commit()
    return experiment


@pytest.mark.parametrize(
    "issue_branch",
    [
        # #589 이후 API가 봉인하는 형식이다.
        f"exp/{ISSUE_NUMBER}",
        # #589 이전에 발행된 실험이 DB에 들고 있는 형식이다. 여기서 막으면 진행 중인
        # 실험의 candidate 보고가 fail-closed된다.
        f"exp/{ISSUE_NUMBER}-candidate-contract",
    ],
)
def test_candidate_report_accepts_both_sealed_branch_forms(issue_branch: str) -> None:
    """브랜치 이름의 이슈 번호가 좌표와 맞으면 slug 유무는 따지지 않는다."""
    request = CandidateReportRequest.model_validate(
        {
            "idempotency_key": "executor-candidate:1",
            "issue_number": ISSUE_NUMBER,
            "issue_branch": issue_branch,
            "base_dev_sha": BASE_DEV_SHA,
            "candidate_sha": CANDIDATE_SHA,
        }
    )

    assert request.issue_branch == issue_branch


def test_candidate_report_rejects_a_branch_of_another_issue() -> None:
    """브랜치 이름과 좌표가 갈리면 요청 단계에서 막는다."""
    with pytest.raises(ValidationError):
        CandidateReportRequest.model_validate(
            {
                "idempotency_key": "executor-candidate:1",
                "issue_number": ISSUE_NUMBER,
                "issue_branch": f"exp/{ISSUE_NUMBER + 1}",
                "base_dev_sha": BASE_DEV_SHA,
                "candidate_sha": CANDIDATE_SHA,
            }
        )


def _request(experiment_id: uuid.UUID, **overrides: object) -> CandidateReportRequest:
    """유효한 candidate 보고 요청에 필요한 한 필드만 바꿔 만든다."""
    values: dict[str, object] = {
        "idempotency_key": f"executor-candidate:{experiment_id}",
        "issue_number": ISSUE_NUMBER,
        "issue_branch": ISSUE_BRANCH,
        "base_dev_sha": BASE_DEV_SHA,
        "candidate_sha": CANDIDATE_SHA,
    }
    values.update(overrides)
    return CandidateReportRequest.model_validate(values)


def test_service_records_candidate_and_evaluating_event_atomically(db_session: Session) -> None:
    """RUNNING 행은 candidate SHA·EVALUATING event를 같은 commit으로 남긴다."""
    experiment = _running_experiment(db_session)

    updated = record_candidate(db_session, experiment.id, _request(experiment.id))

    assert updated.candidate_sha == CANDIDATE_SHA
    assert updated.status == ExperimentStatus.EVALUATING.value
    evaluating_events = db_session.scalars(
        select(ExperimentEvent).where(
            ExperimentEvent.experiment_id == experiment.id,
            ExperimentEvent.to_status == ExperimentStatus.EVALUATING.value,
        )
    ).all()
    assert len(evaluating_events) == 1
    assert evaluating_events[0].idempotency_key == f"executor-candidate:{experiment.id}"


def test_service_same_candidate_fingerprint_is_idempotent_without_new_event(
    db_session: Session,
) -> None:
    """재시도는 이미 EVALUATING이어도 기존 candidate 보고를 성공으로 돌려준다."""
    experiment = _running_experiment(db_session)
    first = record_candidate(db_session, experiment.id, _request(experiment.id))
    retried = record_candidate(db_session, experiment.id, _request(experiment.id))

    assert retried.id == first.id
    assert db_session.scalar(
        select(func.count())
        .select_from(ExperimentEvent)
        .where(
            ExperimentEvent.experiment_id == experiment.id,
            ExperimentEvent.to_status == ExperimentStatus.EVALUATING.value,
        )
    ) == 1


def test_service_same_candidate_event_key_with_different_payload_conflicts(
    db_session: Session,
) -> None:
    """고정 candidate event key로 다른 SHA를 덮어쓰려는 재시도를 거부한다."""
    experiment = _running_experiment(db_session)
    record_candidate(db_session, experiment.id, _request(experiment.id))

    with pytest.raises(IdempotencyConflictError):
        record_candidate(
            db_session,
            experiment.id,
            _request(experiment.id, candidate_sha="c" * 40),
        )


def test_service_rejects_different_existing_candidate_sha(db_session: Session) -> None:
    """같은 실험에 이미 기록된 다른 candidate SHA는 바꾸지 못한다."""
    experiment = _running_experiment(db_session)
    experiment.candidate_sha = "c" * 40
    db_session.commit()

    with pytest.raises(CandidateConflictError):
        record_candidate(db_session, experiment.id, _request(experiment.id))


@pytest.mark.parametrize(
    "field,value",
    [
        ("issue_number", ISSUE_NUMBER + 1),
        ("issue_branch", "exp/557-another-branch"),
        ("base_dev_sha", "c" * 40),
    ],
)
def test_service_rejects_candidate_coordinates_that_do_not_match_experiment(
    db_session: Session,
    field: str,
    value: object,
) -> None:
    """발행 때 봉인한 이슈·branch·baseline 좌표와 다른 보고를 거부한다."""
    experiment = _running_experiment(db_session)
    request_values = _request(experiment.id).model_dump()
    request_values[field] = value
    request = CandidateReportRequest.model_construct(**request_values)

    with pytest.raises(CandidateConflictError):
        record_candidate(db_session, experiment.id, request)


def test_service_rolls_back_candidate_and_status_when_event_flush_fails(
    db_session: Session,
) -> None:
    """EVALUATING event flush 실패 뒤 SHA나 상태만 남지 않는다."""
    experiment = _running_experiment(db_session)

    @event.listens_for(db_session, "before_flush")
    def fail_candidate_event(session: Session, _flush_context, _instances) -> None:
        if any(
            isinstance(row, ExperimentEvent)
            and row.idempotency_key == f"executor-candidate:{experiment.id}"
            for row in session.new
        ):
            raise RuntimeError("controlled candidate event failure")

    with pytest.raises(RuntimeError, match="controlled candidate event failure"):
        record_candidate(db_session, experiment.id, _request(experiment.id))

    assert not db_session.in_transaction()
    persisted = db_session.get(Experiment, experiment.id)
    assert persisted is not None
    assert persisted.status == ExperimentStatus.RUNNING.value
    assert persisted.candidate_sha is None


@pytest.fixture
def executor_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """일반 API와 분리된 executor 토큰 경계를 SQLite에서 실행한다."""
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
        executor_api_token=EXECUTOR_TOKEN,
        github_token="x" * 40,
        github_repository="SKYAHO/Autoresearch",
        gh_timeout_sec=30,
        issue_daily_limit=20,
    )
    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(main_module, "ensure_schema", lambda *_args: None)
    monkeypatch.setattr(main_module, "create_database_engine", lambda *_args: engine)

    app = main_module.create_app()
    with TestClient(app) as client:
        yield client
    Base.metadata.drop_all(engine)
    engine.dispose()


def _create_running_experiment_for_http(client: TestClient) -> Experiment:
    """HTTP candidate 보고 전 필요한 봉인 좌표와 RUNNING 상태를 직접 준비한다."""
    factory = client.app.state.experiment_session_factory
    with factory() as session:
        experiment = _running_experiment(session)
        experiment_id = experiment.id
    with factory() as session:
        persisted = session.get(Experiment, experiment_id)
        assert persisted is not None
        session.expunge(persisted)
        return persisted


def _http_payload(experiment_id: uuid.UUID, **overrides: object) -> dict[str, object]:
    """executor HTTP 요청의 기본 payload를 반환한다."""
    payload: dict[str, object] = {
        "idempotency_key": f"executor-candidate:{experiment_id}",
        "issue_number": ISSUE_NUMBER,
        "issue_branch": ISSUE_BRANCH,
        "base_dev_sha": BASE_DEV_SHA,
        "candidate_sha": CANDIDATE_SHA,
    }
    payload.update(overrides)
    return payload


def test_executor_candidate_endpoint_requires_dedicated_token(
    executor_client: TestClient,
) -> None:
    """정확한 executor 토큰만 candidate SHA 저장과 평가 전이를 허용한다."""
    experiment = _create_running_experiment_for_http(executor_client)
    path = f"/internal/executor/experiments/{experiment.id}/candidate"

    missing = executor_client.post(path, json=_http_payload(experiment.id))
    general_token = executor_client.post(
        path,
        headers={"X-Orch-Token": API_TOKEN},
        json=_http_payload(experiment.id),
    )
    invalid_token = executor_client.post(
        path,
        headers={"X-Orch-Executor-Token": "wrong"},
        json=_http_payload(experiment.id),
    )
    response = executor_client.post(
        path,
        headers=EXECUTOR_HEADERS,
        json=_http_payload(experiment.id),
    )

    assert missing.status_code == 401
    assert general_token.status_code == 401
    assert invalid_token.status_code == 401
    assert response.status_code == 200
    assert response.json()["candidate_sha"] == CANDIDATE_SHA
    assert response.json()["status"] == "EVALUATING"


def test_executor_candidate_endpoint_maps_sealed_coordinate_conflict_to_409(
    executor_client: TestClient,
) -> None:
    """유효 형식이지만 봉인된 branch와 다른 candidate 보고는 409다."""
    experiment = _create_running_experiment_for_http(executor_client)

    response = executor_client.post(
        f"/internal/executor/experiments/{experiment.id}/candidate",
        headers=EXECUTOR_HEADERS,
        json=_http_payload(experiment.id, issue_branch="exp/557-other-branch"),
    )

    assert response.status_code == 409


@pytest.mark.parametrize(
    "field,value",
    [
        ("candidate_sha", "B" * 40),
        ("candidate_sha", "b" * 39),
        ("candidate_sha", "b" * 41),
        ("issue_number", 558),
        ("extra_field", True),
    ],
)
def test_executor_candidate_endpoint_rejects_invalid_requests_with_422(
    executor_client: TestClient,
    field: str,
    value: object,
) -> None:
    """SHA 형식·branch 이슈 번호·extra field를 service 전에 요청 검증으로 막는다."""
    experiment = _create_running_experiment_for_http(executor_client)

    payload = _http_payload(experiment.id, **{field: value})
    response = executor_client.post(
        f"/internal/executor/experiments/{experiment.id}/candidate",
        headers=EXECUTOR_HEADERS,
        json=payload,
    )

    assert response.status_code == 422


def test_service_rejects_candidate_report_with_noncanonical_idempotency_key(
    db_session: Session,
) -> None:
    """요청 key가 experiment별 고정 candidate event key와 다르면 저장하지 않는다."""
    experiment = _running_experiment(db_session)

    with pytest.raises(CandidateConflictError):
        record_candidate(
            db_session,
            experiment.id,
            _request(experiment.id, idempotency_key="executor-report-0001"),
        )

    persisted = db_session.get(Experiment, experiment.id)
    assert persisted is not None
    assert persisted.status == ExperimentStatus.RUNNING.value
    assert persisted.candidate_sha is None


def test_executor_candidate_endpoint_rejects_noncanonical_idempotency_key_with_409(
    executor_client: TestClient,
) -> None:
    """HTTP도 executor-candidate 실험별 고정 key 이외의 보고를 충돌로 돌려준다."""
    experiment = _create_running_experiment_for_http(executor_client)

    response = executor_client.post(
        f"/internal/executor/experiments/{experiment.id}/candidate",
        headers=EXECUTOR_HEADERS,
        json=_http_payload(experiment.id, idempotency_key="executor-report-0001"),
    )

    assert response.status_code == 409


METRIC_SNAPSHOT: dict[str, object] = {
    "contract_version": "experiment-metric-snapshot-v1",
    "primary_metric": "roc_auc",
    "baseline_mean": 0.7412,
    "candidate_mean": 0.7439,
    "paired_delta_mean": 0.0027,
    "seeds": [11, 12, 13],
    "split_matches": True,
}


def _evaluating_experiment(session: Session) -> Experiment:
    """candidate 보고까지 끝나 결과 보고만 남은 EVALUATING Experiment를 만든다."""
    experiment = _running_experiment(session)
    record_candidate(session, experiment.id, _request(experiment.id))
    return experiment


def _result_request(
    experiment_id: uuid.UUID, **overrides: object
) -> ExecutorResultReportRequest:
    """유효한 결과 보고 요청에 필요한 한 필드만 바꿔 만든다."""
    values: dict[str, object] = {
        "idempotency_key": f"executor-result:{experiment_id}",
        "candidate_sha": CANDIDATE_SHA,
        "metric_snapshot": METRIC_SNAPSHOT,
    }
    values.update(overrides)
    return ExecutorResultReportRequest.model_validate(values)


def test_service_records_result_metrics_and_passed_event_atomically(
    db_session: Session,
) -> None:
    """EVALUATING 행은 지표와 PASSED event를 같은 commit으로 남긴다."""
    experiment = _evaluating_experiment(db_session)

    updated = record_experiment_result(db_session, experiment.id, _result_request(experiment.id))

    assert updated.status == ExperimentStatus.PASSED.value
    assert updated.metric_summary == METRIC_SNAPSHOT
    passed_events = db_session.scalars(
        select(ExperimentEvent).where(
            ExperimentEvent.experiment_id == experiment.id,
            ExperimentEvent.to_status == ExperimentStatus.PASSED.value,
        )
    ).all()
    assert len(passed_events) == 1
    assert passed_events[0].idempotency_key == f"executor-result:{experiment.id}"
    # 지표는 실험 행뿐 아니라 event에도 남아야 한다 — 나중에 어느 시점의 숫자였는지
    # 타임라인에서 되짚을 수 있어야 한다.
    assert passed_events[0].metric_snapshot == METRIC_SNAPSHOT


def test_service_same_result_report_is_idempotent_without_new_event(
    db_session: Session,
) -> None:
    """같은 지표 재시도는 이미 PASSED여도 성공으로 돌려주고 event를 늘리지 않는다."""
    experiment = _evaluating_experiment(db_session)
    record_experiment_result(db_session, experiment.id, _result_request(experiment.id))

    record_experiment_result(db_session, experiment.id, _result_request(experiment.id))

    assert db_session.scalar(
        select(func.count())
        .select_from(ExperimentEvent)
        .where(
            ExperimentEvent.experiment_id == experiment.id,
            ExperimentEvent.to_status == ExperimentStatus.PASSED.value,
        )
    ) == 1


def test_service_rejects_a_second_result_with_different_metrics(
    db_session: Session,
) -> None:
    """한 실험의 결과는 하나다 — 다른 숫자로 덮어쓰려는 보고를 거부한다."""
    experiment = _evaluating_experiment(db_session)
    record_experiment_result(db_session, experiment.id, _result_request(experiment.id))

    with pytest.raises(IdempotencyConflictError):
        record_experiment_result(
            db_session,
            experiment.id,
            _result_request(
                experiment.id,
                metric_snapshot={**METRIC_SNAPSHOT, "paired_delta_mean": 0.9},
            ),
        )


def test_service_rejects_result_for_a_different_candidate_sha(db_session: Session) -> None:
    """다른 실행의 산출물을 이 실험의 결과로 받지 않는다."""
    experiment = _evaluating_experiment(db_session)

    with pytest.raises(CandidateConflictError):
        record_experiment_result(
            db_session,
            experiment.id,
            _result_request(experiment.id, candidate_sha="c" * 40),
        )


def test_service_rejects_result_before_candidate_report(db_session: Session) -> None:
    """candidate 보고 없이 결과부터 오면 순서가 어긋난 것이라 받지 않는다."""
    experiment = _running_experiment(db_session)

    with pytest.raises(CandidateConflictError):
        record_experiment_result(db_session, experiment.id, _result_request(experiment.id))

    persisted = db_session.get(Experiment, experiment.id)
    assert persisted is not None
    assert persisted.status == ExperimentStatus.RUNNING.value
    assert persisted.metric_summary is None


def test_service_rejects_result_report_with_noncanonical_idempotency_key(
    db_session: Session,
) -> None:
    """요청 key가 experiment별 고정 result event key와 다르면 저장하지 않는다."""
    experiment = _evaluating_experiment(db_session)

    with pytest.raises(CandidateConflictError):
        record_experiment_result(
            db_session,
            experiment.id,
            _result_request(experiment.id, idempotency_key="executor-result-0001"),
        )

    persisted = db_session.get(Experiment, experiment.id)
    assert persisted is not None
    assert persisted.status == ExperimentStatus.EVALUATING.value


def test_result_request_rejects_empty_metric_snapshot() -> None:
    """빈 요약을 받으면 "측정했다"로 오인된다."""
    with pytest.raises(ValidationError):
        _result_request(uuid.uuid4(), metric_snapshot={})


def test_result_request_rejects_metric_snapshot_over_the_size_limit() -> None:
    """전문은 GCS에 있다 — 요약이 상한을 넘으면 요청 단계에서 막는다."""
    oversized = {"note": "x" * (MAX_METRIC_SNAPSHOT_BYTES + 1)}

    with pytest.raises(ValidationError):
        _result_request(uuid.uuid4(), metric_snapshot=oversized)


def _create_evaluating_experiment_for_http(client: TestClient) -> Experiment:
    """HTTP 결과 보고 전 필요한 candidate 보고까지 마친 행을 준비한다."""
    factory = client.app.state.experiment_session_factory
    with factory() as session:
        experiment = _evaluating_experiment(session)
        experiment_id = experiment.id
    with factory() as session:
        persisted = session.get(Experiment, experiment_id)
        assert persisted is not None
        session.expunge(persisted)
        return persisted


def _http_result_payload(experiment_id: uuid.UUID, **overrides: object) -> dict[str, object]:
    """executor 결과 보고 HTTP 요청의 기본 payload를 반환한다."""
    payload: dict[str, object] = {
        "idempotency_key": f"executor-result:{experiment_id}",
        "candidate_sha": CANDIDATE_SHA,
        "metric_snapshot": METRIC_SNAPSHOT,
    }
    payload.update(overrides)
    return payload


def test_executor_result_endpoint_requires_dedicated_token(
    executor_client: TestClient,
) -> None:
    """정확한 executor 토큰만 지표 저장과 완주 전이를 허용한다."""
    experiment = _create_evaluating_experiment_for_http(executor_client)
    path = f"/internal/executor/experiments/{experiment.id}/result"

    missing = executor_client.post(path, json=_http_result_payload(experiment.id))
    general_token = executor_client.post(
        path,
        headers={"X-Orch-Token": API_TOKEN},
        json=_http_result_payload(experiment.id),
    )
    invalid_token = executor_client.post(
        path,
        headers={"X-Orch-Executor-Token": "wrong"},
        json=_http_result_payload(experiment.id),
    )
    response = executor_client.post(
        path,
        headers=EXECUTOR_HEADERS,
        json=_http_result_payload(experiment.id),
    )

    assert missing.status_code == 401
    assert general_token.status_code == 401
    assert invalid_token.status_code == 401
    assert response.status_code == 200
    assert response.json()["status"] == "PASSED"
    assert response.json()["metric_summary"] == METRIC_SNAPSHOT


def test_executor_result_endpoint_maps_out_of_order_report_to_409(
    executor_client: TestClient,
) -> None:
    """candidate 보고 전에 도착한 결과 보고는 409다."""
    experiment = _create_running_experiment_for_http(executor_client)

    response = executor_client.post(
        f"/internal/executor/experiments/{experiment.id}/result",
        headers=EXECUTOR_HEADERS,
        json=_http_result_payload(experiment.id),
    )

    assert response.status_code == 409


@pytest.mark.parametrize(
    "field,value",
    [
        ("candidate_sha", "B" * 40),
        ("candidate_sha", "b" * 39),
        ("metric_snapshot", {}),
        ("metric_snapshot", []),
        ("extra_field", True),
    ],
)
def test_executor_result_endpoint_rejects_invalid_requests_with_422(
    executor_client: TestClient,
    field: str,
    value: object,
) -> None:
    """SHA 형식·빈 요약·extra field를 service 전에 요청 검증으로 막는다."""
    experiment = _create_evaluating_experiment_for_http(executor_client)

    response = executor_client.post(
        f"/internal/executor/experiments/{experiment.id}/result",
        headers=EXECUTOR_HEADERS,
        json=_http_result_payload(experiment.id, **{field: value}),
    )

    assert response.status_code == 422
