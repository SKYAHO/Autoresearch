"""Agent Orchestration 실험 모델의 SQLAlchemy query를 제공한다.

전체 파이프라인에서 실험 service와 PostgreSQL ORM 사이의 조회 경계를 담당한다.
transaction commit, 상태 전이 판단과 HTTP 응답 생성은 담당하지 않는다.
"""

from __future__ import annotations

import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from agent_orchestration.app.experiments.exceptions import InvalidCursorError
from agent_orchestration.app.experiments.models import (
    Experiment,
    ExperimentEvent,
    ExperimentLog,
    ExperimentMetadata,
    ExperimentStatus,
    ExperimentStep,
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


def find_event_by_idempotency_key(
    session: Session,
    experiment_id: uuid.UUID,
    idempotency_key: str,
) -> ExperimentEvent | None:
    """한 실험에서 멱등성 key가 같은 기존 event를 조회한다."""
    return session.scalar(
        select(ExperimentEvent).where(
            ExperimentEvent.experiment_id == experiment_id,
            ExperimentEvent.idempotency_key == idempotency_key,
        )
    )


def find_experiment_events(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    limit: int,
    after_id: uuid.UUID | None,
) -> list[ExperimentEvent]:
    """created_at ASC, id ASC cursor 규칙으로 새 Event를 조회한다."""
    filters = [ExperimentEvent.experiment_id == experiment_id]
    if after_id is not None:
        cursor = session.scalar(
            select(ExperimentEvent).where(
                ExperimentEvent.experiment_id == experiment_id,
                ExperimentEvent.id == after_id,
            )
        )
        if cursor is None:
            raise InvalidCursorError(after_id)
        filters.append(
            or_(
                ExperimentEvent.created_at > cursor.created_at,
                and_(
                    ExperimentEvent.created_at == cursor.created_at,
                    ExperimentEvent.id > cursor.id,
                ),
            )
        )
    return list(
        session.scalars(
            select(ExperimentEvent)
            .where(*filters)
            .order_by(ExperimentEvent.created_at.asc(), ExperimentEvent.id.asc())
            .limit(limit)
        ).all()
    )


def find_log_by_idempotency_key(
    session: Session,
    experiment_id: uuid.UUID,
    idempotency_key: str,
) -> ExperimentLog | None:
    """한 실험에서 멱등성 key가 같은 기존 Log를 조회한다."""
    return session.scalar(
        select(ExperimentLog).where(
            ExperimentLog.experiment_id == experiment_id,
            ExperimentLog.idempotency_key == idempotency_key,
        )
    )


def find_step_by_idempotency_key(
    session: Session,
    experiment_id: uuid.UUID,
    idempotency_key: str,
) -> ExperimentStep | None:
    """한 실험에서 멱등성 key가 같은 기존 Step을 조회한다."""
    return session.scalar(
        select(ExperimentStep).where(
            ExperimentStep.experiment_id == experiment_id,
            ExperimentStep.idempotency_key == idempotency_key,
        )
    )


def find_experiment_logs(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    limit: int,
    after_id: uuid.UUID | None,
    log_type: str | None,
) -> list[ExperimentLog]:
    """created_at ASC, id ASC cursor 규칙으로 새 Log를 조회한다."""
    filters = [ExperimentLog.experiment_id == experiment_id]
    if log_type is not None:
        filters.append(ExperimentLog.log_type == log_type)
    if after_id is not None:
        cursor = session.scalar(
            select(ExperimentLog).where(
                ExperimentLog.experiment_id == experiment_id,
                ExperimentLog.id == after_id,
            )
        )
        if cursor is None:
            raise InvalidCursorError(after_id)
        filters.append(
            or_(
                ExperimentLog.created_at > cursor.created_at,
                and_(
                    ExperimentLog.created_at == cursor.created_at,
                    ExperimentLog.id > cursor.id,
                ),
            )
        )
    return list(
        session.scalars(
            select(ExperimentLog)
            .where(*filters)
            .order_by(ExperimentLog.created_at.asc(), ExperimentLog.id.asc())
            .limit(limit)
        ).all()
    )
