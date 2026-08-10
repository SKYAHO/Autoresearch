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
from agent_orchestration.app.experiments.models import Experiment
from agent_orchestration.app.experiments.repository import find_experiment_report


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
