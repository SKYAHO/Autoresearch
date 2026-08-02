"""PostgreSQL에서만 보장되는 실험 워크벤치 동시성 계약을 검증한다.

전체 파이프라인 중 Agent Orchestration 실험 API의 row lock과 멱등성 unique
constraint 복구를 실제 PostgreSQL transaction으로 검증한다. 컨테이너 수명주기와
Alembic 적용 자체는 검증 명령이 담당하며, 이 모듈은 Compose 자원을 관리하지 않는다.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import uuid

import pytest
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from agent_orchestration.app.experiments.exceptions import IdempotencyConflictError
from agent_orchestration.app.database import create_database_engine
from agent_orchestration.app.experiments.models import Experiment, ExperimentLog
from agent_orchestration.app.experiments.schemas import (
    ExperimentCreate,
    ExperimentLogCreate,
    ExperimentLogResponse,
    StatusUpdateRequest,
)
from agent_orchestration.app.experiments.models import ExperimentStatus
from agent_orchestration.app.experiments.service import (
    create_experiment,
    create_experiment_log,
    update_experiment_status,
)


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    """명시적으로 제공된 일회성 PostgreSQL URL에만 연결한다."""
    database_url = os.getenv("ORCH_TEST_POSTGRES_URL")
    if not database_url:
        pytest.skip("ORCH_TEST_POSTGRES_URL is required for PostgreSQL integration tests")
    engine = create_database_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def postgres_session_factory(postgres_engine: Engine) -> sessionmaker[Session]:
    """동시성 검증용 독립 Session factory를 제공한다."""
    return sessionmaker(bind=postgres_engine, expire_on_commit=False)


def test_select_for_update_blocks_second_independent_session_until_commit(
    postgres_engine: Engine,
    postgres_session_factory: sessionmaker[Session],
) -> None:
    """동일 experiment의 두 번째 상태 변경은 첫 row lock 해제 전 완료되지 않는다."""
    with postgres_session_factory() as setup_session:
        experiment_id = create_experiment(
            setup_session,
            ExperimentCreate(hypothesis="postgres row lock"),
        ).id

    first_lock_acquired = threading.Event()
    release_first_lock = threading.Event()
    second_lock_issued = threading.Event()
    lock_statement_count = 0
    counter_lock = threading.Lock()

    @event.listens_for(postgres_engine, "before_cursor_execute")
    def observe_for_update(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        nonlocal lock_statement_count
        if "FOR UPDATE" not in statement.upper():
            return
        with counter_lock:
            lock_statement_count += 1
            if lock_statement_count == 2:
                second_lock_issued.set()

    def hold_first_lock() -> None:
        with postgres_session_factory() as session, session.begin():
            session.scalar(
                select(Experiment)
                .where(Experiment.id == experiment_id)
                .with_for_update()
            )
            first_lock_acquired.set()
            assert release_first_lock.wait(timeout=5)

    def update_from_second_session() -> str:
        assert first_lock_acquired.wait(timeout=5)
        with postgres_session_factory() as session:
            return update_experiment_status(
                session,
                experiment_id,
                StatusUpdateRequest(status=ExperimentStatus.RUNNING),
            ).status

    executor = ThreadPoolExecutor(max_workers=2)
    try:
        first_future = executor.submit(hold_first_lock)
        second_future = executor.submit(update_from_second_session)
        assert second_lock_issued.wait(timeout=5)
        assert not second_future.done()
        release_first_lock.set()
        first_future.result(timeout=10)
        assert second_future.result(timeout=10) == ExperimentStatus.RUNNING.value
    finally:
        release_first_lock.set()
        event.remove(postgres_engine, "before_cursor_execute", observe_for_update)
        executor.shutdown(wait=True, cancel_futures=True)


def test_concurrent_same_log_request_returns_one_identical_resource_and_reusable_sessions(
    postgres_session_factory: sessionmaker[Session],
) -> None:
    """동일 key·payload 경쟁은 한 row와 동일 응답을 만들고 loser Session도 복구한다."""
    with postgres_session_factory() as setup_session:
        experiment_id = create_experiment(
            setup_session,
            ExperimentCreate(hypothesis="postgres same log retry"),
        ).id
    request = ExperimentLogCreate(
        idempotency_key=f"same-{uuid.uuid4()}",
        log_type="stdout",
        content="same payload",
    )
    flush_barrier = threading.Barrier(2)

    @event.listens_for(Session, "before_flush")
    def synchronize_log_flush(session: Session, _flush_context, _instances) -> None:
        if any(
            isinstance(row, ExperimentLog)
            and row.idempotency_key == request.idempotency_key
            for row in session.new
        ):
            flush_barrier.wait(timeout=5)

    def submit() -> dict:
        with postgres_session_factory() as session:
            row = create_experiment_log(session, experiment_id, request)
            assert not session.in_transaction()
            with session.begin():
                assert session.scalar(select(1)) == 1
            return ExperimentLogResponse.model_validate(row).model_dump()

    executor = ThreadPoolExecutor(max_workers=2)
    try:
        futures = [executor.submit(submit) for _index in range(2)]
        responses = [future.result(timeout=10) for future in futures]
    finally:
        event.remove(Session, "before_flush", synchronize_log_flush)
        executor.shutdown(wait=True, cancel_futures=True)

    with postgres_session_factory() as session:
        row_count = session.scalar(
            select(func.count())
            .select_from(ExperimentLog)
            .where(ExperimentLog.idempotency_key == request.idempotency_key)
        )
    assert row_count == 1
    assert responses[0]["id"] == responses[1]["id"]
    assert responses[0] == responses[1]


def test_concurrent_conflicting_log_request_has_one_success_and_no_loser_side_effect(
    postgres_session_factory: sessionmaker[Session],
) -> None:
    """동일 key·다른 payload 경쟁은 정확히 하나만 저장하고 loser를 원자적으로 롤백한다."""
    with postgres_session_factory() as setup_session:
        experiment_id = create_experiment(
            setup_session,
            ExperimentCreate(hypothesis="postgres conflicting log retry"),
        ).id
    idempotency_key = f"conflict-{uuid.uuid4()}"
    requests = [
        ExperimentLogCreate(
            idempotency_key=idempotency_key,
            log_type="stdout",
            content="payload-a",
        ),
        ExperimentLogCreate(
            idempotency_key=idempotency_key,
            log_type="stderr",
            content="payload-b",
        ),
    ]
    flush_barrier = threading.Barrier(2)

    @event.listens_for(Session, "before_flush")
    def synchronize_log_flush(session: Session, _flush_context, _instances) -> None:
        if any(
            isinstance(row, ExperimentLog)
            and row.idempotency_key == idempotency_key
            for row in session.new
        ):
            flush_barrier.wait(timeout=5)

    def submit(request: ExperimentLogCreate) -> tuple[str, str]:
        with postgres_session_factory() as session:
            try:
                row = create_experiment_log(session, experiment_id, request)
                outcome = ("success", row.content)
            except IdempotencyConflictError:
                outcome = ("conflict", request.content)
            assert not session.in_transaction()
            with session.begin():
                assert session.scalar(select(1)) == 1
            return outcome

    executor = ThreadPoolExecutor(max_workers=2)
    try:
        futures = [executor.submit(submit, request) for request in requests]
        outcomes = [future.result(timeout=10) for future in futures]
    finally:
        event.remove(Session, "before_flush", synchronize_log_flush)
        executor.shutdown(wait=True, cancel_futures=True)

    with postgres_session_factory() as session:
        rows = session.scalars(
            select(ExperimentLog).where(
                ExperimentLog.experiment_id == experiment_id,
                ExperimentLog.idempotency_key == idempotency_key,
            )
        ).all()
    successes = [content for status, content in outcomes if status == "success"]
    conflicts = [content for status, content in outcomes if status == "conflict"]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert len(rows) == 1
    assert rows[0].content == successes[0]
    assert rows[0].content != conflicts[0]
