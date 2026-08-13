"""Agent Orchestration 실험 영속화 구간에 작업 단계 테이블을 추가한다.

전체 파이프라인에서 에이전트가 실험 도중 수행하는 작업 단계와 진행 상태를 PostgreSQL에
저장할 schema를 담당한다. API 상태 전이 검증과 기존 실험 워크벤치 네 테이블은 담당하지
않는다.

`experiment_steps` 한 테이블과 FK cascade, step_kind·status check, 멱등성 unique
constraint, cursor polling용 3컬럼 index를 생성하고 역순으로 제거한다.

Revision ID: 0002_experiment_steps
Revises: 0001_experiment_tables
Create Date: 2026-08-04
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0002_experiment_steps"
down_revision = "0001_experiment_tables"
branch_labels = None
depends_on = None

STEP_KINDS = (
    "FEATURE_ASSEMBLY",
    "FEATURE_DERIVE",
    "TRAIN",
    "EVALUATE",
    "OTHER",
)
STEP_STATUSES = (
    "STARTED",
    "PROGRESS",
    "COMPLETED",
    "FAILED",
)


def upgrade() -> None:
    """experiment_steps 테이블과 제약·인덱스를 생성한다."""
    op.create_table(
        "experiment_steps",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "experiment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("experiments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("step_kind", sa.String(length=32), nullable=False),
        sa.Column("step_type", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="STARTED"),
        sa.Column("message", sa.String(length=500), nullable=True),
        sa.Column("target", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "step_kind IN (" + ", ".join(f"'{kind}'" for kind in STEP_KINDS) + ")",
            name="ck_experiment_step_kind_valid",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{status}'" for status in STEP_STATUSES) + ")",
            name="ck_experiment_step_status_valid",
        ),
        sa.UniqueConstraint(
            "experiment_id",
            "idempotency_key",
            name="uq_experiment_steps_idempotency",
        ),
    )
    # events/logs의 2컬럼 인덱스와 달리 `id`를 포함한다 — cursor 쿼리의 keyset이
    # (created_at, id)이므로 3컬럼이라야 정확히 덮는다. 기존 두 인덱스는 바꾸지 않는다.
    op.create_index(
        "ix_steps_experiment_created",
        "experiment_steps",
        ["experiment_id", "created_at", "id"],
    )


def downgrade() -> None:
    """experiment_steps 인덱스와 테이블을 제거한다."""
    op.drop_index("ix_steps_experiment_created", table_name="experiment_steps")
    op.drop_table("experiment_steps")
