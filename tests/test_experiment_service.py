"""실험 워크벤치 service의 생성·조회·상태 쓰기 계약을 검증한다.

전체 파이프라인에서 FastAPI 입력이 SQLAlchemy transaction을 통해 실험, event, log,
metadata로 일관되게 저장되는 경계를 담당한다. HTTP 인증과 실제 PostgreSQL migration은
이 모듈의 검증 범위가 아니다.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
import threading
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from agent_orchestration.app.database import Base
from agent_orchestration.app.experiments.exceptions import ExperimentNotFoundError
from agent_orchestration.app.experiments.exceptions import (
    IdempotencyConflictError,
    PromotionRequiresDedicatedEndpointError,
)
from agent_orchestration.app.experiments.models import (
    Experiment,
    ExperimentEvent,
    ExperimentLog,
    ExperimentMetadata,
    ExperimentStatus,
)
from agent_orchestration.app.experiments.schemas import (
    ExperimentCreate,
    ExperimentEventCreate,
    ExperimentLogCreate,
    ExperimentLogListQuery,
    StatusUpdateRequest,
)
from agent_orchestration.app.experiments.service import (
    create_experiment_event,
    create_experiment,
    create_experiment_log,
    get_experiment,
    get_experiment_metadata,
    list_experiments,
    list_experiment_logs,
    update_experiment_status,
)
from agent_orchestration.app.experiments.transition_service import InvalidTransitionError


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    """PostgreSQL server UUID 함수를 재현하는 in-memory SQLAlchemy engine."""
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


def test_create_experiment_persists_metadata_and_initial_event_atomically(
    db_session: Session,
) -> None:
    """실험만 저장되고 CREATED event나 metadata가 빠지는 회귀를 잡는다."""
    experiment = create_experiment(
        db_session,
        ExperimentCreate(
            hypothesis="새 추천 피처가 CTR을 높인다",
            agent_session_id="session-448",
            metadata={"branch": "feat/448", "model": "lightgbm"},
        ),
    )

    assert experiment.id is not None
    assert experiment.status == ExperimentStatus.CREATED.value
    assert get_experiment_metadata(db_session, experiment.id) == {
        "branch": "feat/448",
        "model": "lightgbm",
    }
    events = db_session.scalars(
        select(ExperimentEvent).where(ExperimentEvent.experiment_id == experiment.id)
    ).all()
    assert len(events) == 1
    assert events[0].from_status is None
    assert events[0].to_status == ExperimentStatus.CREATED.value
    assert events[0].idempotency_key == f"experiment-created:{experiment.id}"
    assert len(events[0].request_fingerprint) == 64


def test_list_experiments_filters_status_and_returns_total_before_pagination(
    db_session: Session,
) -> None:
    """status filter나 offset 적용이 total 계산을 훼손하는 회귀를 잡는다."""
    first = create_experiment(db_session, ExperimentCreate(hypothesis="first"))
    second = create_experiment(db_session, ExperimentCreate(hypothesis="second"))
    third = create_experiment(db_session, ExperimentCreate(hypothesis="third"))
    first.status = ExperimentStatus.RUNNING.value
    second.status = ExperimentStatus.RUNNING.value
    db_session.commit()

    page = list_experiments(
        db_session,
        limit=1,
        offset=1,
        status=ExperimentStatus.RUNNING,
    )

    assert page.total == 2
    assert len(page.items) == 1
    assert page.items[0].id in {first.id, second.id}
    assert page.items[0].id != third.id


def test_get_experiment_raises_domain_not_found_error(db_session: Session) -> None:
    """없는 UUID가 None으로 흘러 router에서 500이 되는 회귀를 잡는다."""
    missing_id = uuid.UUID("00000000-0000-0000-0000-000000000448")

    with pytest.raises(ExperimentNotFoundError) as error:
        get_experiment(db_session, missing_id)

    assert error.value.experiment_id == missing_id


def test_create_experiment_rolls_back_all_rows_when_metadata_insert_fails(
    db_session: Session,
) -> None:
    """metadata 저장 실패 뒤 experiment나 최초 event만 남는 회귀를 잡는다."""
    @event.listens_for(db_session, "before_flush")
    def fail_metadata_insert(session: Session, _flush_context, _instances) -> None:
        if any(isinstance(row, ExperimentMetadata) for row in session.new):
            raise RuntimeError("controlled metadata failure")

    with pytest.raises(RuntimeError, match="controlled metadata failure"):
        create_experiment(
            db_session,
            ExperimentCreate(hypothesis="rollback", metadata={"branch": "feat/448"}),
        )

    assert db_session.scalar(select(func.count()).select_from(Experiment)) == 0
    assert db_session.scalar(select(func.count()).select_from(ExperimentEvent)) == 0
    assert db_session.scalar(select(func.count()).select_from(ExperimentMetadata)) == 0


def test_status_update_allows_evaluating_to_error_and_records_each_event(
    db_session: Session,
) -> None:
    """평가 과정의 시스템 오류가 FAILED로 섞이거나 전이 불가가 되는 회귀를 잡는다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="evaluation error"))

    update_experiment_status(
        db_session,
        experiment.id,
        StatusUpdateRequest(status=ExperimentStatus.RUNNING),
    )
    update_experiment_status(
        db_session,
        experiment.id,
        StatusUpdateRequest(status=ExperimentStatus.EVALUATING),
    )
    updated = update_experiment_status(
        db_session,
        experiment.id,
        StatusUpdateRequest(
            status=ExperimentStatus.ERROR,
            reason="statistics worker crashed",
        ),
    )

    assert updated.status == ExperimentStatus.ERROR.value
    events = db_session.scalars(
        select(ExperimentEvent).where(ExperimentEvent.experiment_id == experiment.id)
    ).all()
    assert {(event.from_status, event.to_status) for event in events} == {
        (None, ExperimentStatus.CREATED.value),
        (ExperimentStatus.CREATED.value, ExperimentStatus.RUNNING.value),
        (ExperimentStatus.RUNNING.value, ExperimentStatus.EVALUATING.value),
        (ExperimentStatus.EVALUATING.value, ExperimentStatus.ERROR.value),
    }


