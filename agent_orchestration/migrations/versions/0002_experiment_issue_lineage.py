"""Agent Orchestration 실험에 이슈 발행 lineage 컬럼을 추가한다.

전체 파이프라인에서 가설이 GitHub `[AR]` 이슈로 발행된 사실을 실험 행에 남기는 구간을
담당한다. 발행 절차와 HTTP 계약은 각각 service와 router의 책임이다.

`issue_body`(발행 전 커밋), `issue_number`, `issue_branch`, `issue_published_at`을
nullable로 추가하고
`issue_number` 조회 index를 만든 뒤 역순으로 제거한다.

Revision ID: 0002_experiment_issue_lineage
Revises: 0001_experiment_tables
Create Date: 2026-08-04
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_experiment_issue_lineage"
down_revision = "0001_experiment_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """발행 lineage 컬럼 셋과 issue_number index를 추가한다."""
    # 기존 행이 있으므로 셋 모두 nullable이다. issue_number에 unique를 두지 않는 것은
    # 이슈 1건이 실험 N건을 가질 수 있기 때문이다.
    op.add_column("experiments", sa.Column("issue_body", sa.Text(), nullable=True))
    op.add_column("experiments", sa.Column("issue_number", sa.Integer(), nullable=True))
    op.add_column(
        "experiments", sa.Column("issue_branch", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "experiments",
        sa.Column("issue_published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_experiments_issue_number", "experiments", ["issue_number"])


def downgrade() -> None:
    """upgrade의 역순으로 index와 컬럼을 제거한다."""
    op.drop_index("ix_experiments_issue_number", table_name="experiments")
    op.drop_column("experiments", "issue_published_at")
    op.drop_column("experiments", "issue_branch")
    op.drop_column("experiments", "issue_number")
    op.drop_column("experiments", "issue_body")
