"""Experiment에 에이전트가 쓴 리포트 본문을 추가한다.

전체 파이프라인에서 executor가 완주 보고에 실은 `report.md` 본문을 실험 행에 한 번
적재하는 schema 구간을 담당한다. 적재 시점의 트랜잭션 분리와 정규화는 application
service의 책임이다.

Revision ID: 0006_experiment_report_markdown
Revises: 0005_experiment_candidate_sha
Create Date: 2026-08-10
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_experiment_report_markdown"
down_revision = "0005_experiment_candidate_sha"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """nullable 리포트 본문 컬럼을 추가한다.

    형식 제약을 두지 않는다 — 본문은 에이전트가 쓴 자유 서술이고, 크기 상한은
    거절이 아니라 절단으로 처리한다(spec 결정 3).
    """
    op.add_column(
        "experiments", sa.Column("report_markdown", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    """리포트 본문 컬럼을 제거한다."""
    op.drop_column("experiments", "report_markdown")
