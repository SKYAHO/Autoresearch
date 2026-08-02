"""Agent Orchestration 실험 생성·조회와 상태 쓰기 유스케이스를 제공한다.

전체 파이프라인에서 검증된 API 입력을 SQLAlchemy transaction으로 실험·event·log·metadata에
반영하는 구간을 담당한다. HTTP 인증·상태 코드 변환과 실제 학습 실행은 담당하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_orchestration.app.experiments.exceptions import (
    ExperimentNotFoundError,
    IdempotencyConflictError,
    PromotionRequiresDedicatedEndpointError,
)
from agent_orchestration.app.experiments.models import (
    Experiment,
    ExperimentEvent,
    ExperimentLog,
    ExperimentMetadata,
    ExperimentStatus,
)
from agent_orchestration.app.experiments.repository import (
    find_experiment,
    find_experiment_events,
    find_event_by_idempotency_key,
    find_experiment_logs,
    find_experiment_metadata,
    find_experiments,
    find_log_by_idempotency_key,
)
from agent_orchestration.app.experiments.schemas import (
    ExperimentCreate,
    ExperimentEventCreate,
    ExperimentLogCreate,
    PromotionRequest,
    StatusUpdateRequest,
)
from agent_orchestration.app.experiments.transition_service import validate_transition


@dataclass(frozen=True)
class ExperimentPageResult:
    """목록 응답을 만들기 위한 현재 page와 전체 건수."""

    items: list[Experiment]
    total: int


@dataclass(frozen=True)
class ExperimentLogPageResult:
    """polling용 Log page와 다음 cursor."""

    items: list[ExperimentLog]
    next_cursor: uuid.UUID | None


@dataclass(frozen=True)
class ExperimentEventPageResult:
    """polling용 Event page와 다음 cursor."""

    items: list[ExperimentEvent]
    next_cursor: uuid.UUID | None


def _request_fingerprint(payload: dict) -> str:
    """의미 있는 요청 payload의 canonical JSON SHA-256을 반환한다."""
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_experiment(session: Session, request: ExperimentCreate) -> Experiment:
    """실험, metadata와 최초 CREATED event를 원자적으로 생성한다."""
    experiment = Experiment(
        hypothesis=request.hypothesis,
        agent_session_id=request.agent_session_id,
    )
    with session.begin():
        session.add(experiment)
        session.flush()
        session.add_all(
            ExperimentMetadata(
                experiment_id=experiment.id,
                key=key,
                value=value,
            )
            for key, value in request.metadata.items()
        )
        initial_key = f"experiment-created:{experiment.id}"
        session.add(
            ExperimentEvent(
                experiment_id=experiment.id,
                idempotency_key=initial_key,
                request_fingerprint=_request_fingerprint(
                    {"experiment_id": str(experiment.id), "to_status": "CREATED"}
                ),
                from_status=None,
                to_status=ExperimentStatus.CREATED.value,
            )
        )
    return experiment


def get_experiment(session: Session, experiment_id: uuid.UUID) -> Experiment:
    """실험을 반환하거나 도메인 not-found 오류를 발생시킨다."""
    experiment = find_experiment(session, experiment_id)
    if experiment is None:
        raise ExperimentNotFoundError(experiment_id)
    return experiment


def list_experiments(
    session: Session,
    *,
    limit: int,
    offset: int,
    status: ExperimentStatus | None = None,
) -> ExperimentPageResult:
    """필터와 pagination을 적용한 실험 목록을 반환한다."""
    items, total = find_experiments(
        session,
        limit=limit,
        offset=offset,
        status=status,
    )
    return ExperimentPageResult(items=items, total=total)


def get_experiment_metadata(
    session: Session,
    experiment_id: uuid.UUID,
) -> dict[str, str]:
    """존재하는 실험의 metadata를 mapping으로 반환한다."""
    get_experiment(session, experiment_id)
    return find_experiment_metadata(session, experiment_id)


def list_experiment_events(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    limit: int,
    after_id: uuid.UUID | None = None,
) -> ExperimentEventPageResult:
    """append 순서와 UUID tie-breaker를 적용한 Event polling page를 반환한다."""
    get_experiment(session, experiment_id)
    items = find_experiment_events(
        session,
        experiment_id,
        limit=limit,
        after_id=after_id,
    )
    return ExperimentEventPageResult(
        items=items,
        next_cursor=items[-1].id if items else after_id,
    )


def _require_general_transition(requested: ExperimentStatus) -> None:
    """수동 승격 전용 상태가 일반 쓰기 경로로 들어오면 거부한다."""
    if requested is ExperimentStatus.PROMOTED:
        raise PromotionRequiresDedicatedEndpointError


def _transition_experiment(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    requested: ExperimentStatus,
    reason: str | None,
    metric_snapshot: dict | None,
    idempotency_key: str,
    request_fingerprint: str,
    check_idempotency: bool,
) -> tuple[Experiment, ExperimentEvent]:
    """row lock 안에서 상태와 event를 한 transaction으로 갱신한다."""
    _require_general_transition(requested)
    with session.begin():
        experiment = find_experiment(session, experiment_id, for_update=True)
        if experiment is None:
            raise ExperimentNotFoundError(experiment_id)

        if check_idempotency:
            existing_event = find_event_by_idempotency_key(
                session,
                experiment_id,
                idempotency_key,
            )
            if existing_event is not None:
                if existing_event.request_fingerprint != request_fingerprint:
                    raise IdempotencyConflictError(idempotency_key)
                return experiment, existing_event

        current = ExperimentStatus(experiment.status)
        validate_transition(current, requested)
        experiment.status = requested.value
        if metric_snapshot is not None:
            experiment.metric_summary = metric_snapshot
        event_row = ExperimentEvent(
            experiment_id=experiment.id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            from_status=current.value,
            to_status=requested.value,
            reason=reason,
            metric_snapshot=metric_snapshot,
        )
        session.add(event_row)
        session.flush()
    return experiment, event_row


def update_experiment_status(
    session: Session,
    experiment_id: uuid.UUID,
    request: StatusUpdateRequest,
) -> Experiment:
    """클라이언트 멱등성을 제공하지 않는 일반 상태 변경을 수행한다."""
    requested = ExperimentStatus(request.status)
    payload = {
        "to_status": requested.value,
        "reason": request.reason,
        "metric_snapshot": request.metric_snapshot,
    }
    experiment, _event = _transition_experiment(
        session,
        experiment_id,
        requested=requested,
        reason=request.reason,
        metric_snapshot=request.metric_snapshot,
        idempotency_key=f"status-update:{uuid.uuid4()}",
        request_fingerprint=_request_fingerprint(payload),
        check_idempotency=False,
    )
    return experiment


def create_experiment_event(
    session: Session,
    experiment_id: uuid.UUID,
    request: ExperimentEventCreate,
) -> ExperimentEvent:
    """멱등성 key를 적용해 상태 전이 event를 생성한다."""
    requested = ExperimentStatus(request.to_status)
    payload = {
        "to_status": requested.value,
        "reason": request.reason,
        "metric_snapshot": request.metric_snapshot,
    }
    fingerprint = _request_fingerprint(payload)
    try:
        _experiment, event_row = _transition_experiment(
            session,
            experiment_id,
            requested=requested,
            reason=request.reason,
            metric_snapshot=request.metric_snapshot,
            idempotency_key=request.idempotency_key,
            request_fingerprint=fingerprint,
            check_idempotency=True,
        )
        return event_row
    except IntegrityError as error:
        session.rollback()
        existing_event = find_event_by_idempotency_key(
            session,
            experiment_id,
            request.idempotency_key,
        )
        if existing_event is None:
            raise error
        if existing_event.request_fingerprint != fingerprint:
            session.rollback()
            raise IdempotencyConflictError(request.idempotency_key) from error
        session.expunge(existing_event)
        session.rollback()
        return existing_event


def create_experiment_log(
    session: Session,
    experiment_id: uuid.UUID,
    request: ExperimentLogCreate,
) -> ExperimentLog:
    """상태와 무관하게 멱등성이 보장되는 실행 Log를 추가한다."""
    payload = {"log_type": request.log_type, "content": request.content}
    fingerprint = _request_fingerprint(payload)
    try:
        with session.begin():
            if find_experiment(session, experiment_id) is None:
                raise ExperimentNotFoundError(experiment_id)
            existing_log = find_log_by_idempotency_key(
                session,
                experiment_id,
                request.idempotency_key,
            )
            if existing_log is not None:
                if existing_log.request_fingerprint != fingerprint:
                    raise IdempotencyConflictError(request.idempotency_key)
                return existing_log
            log_row = ExperimentLog(
                experiment_id=experiment_id,
                idempotency_key=request.idempotency_key,
                request_fingerprint=fingerprint,
                log_type=request.log_type,
                content=request.content,
            )
            session.add(log_row)
            session.flush()
        return log_row
    except IntegrityError as error:
        session.rollback()
        existing_log = find_log_by_idempotency_key(
            session,
            experiment_id,
            request.idempotency_key,
        )
        if existing_log is None:
            session.rollback()
            raise error
        if existing_log.request_fingerprint != fingerprint:
            session.rollback()
            raise IdempotencyConflictError(request.idempotency_key) from error
        session.expunge(existing_log)
        session.rollback()
        return existing_log


def list_experiment_logs(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    limit: int,
    after_id: uuid.UUID | None = None,
    log_type: str | None = None,
) -> ExperimentLogPageResult:
    """append 순서와 UUID tie-breaker를 적용한 polling page를 반환한다."""
    get_experiment(session, experiment_id)
    items = find_experiment_logs(
        session,
        experiment_id,
        limit=limit,
        after_id=after_id,
        log_type=log_type,
    )
    return ExperimentLogPageResult(
        items=items,
        next_cursor=items[-1].id if items else after_id,
    )


def promote_experiment(
    session: Session,
    experiment_id: uuid.UUID,
    request: PromotionRequest,
) -> Experiment:
    """PASSED 실험을 운영 근거와 함께 멱등하게 PROMOTED로 전환한다."""
    payload = {
        "reason": request.reason,
        "deployment_metadata": request.deployment_metadata,
    }
    fingerprint = _request_fingerprint(payload)
    with session.begin():
        experiment = find_experiment(session, experiment_id, for_update=True)
        if experiment is None:
            raise ExperimentNotFoundError(experiment_id)

        existing_event = find_event_by_idempotency_key(
            session,
            experiment_id,
            request.idempotency_key,
        )
        if existing_event is not None:
            if existing_event.request_fingerprint != fingerprint:
                raise IdempotencyConflictError(request.idempotency_key)
            return experiment

        current = ExperimentStatus(experiment.status)
        validate_transition(current, ExperimentStatus.PROMOTED)
        experiment.status = ExperimentStatus.PROMOTED.value
        session.add(
            ExperimentEvent(
                experiment_id=experiment.id,
                idempotency_key=request.idempotency_key,
                request_fingerprint=fingerprint,
                from_status=current.value,
                to_status=ExperimentStatus.PROMOTED.value,
                reason=request.reason,
                metric_snapshot=request.deployment_metadata,
            )
        )
        session.flush()
    return experiment
