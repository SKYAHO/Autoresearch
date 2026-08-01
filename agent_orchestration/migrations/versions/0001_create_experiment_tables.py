"""Agent Orchestration 실험 영속화 구간의 v0 테이블을 생성한다.

전체 파이프라인에서 실험 상태·event·log·metadata를 PostgreSQL에 저장할 schema를
담당한다. API 상태 전이 검증과 기존 `/chat` 저장 schema는 담당하지 않는다.

실험 워크벤치의 네 테이블, FK cascade, 상태 check와 멱등성 unique constraint를
생성하고 역순으로 제거한다.

Revision ID: 0001_experiment_tables
Revises:
Create Date: 2026-08-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0001_experiment_tables"
down_revision = None
branch_labels = None
depends_on = None

EXPERIMENT_STATUSES = (
    "CREATED",
    "RUNNING",
    "EVALUATING",
    "PASSED",
    "FAILED",
    "ERROR",
    "PROMOTED",
)


def _uuid_column(name: str) -> sa.Column:
    """PostgreSQL server가 UUID를 생성하는 기본키 column을 만든다."""
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        server_default=sa.text("gen_random_uuid()"),
    )


def upgrade() -> None:
    """실험, event, log와 metadata 테이블을 생성한다."""
    op.create_table(
        "experiments",
        _uuid_column("id"),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="CREATED"),
        sa.Column("metric_summary", postgresql.JSONB(), nullable=True),
        sa.Column("agent_session_id", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{status}'" for status in EXPERIMENT_STATUSES) + ")",
            name="ck_experiment_status_valid",
        ),
    )
    op.create_index("ix_experiments_status", "experiments", ["status"])

    op.create_table(
        "experiment_events",
        _uuid_column("id"),
        sa.Column(
            "experiment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("experiments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("from_status", sa.String(length=20), nullable=True),
        sa.Column("to_status", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("metric_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "experiment_id",
            "idempotency_key",
            name="uq_experiment_events_idempotency",
        ),
    )
    op.create_index(
        "ix_events_experiment_created",
        "experiment_events",
        ["experiment_id", "created_at"],
    )

    op.create_table(
        "experiment_logs",
        _uuid_column("id"),
        sa.Column(
            "experiment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("experiments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("log_type", sa.String(length=32), nullable=False, server_default="stdout"),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "experiment_id",
            "idempotency_key",
            name="uq_experiment_logs_idempotency",
        ),
    )
    op.create_index(
        "ix_logs_experiment_created",
        "experiment_logs",
        ["experiment_id", "created_at"],
    )

    op.create_table(
        "experiment_metadata",
        _uuid_column("id"),
        sa.Column(
            "experiment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("experiments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "experiment_id",
            "key",
            name="uq_experiment_metadata_key",
        ),
    )


def downgrade() -> None:
    """실험 워크벤치 테이블을 자식부터 제거한다."""
    op.drop_table("experiment_metadata")
    op.drop_index("ix_logs_experiment_created", table_name="experiment_logs")
    op.drop_table("experiment_logs")
    op.drop_index("ix_events_experiment_created", table_name="experiment_events")
    op.drop_table("experiment_events")
    op.drop_index("ix_experiments_status", table_name="experiments")
    op.drop_table("experiments")
