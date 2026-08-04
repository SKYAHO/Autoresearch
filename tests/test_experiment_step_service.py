"""실험 Step ORM 모델의 제약과 기본값을 검증한다.

전체 파이프라인 중 Agent Orchestration 실험 API가 작업 단계를 저장하는 구간의 스키마
계약을 확인한다. 생성 멱등성과 PATCH 가드는 각각 Task 3·4에서 service 계층과 함께
추가하며, PostgreSQL 동시성 계약은 tests/test_experiment_postgres.py가 담당한다.
"""

from __future__ import annotations

from collections.abc import Iterator
import uuid

import pytest
from sqlalchemy import Engine, create_engine, event, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agent_orchestration.app.database import Base
from agent_orchestration.app.experiments.models import (
    Experiment,
    ExperimentStep,
    StepKind,
    StepStatus,
    TERMINAL_STEP_STATUSES,
)


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    """PostgreSQL server UUID 함수를 재현하는 in-memory SQLAlchemy engine."""
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def register_uuid_function(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)
        # SQLite는 FK를 기본으로 강제하지 않는다. 켜지 않으면 passive_deletes=True인
        # cascade가 DB 레벨에서 일어나지 않아, PostgreSQL과 다른 결과가 조용히 나온다.
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

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


def _experiment(session: Session) -> Experiment:
    experiment = Experiment(hypothesis="새 파생 피처가 CTR을 높인다")
    session.add(experiment)
    session.flush()
    return experiment


def _step(experiment: Experiment, **overrides: object) -> ExperimentStep:
    values: dict[str, object] = {
        "experiment_id": experiment.id,
        "idempotency_key": "step-1",
        "request_fingerprint": "f" * 64,
        "step_kind": StepKind.FEATURE_ASSEMBLY.value,
        "step_type": "assemble_training_dataset",
    }
    values.update(overrides)
    return ExperimentStep(**values)


def test_step_defaults_to_started(db_session: Session) -> None:
    """status를 지정하지 않은 Step은 STARTED로 저장된다."""
    experiment = _experiment(db_session)
    db_session.add(_step(experiment))
    db_session.commit()

    stored = db_session.scalar(select(ExperimentStep))
    assert stored is not None
    assert stored.status == StepStatus.STARTED.value
    assert stored.message is None
    assert stored.target is None


def test_step_target_roundtrips_as_json_object(db_session: Session) -> None:
    """target은 JSON object로 저장·복원된다."""
    experiment = _experiment(db_session)
    target = {"features": ["views_per_day", "like_ratio"], "base_model": "lightgbm"}
    db_session.add(_step(experiment, target=target, message="파생 피처 2개 조립 중"))
    db_session.commit()

    stored = db_session.scalar(select(ExperimentStep))
    assert stored is not None
    assert stored.target == target
    assert stored.message == "파생 피처 2개 조립 중"


def test_duplicate_idempotency_key_in_same_experiment_is_rejected(
    db_session: Session,
) -> None:
    """같은 실험에서 같은 idempotency_key를 두 번 쓰면 unique constraint가 막는다."""
    experiment = _experiment(db_session)
    db_session.add(_step(experiment))
    db_session.commit()

    db_session.add(_step(experiment, step_type="train_candidate"))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_same_idempotency_key_is_allowed_across_experiments(db_session: Session) -> None:
    """멱등성 범위는 실험 단위다 — 다른 실험이면 같은 key를 쓸 수 있다."""
    first = _experiment(db_session)
    second = _experiment(db_session)
    db_session.add(_step(first))
    db_session.add(_step(second))
    db_session.commit()

    assert db_session.scalars(select(ExperimentStep)).all().__len__() == 2


def test_deleting_experiment_cascades_to_steps(db_session: Session) -> None:
    """실험을 지우면 Step도 함께 지워진다."""
    experiment = _experiment(db_session)
    db_session.add(_step(experiment))
    db_session.commit()

    db_session.delete(experiment)
    db_session.commit()

    assert db_session.scalar(select(ExperimentStep)) is None


def test_polling_index_covers_created_at_and_id(sqlite_engine: Engine) -> None:
    """cursor keyset이 (created_at, id)이므로 인덱스도 3컬럼이어야 한다."""
    indexes = inspect(sqlite_engine).get_indexes("experiment_steps")
    polling = next(ix for ix in indexes if ix["name"] == "ix_steps_experiment_created")
    assert polling["column_names"] == ["experiment_id", "created_at", "id"]


def test_terminal_step_statuses_are_completed_and_failed() -> None:
    """터미널 가드가 참조하는 집합을 계약으로 고정한다."""
    assert TERMINAL_STEP_STATUSES == frozenset(
        {StepStatus.COMPLETED, StepStatus.FAILED}
    )
