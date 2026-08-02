"""실험 워크벤치 DB 기반과 migration 계약을 검증한다.

전체 파이프라인에서 Agent Orchestration 실험 상태를 PostgreSQL에 영속화하기 전의
SQLAlchemy 연결·DDL 경계를 담당한다. 상태 전이와 API 동작은 이 모듈의 검증 범위가
아니다.
"""

from __future__ import annotations

from io import StringIO
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, UniqueConstraint

from agent_orchestration.app.database import create_database_engine
from agent_orchestration.app.experiments.models import (
    Experiment,
    ExperimentEvent,
    ExperimentLog,
    ExperimentMetadata,
)


_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_database_engine_uses_psycopg_driver_for_plain_postgresql_url() -> None:
    """plain PostgreSQL URL이 psycopg 3 SQLAlchemy dialect로 정규화되지 않는 회귀를 잡는다."""
    engine = create_database_engine(
        "postgresql://orch:pw@localhost:5432/orch",
        connect_timeout_sec=7,
    )

    try:
        assert engine.url.drivername == "postgresql+psycopg"
    finally:
        engine.dispose()


def test_database_engine_pool_matches_sync_endpoint_threadpool_ceiling() -> None:
    """pool_size+max_overflow가 SQLAlchemy 기본값(15)에 머물러 있는 회귀를 잡는다."""
    engine = create_database_engine("postgresql://orch:pw@localhost:5432/orch")

    try:
        assert engine.pool.size() == 20
        assert engine.pool._max_overflow == 20
    finally:
        engine.dispose()


def test_initial_migration_offline_sql_contains_workbench_contract() -> None:
    """초기 migration에서 테이블·멱등 제약·서버 UUID 기본값이 빠지는 회귀를 잡는다."""
    output = StringIO()
    config = Config(str(_REPO_ROOT / "agent_orchestration" / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://offline")
    config.output_buffer = output

    command.upgrade(config, "head", sql=True)

    sql = output.getvalue()
    assert "CREATE TABLE experiments" in sql
    assert "CREATE TABLE experiment_events" in sql
    assert "CREATE TABLE experiment_logs" in sql
    assert "CREATE TABLE experiment_metadata" in sql
    assert sql.count("gen_random_uuid()") == 4
    assert "CONSTRAINT uq_experiment_events_idempotency" in sql
    assert "CONSTRAINT uq_experiment_logs_idempotency" in sql
    assert "CONSTRAINT ck_experiment_status_valid" in sql


def test_offline_migration_does_not_disable_existing_application_loggers() -> None:
    """Alembic SQL 생성이 이후 테스트와 앱 logger를 전역 비활성화하지 않는다."""
    logger = logging.getLogger("autoresearch.migration-regression")
    original_disabled = logger.disabled
    logger.disabled = False
    output = StringIO()
    config = Config(str(_REPO_ROOT / "agent_orchestration" / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", "postgresql+psycopg://offline")
    config.output_buffer = output

    try:
        command.upgrade(config, "head", sql=True)
        assert logger.disabled is False
    finally:
        logger.disabled = original_disabled


def test_orm_primary_keys_use_the_migration_server_uuid_default() -> None:
    """ORM만 uuid4를 생성해 migration의 server default와 갈라지는 회귀를 잡는다."""
    for model in (Experiment, ExperimentEvent, ExperimentLog, ExperimentMetadata):
        id_column = model.__table__.c.id
        assert id_column.default is None
        assert str(id_column.server_default.arg) == "gen_random_uuid()"


def test_orm_indexes_match_the_initial_migration() -> None:
    """ORM의 index=True가 migration에 없는 중복 단일 인덱스를 만드는 회귀를 잡는다."""
    assert {index.name for index in Experiment.__table__.indexes} == {
        "ix_experiments_status"
    }
    assert {index.name for index in ExperimentEvent.__table__.indexes} == {
        "ix_events_experiment_created"
    }
    assert {index.name for index in ExperimentLog.__table__.indexes} == {
        "ix_logs_experiment_created"
    }
    assert ExperimentMetadata.__table__.indexes == set()


def test_orm_constraints_match_the_initial_migration() -> None:
    """상태 check와 두 멱등성·metadata unique 제약이 ORM에서 누락되는 회귀를 잡는다."""
    experiment_checks = {
        constraint.name
        for constraint in Experiment.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert experiment_checks == {"ck_experiment_status_valid"}

    for model, constraint_name in (
        (ExperimentEvent, "uq_experiment_events_idempotency"),
        (ExperimentLog, "uq_experiment_logs_idempotency"),
        (ExperimentMetadata, "uq_experiment_metadata_key"),
    ):
        unique_names = {
            constraint.name
            for constraint in model.__table__.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        assert unique_names == {constraint_name}


def test_orm_foreign_keys_keep_database_cascade_delete() -> None:
    """부모 실험 삭제 시 자식 row가 남는 FK 매핑 회귀를 잡는다."""
    for model in (ExperimentEvent, ExperimentLog, ExperimentMetadata):
        foreign_key = next(iter(model.__table__.c.experiment_id.foreign_keys))
        assert foreign_key.ondelete == "CASCADE"
