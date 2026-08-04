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

from agent_orchestration.app.experiments.exceptions import (
    IdempotencyConflictError,
    StepAlreadyFinalizedError,
)
from agent_orchestration.app.database import create_database_engine
from agent_orchestration.app.experiments.models import (
    Experiment,
    ExperimentLog,
    ExperimentStep,
    StepKind,
    StepStatus,
)
from agent_orchestration.app.experiments.schemas import (
    ExperimentCreate,
    ExperimentLogCreate,
    ExperimentLogResponse,
    ExperimentStepCreate,
    ExperimentStepResponse,
    ExperimentStepUpdate,
    StatusUpdateRequest,
)
from agent_orchestration.app.experiments.models import ExperimentStatus
from agent_orchestration.app.experiments.service import (
    create_experiment,
    create_experiment_log,
    create_experiment_step,
    update_experiment_step,
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


def test_concurrent_same_step_request_returns_one_identical_resource(
    postgres_session_factory: sessionmaker[Session],
) -> None:
    """동일 key·payload로 동시에 Step을 만들어도 row는 하나이고 응답이 같다."""
    with postgres_session_factory() as setup_session:
        experiment_id = create_experiment(
            setup_session,
            ExperimentCreate(hypothesis="postgres same step retry"),
        ).id
    request = ExperimentStepCreate(
        idempotency_key=f"same-step-{uuid.uuid4()}",
        step_kind=StepKind.FEATURE_ASSEMBLY,
        step_type="assemble_training_dataset",
        message="피처 2개 조립 중",
        target={"features": ["views_per_day", "like_ratio"]},
    )
    flush_barrier = threading.Barrier(2)

    @event.listens_for(Session, "before_flush")
    def synchronize_step_flush(session: Session, _flush_context, _instances) -> None:
        if any(
            isinstance(row, ExperimentStep)
            and row.idempotency_key == request.idempotency_key
            for row in session.new
        ):
            flush_barrier.wait(timeout=5)

    def submit() -> dict:
        with postgres_session_factory() as session:
            row = create_experiment_step(session, experiment_id, request)
            assert not session.in_transaction()
            with session.begin():
                assert session.scalar(select(1)) == 1
            return ExperimentStepResponse.model_validate(row).model_dump(mode="json")

    executor = ThreadPoolExecutor(max_workers=2)
    try:
        futures = [executor.submit(submit) for _index in range(2)]
        responses = [future.result(timeout=10) for future in futures]
    finally:
        event.remove(Session, "before_flush", synchronize_step_flush)
        executor.shutdown(wait=True, cancel_futures=True)

    with postgres_session_factory() as session:
        row_count = session.scalar(
            select(func.count())
            .select_from(ExperimentStep)
            .where(ExperimentStep.idempotency_key == request.idempotency_key)
        )
    assert row_count == 1
    assert responses[0]["id"] == responses[1]["id"]
    assert responses[0] == responses[1]


def test_concurrent_conflicting_step_request_has_one_success_and_no_loser_side_effect(
    postgres_session_factory: sessionmaker[Session],
) -> None:
    """동일 key·다른 payload 경쟁은 정확히 하나만 저장하고 loser를 원자적으로 롤백한다."""
    with postgres_session_factory() as setup_session:
        experiment_id = create_experiment(
            setup_session,
            ExperimentCreate(hypothesis="postgres conflicting step retry"),
        ).id
    idempotency_key = f"conflict-step-{uuid.uuid4()}"
    requests = [
        ExperimentStepCreate(
            idempotency_key=idempotency_key,
            step_kind=StepKind.FEATURE_ASSEMBLY,
            step_type="assemble_training_dataset",
        ),
        ExperimentStepCreate(
            idempotency_key=idempotency_key,
            step_kind=StepKind.TRAIN,
            step_type="train_candidate",
        ),
    ]
    flush_barrier = threading.Barrier(2)

    @event.listens_for(Session, "before_flush")
    def synchronize_step_flush(session: Session, _flush_context, _instances) -> None:
        if any(
            isinstance(row, ExperimentStep) and row.idempotency_key == idempotency_key
            for row in session.new
        ):
            flush_barrier.wait(timeout=5)

    def submit(request: ExperimentStepCreate) -> tuple[str, str]:
        with postgres_session_factory() as session:
            try:
                row = create_experiment_step(session, experiment_id, request)
                outcome = ("success", row.step_type)
            except IdempotencyConflictError:
                outcome = ("conflict", request.step_type)
            assert not session.in_transaction()
            with session.begin():
                assert session.scalar(select(1)) == 1
            return outcome

    executor = ThreadPoolExecutor(max_workers=2)
    try:
        futures = [executor.submit(submit, request) for request in requests]
        outcomes = [future.result(timeout=10) for future in futures]
    finally:
        event.remove(Session, "before_flush", synchronize_step_flush)
        executor.shutdown(wait=True, cancel_futures=True)

    with postgres_session_factory() as session:
        rows = session.scalars(
            select(ExperimentStep).where(
                ExperimentStep.experiment_id == experiment_id,
                ExperimentStep.idempotency_key == idempotency_key,
            )
        ).all()
    successes = [step_type for status, step_type in outcomes if status == "success"]
    conflicts = [step_type for status, step_type in outcomes if status == "conflict"]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert len(rows) == 1
    assert rows[0].step_type == successes[0]
    assert rows[0].step_type != conflicts[0]


def test_step_update_rereads_state_committed_by_another_session(
    postgres_session_factory: sessionmaker[Session],
) -> None:
    """확정 판정은 세션 캐시가 아니라 새 SELECT로 한다.

    session_a가 Step을 비터미널 상태로 들고 있는 동안 session_b가 COMPLETED를 커밋한다.
    session factory는 expire_on_commit=False라 session_a의 객체는 stale하게 남는데,
    그 stale 값으로 판정하면 확정된 결과를 조용히 덮어쓴다.
    """
    with postgres_session_factory() as setup_session:
        experiment_id = create_experiment(
            setup_session,
            ExperimentCreate(hypothesis="postgres step stale read"),
        ).id
        step_id = create_experiment_step(
            setup_session,
            experiment_id,
            ExperimentStepCreate(
                idempotency_key=f"stale-{uuid.uuid4()}",
                step_kind=StepKind.TRAIN,
                step_type="train_candidate",
            ),
        ).id

    session_a = postgres_session_factory()
    session_b = postgres_session_factory()
    try:
        # session_a가 PROGRESS 상태를 identity map에 적재한다.
        cached = update_experiment_step(
            session_a,
            experiment_id,
            step_id,
            ExperimentStepUpdate(status=StepStatus.PROGRESS, message="학습 중"),
        )
        assert cached.status == StepStatus.PROGRESS.value

        # session_b가 그 사이 터미널을 확정한다.
        update_experiment_step(
            session_b,
            experiment_id,
            step_id,
            ExperimentStepUpdate(status=StepStatus.COMPLETED, message="완료"),
        )

        # session_a는 여전히 stale 객체를 들고 있다.
        assert cached.status == StepStatus.PROGRESS.value

        # 비터미널로 되돌리는 갱신도 확정된 결과를 덮지 못해야 한다.
        with pytest.raises(StepAlreadyFinalizedError):
            update_experiment_step(
                session_a,
                experiment_id,
                step_id,
                ExperimentStepUpdate(status=StepStatus.PROGRESS, message="학습 중"),
            )
    finally:
        session_a.close()
        session_b.close()

    with postgres_session_factory() as session:
        stored = session.scalar(
            select(ExperimentStep).where(ExperimentStep.id == step_id)
        )
    assert stored is not None
    assert stored.status == StepStatus.COMPLETED.value
    assert stored.message == "완료"


def test_concurrent_terminal_transitions_finalize_exactly_one(
    postgres_session_factory: sessionmaker[Session],
) -> None:
    """비터미널 Step에 COMPLETED와 FAILED를 동시에 쓰면 한쪽만 확정된다."""
    with postgres_session_factory() as setup_session:
        experiment_id = create_experiment(
            setup_session,
            ExperimentCreate(hypothesis="postgres concurrent terminal"),
        ).id
        step_id = create_experiment_step(
            setup_session,
            experiment_id,
            ExperimentStepCreate(
                idempotency_key=f"terminal-{uuid.uuid4()}",
                step_kind=StepKind.EVALUATE,
                step_type="evaluate_candidate",
                status=StepStatus.PROGRESS,
            ),
        ).id
    start_barrier = threading.Barrier(2)

    def submit(requested: StepStatus) -> tuple[str, str]:
        with postgres_session_factory() as session:
            request = ExperimentStepUpdate(status=requested, message=requested.value)
            start_barrier.wait(timeout=5)
            try:
                row = update_experiment_step(session, experiment_id, step_id, request)
                return ("success", row.status)
            except StepAlreadyFinalizedError:
                return ("conflict", requested.value)

    executor = ThreadPoolExecutor(max_workers=2)
    try:
        futures = [
            executor.submit(submit, requested)
            for requested in (StepStatus.COMPLETED, StepStatus.FAILED)
        ]
        outcomes = [future.result(timeout=10) for future in futures]
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    with postgres_session_factory() as session:
        stored = session.scalar(
            select(ExperimentStep).where(ExperimentStep.id == step_id)
        )
    successes = [value for status, value in outcomes if status == "success"]
    conflicts = [value for status, value in outcomes if status == "conflict"]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert stored is not None
    assert stored.status == successes[0]
    assert stored.status != conflicts[0]
