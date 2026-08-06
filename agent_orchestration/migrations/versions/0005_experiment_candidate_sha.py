"""Experiment에 executor 검증 candidate SHA를 추가한다.

전체 파이프라인에서 executor가 원격 Git 검증을 마친 후보 SHA를 실험 lineage에 한 번
기록하는 schema 구간을 담당한다. SHA 검증 요청 처리와 RUNNING→EVALUATING 전이는
application service의 책임이다.

Revision ID: 0005_experiment_candidate_sha
Revises: 0004_experiment_branch_bootstrap
Create Date: 2026-08-06
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_experiment_candidate_sha"
down_revision = "0004_experiment_branch_bootstrap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """nullable candidate SHA와 PostgreSQL 형식 제약을 추가한다."""
    op.add_column(
        "experiments", sa.Column("candidate_sha", sa.String(length=40), nullable=True)
    )
    op.create_check_constraint(
        "ck_experiment_candidate_sha_format",
        "experiments",
        "candidate_sha IS NULL OR candidate_sha ~ '^[0-9a-f]{40}$'",
    )


def downgrade() -> None:
    """형식 제약을 먼저 제거한 뒤 candidate SHA 컬럼을 제거한다."""
    op.drop_constraint("ck_experiment_candidate_sha_format", "experiments", type_="check")
    op.drop_column("experiments", "candidate_sha")