def test_invalid_status_update_rolls_back_without_new_event(db_session: Session) -> None:
    """거부된 단계 건너뛰기가 상태나 event 일부를 남기는 회귀를 잡는다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="invalid transition"))

    with pytest.raises(InvalidTransitionError):
        update_experiment_status(
            db_session,
            experiment.id,
            StatusUpdateRequest(status=ExperimentStatus.PASSED),
        )

    db_session.refresh(experiment)
    assert experiment.status == ExperimentStatus.CREATED.value
    assert db_session.scalar(
        select(func.count())
        .select_from(ExperimentEvent)
        .where(ExperimentEvent.experiment_id == experiment.id)
    ) == 1


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (ExperimentStatus.CREATED, ExperimentStatus.CREATED),
        (ExperimentStatus.RUNNING, ExperimentStatus.CREATED),
        (ExperimentStatus.CREATED, ExperimentStatus.PASSED),
        (ExperimentStatus.FAILED, ExperimentStatus.RUNNING),
        (ExperimentStatus.ERROR, ExperimentStatus.RUNNING),
        (ExperimentStatus.PROMOTED, ExperimentStatus.RUNNING),
    ],
)
def test_invalid_status_update_preserves_state_and_event_atomicity(
    db_session: Session,
    current: ExperimentStatus,
    requested: ExperimentStatus,
) -> None:
    """잘못된 서비스 전이가 상태·event를 바꾸거나 Session을 망가뜨리지 않는다."""
    experiment = create_experiment(
        db_session,
        ExperimentCreate(hypothesis=f"invalid {current.value} to {requested.value}"),
    )
    experiment.status = current.value
    db_session.commit()
    experiment_id = experiment.id
    event_count_before = db_session.scalar(
        select(func.count())
        .select_from(ExperimentEvent)
        .where(ExperimentEvent.experiment_id == experiment_id)
    )
    db_session.rollback()
    request = StatusUpdateRequest.model_construct(status=requested)

    with pytest.raises(InvalidTransitionError):
        update_experiment_status(db_session, experiment_id, request)

    assert not db_session.in_transaction()
    persisted = db_session.get(Experiment, experiment_id)
    assert persisted is not None
    assert persisted.status == current.value
    assert db_session.scalar(
        select(func.count())
        .select_from(ExperimentEvent)
        .where(ExperimentEvent.experiment_id == experiment_id)
    ) == event_count_before


def test_event_retry_with_same_key_and_payload_returns_original_event(
    db_session: Session,
) -> None:
    """네트워크 재시도가 상태 전이 event를 중복 저장하는 회귀를 잡는다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="idempotent event"))
    request = ExperimentEventCreate(
        idempotency_key="runner-event-0001",
        to_status=ExperimentStatus.RUNNING,
        reason="runner started",
        metric_snapshot={"attempt": 1},
    )

    first = create_experiment_event(db_session, experiment.id, request)
    retried = create_experiment_event(db_session, experiment.id, request)

    assert retried.id == first.id
    assert db_session.scalar(
        select(func.count())
        .select_from(ExperimentEvent)
        .where(ExperimentEvent.idempotency_key == request.idempotency_key)
    ) == 1


