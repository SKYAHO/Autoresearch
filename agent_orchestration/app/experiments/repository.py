"""Agent Orchestration 실험 모델의 SQLAlchemy query를 제공한다.

전체 파이프라인에서 실험 service와 PostgreSQL ORM 사이의 조회 경계를 담당한다.
transaction commit, 상태 전이 판단과 HTTP 응답 생성은 담당하지 않는다.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent_orchestration.app.experiments.models import (
    Experiment,
    ExperimentMetadata,
    ExperimentStatus,
)


def find_experiment(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Experiment | None:
    """UUID로 실험을 조회하고 필요하면 row lock을 요청한다."""
    statement = select(Experiment).where(Experiment.id == experiment_id)
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)


def find_experiments(
    session: Session,
    *,
    limit: int,
    offset: int,
    status: ExperimentStatus | None,
) -> tuple[list[Experiment], int]:
    """필터 적용 후 전체 수와 현재 page를 조회한다."""
    filters = () if status is None else (Experiment.status == status.value,)
    total = session.scalar(select(func.count()).select_from(Experiment).where(*filters))
    items = session.scalars(
        select(Experiment)
        .where(*filters)
        .order_by(Experiment.created_at.desc(), Experiment.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    return list(items), int(total or 0)


def find_experiment_metadata(
    session: Session,
    experiment_id: uuid.UUID,
) -> dict[str, str]:
    """실험 metadata row를 key-value mapping으로 반환한다."""
    entries = session.scalars(
        select(ExperimentMetadata)
        .where(ExperimentMetadata.experiment_id == experiment_id)
        .order_by(ExperimentMetadata.key)
    ).all()
    return {entry.key: entry.value for entry in entries}
