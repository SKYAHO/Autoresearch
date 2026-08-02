"""Agent Orchestration 실험 영속화 구간의 Alembic 실행 환경.

전체 파이프라인에서 실험 워크벤치 API가 사용하는 PostgreSQL schema version을
적용하는 구간을 담당한다. API 요청 처리와 기존 `/chat` 테이블 생성은 담당하지 않는다.

환경 변수의 DB URL을 SQLAlchemy dialect로 정규화하고 offline·online migration을
Alembic context에 연결한다.
"""

from __future__ import annotations

from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from agent_orchestration.app.database import Base, _sqlalchemy_database_url


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _migration_database_url(*, allow_placeholder: bool) -> str:
    """환경 변수를 우선하여 migration용 SQLAlchemy URL을 반환한다.

    `allow_placeholder=False`(online 실행)에서 환경 변수가 없으면 alembic.ini의
    `postgresql+psycopg://localhost/autoresearch` placeholder로 조용히 fallback하지
    않고 즉시 실패한다 — 이 placeholder가 실제 접속 가능한 localhost Postgres(예:
    Cloud SQL Auth Proxy sidecar)를 가리키는 배포 환경에서는 의도하지 않은 DB에
    migration이 적용될 위험이 있다. `allow_placeholder=True`(offline `--sql` 생성)는
    실제로 연결하지 않으므로 placeholder를 그대로 허용한다.
    """
    database_url = os.getenv("ORCH_DATABASE_URL") or os.getenv("DATABASE_URL")
    if database_url:
        return _sqlalchemy_database_url(database_url.strip())
    if allow_placeholder:
        return config.get_main_option("sqlalchemy.url")
    raise RuntimeError(
        "ORCH_DATABASE_URL (or DATABASE_URL) must be set to run online migrations; "
        "refusing to fall back to alembic.ini's placeholder URL."
    )


def run_migrations_offline() -> None:
    """DB 연결 없이 SQL migration을 생성한다."""
    context.configure(
        url=_migration_database_url(allow_placeholder=True),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """대상 PostgreSQL 연결에서 migration을 실행한다."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _migration_database_url(allow_placeholder=False)
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