def test_event_retry_with_same_key_and_different_payload_returns_conflict(
    db_session: Session,
) -> None:
    """같은 key의 다른 payload가 기존 성공으로 오인되는 회귀를 잡는다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="conflicting event"))
    create_experiment_event(
        db_session,
        experiment.id,
        ExperimentEventCreate(
            idempotency_key="runner-event-0002",
            to_status=ExperimentStatus.RUNNING,
            reason="first payload",
        ),
    )

    with pytest.raises(IdempotencyConflictError):
        create_experiment_event(
            db_session,
            experiment.id,
            ExperimentEventCreate(
                idempotency_key="runner-event-0002",
                to_status=ExperimentStatus.ERROR,
                reason="different payload",
            ),
        )


def test_metric_snapshot_updates_experiment_summary_in_same_transition(
    db_session: Session,
) -> None:
    """event 지표와 실험 최신 지표가 서로 달라지는 회귀를 잡는다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="metric snapshot"))
    snapshot = {"roc_auc": 0.81, "p_value": 0.03}

    event_row = create_experiment_event(
        db_session,
        experiment.id,
        ExperimentEventCreate(
            idempotency_key="runner-event-0003",
            to_status=ExperimentStatus.RUNNING,
            metric_snapshot=snapshot,
        ),
    )

    assert event_row.metric_snapshot == snapshot
    assert get_experiment(db_session, experiment.id).metric_summary == snapshot


@pytest.mark.parametrize("request_kind", ["status", "event"])
def test_general_transition_services_reject_promoted_even_if_schema_is_bypassed(
    db_session: Session,
    request_kind: str,
) -> None:
    """내부 호출이 Pydantic을 우회해 일반 경로로 PROMOTED를 여는 회귀를 잡는다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="promotion bypass"))
    experiment.status = ExperimentStatus.PASSED.value
    db_session.commit()

    with pytest.raises(PromotionRequiresDedicatedEndpointError):
        if request_kind == "status":
            request = StatusUpdateRequest.model_construct(status=ExperimentStatus.PROMOTED)
            update_experiment_status(db_session, experiment.id, request)
        else:
            request = ExperimentEventCreate.model_construct(
                idempotency_key="forbidden-promotion",
                to_status=ExperimentStatus.PROMOTED,
                reason=None,
                metric_snapshot=None,
            )
            create_experiment_event(db_session, experiment.id, request)


def test_concurrent_event_retries_return_one_persisted_event(tmp_path: Path) -> None:
    """동시 재시도 중 unique constraint 패자가 500으로 노출되는 회귀를 잡는다."""
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'concurrent.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )

    @event.listens_for(engine, "connect")
    def register_uuid_function(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        experiment = create_experiment(session, ExperimentCreate(hypothesis="concurrent retry"))
        experiment_id = experiment.id

    request = ExperimentEventCreate(
        idempotency_key="runner-event-concurrent",
        to_status=ExperimentStatus.RUNNING,
        reason="same payload",
    )
    flush_barrier = threading.Barrier(2)

    @event.listens_for(Session, "before_flush")
    def synchronize_event_flush(session: Session, _flush_context, _instances) -> None:
        if any(
            isinstance(row, ExperimentEvent)
            and row.idempotency_key == request.idempotency_key
            for row in session.new
        ):
            flush_barrier.wait(timeout=5)

    def submit_event() -> uuid.UUID:
        with factory() as session:
            return create_experiment_event(session, experiment_id, request).id

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            event_ids = list(executor.map(lambda _index: submit_event(), range(2)))
    finally:
        event.remove(Session, "before_flush", synchronize_event_flush)

    with factory() as session:
        count = session.scalar(
            select(func.count())
            .select_from(ExperimentEvent)
            .where(ExperimentEvent.idempotency_key == request.idempotency_key)
        )
    Base.metadata.drop_all(engine)
    engine.dispose()

    assert len(set(event_ids)) == 1
    assert count == 1


def test_log_query_limit_policy_is_default_100_and_range_1_to_100() -> None:
    """polling 조회가 승인된 기본값과 상한을 벗어나지 않는다."""
    assert ExperimentLogListQuery().limit == 100
    assert ExperimentLogListQuery(limit=1).limit == 1
    assert ExperimentLogListQuery(limit=100).limit == 100
    with pytest.raises(ValidationError):
        ExperimentLogListQuery(limit=0)
    with pytest.raises(ValidationError):
        ExperimentLogListQuery(limit=101)


@pytest.mark.parametrize(
    "terminal_status",
    [ExperimentStatus.FAILED, ExperimentStatus.ERROR, ExperimentStatus.PROMOTED],
)
def test_log_append_is_allowed_in_terminal_status(
    db_session: Session,
    terminal_status: ExperimentStatus,
) -> None:
    """종료된 실험의 마지막 출력도 상태 변경 없이 보존한다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="terminal log"))
    experiment.status = terminal_status.value
    db_session.commit()

    row = create_experiment_log(
        db_session,
        experiment.id,
        ExperimentLogCreate(
            idempotency_key=f"terminal-{terminal_status.value}",
            log_type="stderr",
            content="worker stopped",
        ),
    )

    assert row.content == "worker stopped"
    assert get_experiment(db_session, experiment.id).status == terminal_status.value


