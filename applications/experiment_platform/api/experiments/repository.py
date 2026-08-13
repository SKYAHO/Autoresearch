"""Agent Orchestration 실험 모델의 SQLAlchemy query를 제공한다.

전체 파이프라인에서 실험 service와 PostgreSQL ORM 사이의 조회 경계를 담당한다.
transaction commit, 상태 전이 판단과 HTTP 응답 생성은 담당하지 않는다.
"""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, undefer

from applications.experiment_platform.api.experiments.exceptions import InvalidCursorError
from applications.experiment_platform.api.experiments.models import (
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


def find_experiment_report(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Experiment | None:
    """리포트 본문을 함께 로드해 실험을 조회한다.

    `Experiment.report_markdown`은 deferred라 `find_experiment`로 읽으면 접근 시점에
    별도 SELECT가 나간다. 본문이 목적인 조회는 그것을 한 번에 싣는다.
    """
    statement = (
        select(Experiment)
        .where(Experiment.id == experiment_id)
        .options(undefer(Experiment.report_markdown))
    )
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


def find_experiment_step(
    session: Session,
    experiment_id: uuid.UUID,
    step_id: uuid.UUID,
) -> ExperimentStep | None:
    """한 실험에 속한 Step을 UUID로 조회한다."""
    return session.scalar(
        select(ExperimentStep).where(
            ExperimentStep.experiment_id == experiment_id,
            ExperimentStep.id == step_id,
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


def find_experiment_steps(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    limit: int,
    after_id: uuid.UUID | None,
    step_kind: str | None,
) -> list[ExperimentStep]:
    """created_at ASC, id ASC cursor 규칙으로 새 Step을 조회한다."""
    filters = [ExperimentStep.experiment_id == experiment_id]
    if step_kind is not None:
        filters.append(ExperimentStep.step_kind == step_kind)
    if after_id is not None:
        cursor = session.scalar(
            select(ExperimentStep).where(
                ExperimentStep.experiment_id == experiment_id,
                ExperimentStep.id == after_id,
            )
        )
        if cursor is None:
            raise InvalidCursorError(after_id)
        filters.append(
            or_(
                ExperimentStep.created_at > cursor.created_at,
                and_(
                    ExperimentStep.created_at == cursor.created_at,
                    ExperimentStep.id > cursor.id,
                ),
            )
        )
    return list(
        session.scalars(
            select(ExperimentStep)
            .where(*filters)
            .order_by(ExperimentStep.created_at.asc(), ExperimentStep.id.asc())
            .limit(limit)
        ).all()
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


def find_log_contents(session: Session, experiment_id: uuid.UUID) -> list[str]:
    """실험 하나의 로그 본문 전체를 적재 순서대로 이어 붙여 조회한다.

    비용 집계는 cursor 페이지가 아니라 전체가 필요하다. 수집기가 8000자 청크로 쪼개
    적재하므로(#559), 청크 경계에서 잘린 줄을 되살리려면 순서를 지켜 이어야 한다.
    같은 `log_type`끼리만 잇는다 — 다른 컨테이너의 출력이 한 줄로 붙으면 안 된다.

    DB 함수(`string_agg`)가 아니라 파이썬에서 잇는 이유는 이식성이다. 실험 하나의
    로그는 수백 행 규모라 옮겨 붙이는 비용이 문제되지 않는다.
    """
    rows = session.execute(
        select(ExperimentLog.log_type, ExperimentLog.content)
        .where(ExperimentLog.experiment_id == experiment_id)
        .order_by(ExperimentLog.created_at, ExperimentLog.id)
    ).all()
    joined: dict[str, list[str]] = {}
    for log_type, content in rows:
        if content:
            joined.setdefault(log_type, []).append(content)
    return ["".join(chunks) for chunks in joined.values()]


def find_last_event_time(session: Session, experiment_id: uuid.UUID) -> datetime | None:
    """실험의 마지막 event 시각을 조회한다.

    실행 시간의 끝으로 `Experiment.updated_at`이 아니라 이 값을 쓰는 이유는
    `updated_at`이 `onupdate=func.now()`라 실행과 무관한 UPDATE에도 움직이기 때문이다.
    """
    return session.scalar(
        select(func.max(ExperimentEvent.created_at)).where(
            ExperimentEvent.experiment_id == experiment_id
        )
    )
