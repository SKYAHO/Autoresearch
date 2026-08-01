"""실험 워크벤치 DB 기반과 migration 계약을 검증한다.

전체 파이프라인에서 Agent Orchestration 실험 상태를 PostgreSQL에 영속화하기 전의
SQLAlchemy 연결·DDL 경계를 담당한다. 상태 전이와 API 동작은 이 모듈의 검증 범위가
아니다.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from alembic import command
from alembic.config import Config

from agent_orchestration.app.database import create_database_engine


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
