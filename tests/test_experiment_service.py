"""실험 워크벤치 service의 생성·조회·상태 쓰기 계약을 검증한다.

전체 파이프라인에서 FastAPI 입력이 SQLAlchemy transaction을 통해 실험, event,
metadata로 일관되게 저장되는 경계를 담당한다. HTTP 인증과 실제 PostgreSQL migration은
이 모듈의 검증 범위가 아니다.
"""

from __future__ import annotations

from collections.abc import Iterator
import uuid

import pytest
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from agent_orchestration.app.database import Base
from agent_orchestration.app.experiments.exceptions import ExperimentNotFoundError
from agent_orchestration.app.experiments.models import (
    Experiment,
    ExperimentEvent,
    ExperimentMetadata,
    ExperimentStatus,
)
from agent_orchestration.app.experiments.schemas import ExperimentCreate
from agent_orchestration.app.experiments.service import (
    create_experiment,
    get_experiment,
    get_experiment_metadata,
    list_experiments,
)


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