def test_log_retry_with_same_key_and_payload_returns_identical_resource(
    db_session: Session,
) -> None:
    """동일 Log 재시도가 row나 응답 resource를 중복 생성하지 않는다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="log retry"))
    request = ExperimentLogCreate(
        idempotency_key="log-retry-0001",
        log_type="stdout",
        content="epoch=1",
    )

    first = create_experiment_log(db_session, experiment.id, request)
    retried = create_experiment_log(db_session, experiment.id, request)

    assert retried.id == first.id
    assert retried.experiment_id == first.experiment_id
    assert retried.idempotency_key == first.idempotency_key
    assert retried.log_type == first.log_type
    assert retried.content == first.content
    assert db_session.scalar(
        select(func.count())
        .select_from(ExperimentLog)
        .where(ExperimentLog.idempotency_key == request.idempotency_key)
    ) == 1


def test_log_retry_with_same_key_and_different_payload_is_atomic(
    db_session: Session,
) -> None:
    """충돌 요청이 원본 Log를 바꾸지 않고 Session rollback 뒤 재사용 가능하다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="log conflict"))
    experiment_id = experiment.id
    create_experiment_log(
        db_session,
        experiment_id,
        ExperimentLogCreate(
            idempotency_key="log-conflict-0001",
            log_type="stdout",
            content="original",
        ),
    )

    with pytest.raises(IdempotencyConflictError):
        create_experiment_log(
            db_session,
            experiment_id,
            ExperimentLogCreate(
                idempotency_key="log-conflict-0001",
                log_type="stderr",
                content="different",
            ),
        )

    assert not db_session.in_transaction()
    rows = db_session.scalars(
        select(ExperimentLog).where(ExperimentLog.experiment_id == experiment_id)
    ).all()
    assert [(row.log_type, row.content) for row in rows] == [("stdout", "original")]


def test_log_polling_orders_by_created_at_then_id_and_advances_after_cursor(
    db_session: Session,
) -> None:
    """동일 timestamp에서도 UUID tie-breaker로 polling 누락과 중복을 막는다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="log cursor"))
    rows = [
        create_experiment_log(
            db_session,
            experiment.id,
            ExperimentLogCreate(
                idempotency_key=f"cursor-{index}",
                log_type="stdout" if index != 2 else "metric",
                content=f"line-{index}",
            ),
        )
        for index in range(3)
    ]
    tied_at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    for row in rows:
        row.created_at = tied_at
    db_session.commit()
    expected = sorted(rows, key=lambda row: row.id)

    first_page = list_experiment_logs(db_session, experiment.id, limit=2)
    second_page = list_experiment_logs(
        db_session,
        experiment.id,
        limit=2,
        after_id=first_page.next_cursor,
    )
    stdout_only = list_experiment_logs(
        db_session,
        experiment.id,
        limit=100,
        log_type="stdout",
    )

    assert [row.id for row in first_page.items] == [row.id for row in expected[:2]]
    assert first_page.next_cursor == expected[1].id
    assert [row.id for row in second_page.items] == [expected[2].id]
    assert second_page.next_cursor == expected[2].id
    assert [row.content for row in stdout_only.items] == [
        row.content for row in expected if row.log_type == "stdout"
    ]
