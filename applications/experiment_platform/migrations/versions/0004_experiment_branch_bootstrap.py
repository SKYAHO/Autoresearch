"""Experiment에 branch bootstrap 기준선과 Job 복구 좌표를 추가한다.

전체 파이프라인에서 `[AR]` 이슈 발행 전에 고정한 `dev` SHA를 보관하고, 후속 launcher가
결정론적 Kubernetes Job 생성을 재개할 수 있는 내부 좌표를 제공하는 schema 구간이다.
SHA 조회·이슈 발행과 Job 생성 자체는 각각 API service와 launcher의 책임이다.

Revision ID: 0004_experiment_branch_bootstrap
Revises: 0003_experiment_issue_lineage
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_experiment_branch_bootstrap"
down_revision = "0003_experiment_issue_lineage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """기준 SHA와 결정론적 Job 생성 복구 컬럼을 nullable로 추가한다."""
    # 기존 행은 세 좌표가 null인 채 남아 launcher 대상에서 제외된다.
    op.add_column(
        "experiments", sa.Column("base_dev_sha", sa.String(length=40), nullable=True)
    )
    op.add_column(
        "experiments", sa.Column("executor_job_name", sa.String(length=63), nullable=True)
    )
    op.add_column(
        "experiments",
        sa.Column("executor_job_created_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """branch bootstrap 컬럼을 upgrade의 역순으로 제거한다."""
    op.drop_column("experiments", "executor_job_created_at")
    op.drop_column("experiments", "executor_job_name")
    op.drop_column("experiments", "base_dev_sha")
