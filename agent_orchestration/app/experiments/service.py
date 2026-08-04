"""Agent Orchestration 실험 생성·조회와 상태 쓰기 유스케이스를 제공한다.

전체 파이프라인에서 검증된 API 입력을 SQLAlchemy transaction으로 실험·event·log·metadata에
반영하는 구간을 담당한다. HTTP 인증·상태 코드 변환과 실제 학습 실행은 담당하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import uuid

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_orchestration.app.experiments.exceptions import (
    ExperimentNotFoundError,
    ExperimentStepNotFoundError,
    IdempotencyConflictError,
    PromotionRequiresDedicatedEndpointError,
    StepAlreadyFinalizedError,
)
from agent_orchestration.app.experiments.models import (
    Experiment,
    ExperimentEvent,
    ExperimentLog,
    ExperimentMetadata,
    ExperimentStatus,
    ExperimentStep,
    TERMINAL_STEP_STATUSES,
)
from agent_orchestration.app.experiments.repository import (
    find_experiment,
    find_experiment_events,
    find_event_by_idempotency_key,
    find_experiment_logs,
    find_experiment_metadata,
    find_experiment_step,
    find_experiments,
    find_log_by_idempotency_key,
    find_step_by_idempotency_key,
)
from agent_orchestration.app.experiments.schemas import (
    ExperimentCreate,
    ExperimentEventCreate,
    ExperimentLogCreate,
    ExperimentStepCreate,
    ExperimentStepUpdate,
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
    """created_at 우선·동률 시 UUID tie-breaker 순으로 정렬한 Event polling page를 반환한다.

    tie-breaker인 `gen_random_uuid()`는 insert 순서와 무관한 난수라, 동률에서는 실제
    append 순서를 보존하지 않는다(알려진 한계, spec의 "알려진 한계" 절 참고).
    """
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
        # _transition_experiment의 `with session.begin()`이 이미 __exit__에서 rollback을
        # 수행했으므로 이 rollback은 사실상 no-op이다. 이후 SELECT는 session의 새
        # implicit transaction을 연다.
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
        # 순서 고정: expunge를 rollback보다 먼저 호출해 existing_event를 detach한다.
        # rollback은 세션에 남은 객체를 expire시키므로, 순서가 바뀌면 이후
        # ExperimentEventResponse.model_validate(existing_event)가 새 SELECT 없이 이미
        # 로드된 컬럼만 읽는다는 전제가 깨지고, 이 스키마에 relationship 필드가 추가되면
        # detach된 객체의 lazy-load 시도가 DetachedInstanceError로 즉시 실패한다.
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
            raise error
        if existing_log.request_fingerprint != fingerprint:
            session.rollback()
            raise IdempotencyConflictError(request.idempotency_key) from error
        # expunge-before-rollback 순서 의존성은 create_experiment_event와 동일 (위 주석 참고).
        session.expunge(existing_log)
        session.rollback()
        return existing_log


def create_experiment_step(
    session: Session,
    experiment_id: uuid.UUID,
    request: ExperimentStepCreate,
) -> ExperimentStep:
    """실험 상태와 무관하게 멱등성이 보장되는 작업 단계를 추가한다.

    Step은 `experiments.status`를 변경하지 않으므로 `create_experiment_log`와 같이 row
    lock 없이 동작한다. 동시 요청의 최종 방어선은 unique constraint와 아래 IntegrityError
    복구다.
    """
    payload = {
        "step_kind": request.step_kind.value,
        "step_type": request.step_type,
        "status": request.status.value,
        "message": request.message,
        "target": request.target,
    }
    fingerprint = _request_fingerprint(payload)
    try:
        with session.begin():
            if find_experiment(session, experiment_id) is None:
                raise ExperimentNotFoundError(experiment_id)
            existing_step = find_step_by_idempotency_key(
                session,
                experiment_id,
                request.idempotency_key,
            )
            if existing_step is not None:
                if existing_step.request_fingerprint != fingerprint:
                    raise IdempotencyConflictError(request.idempotency_key)
                return existing_step
            step_row = ExperimentStep(
                experiment_id=experiment_id,
                idempotency_key=request.idempotency_key,
                request_fingerprint=fingerprint,
                step_kind=request.step_kind.value,
                step_type=request.step_type,
                status=request.status.value,
                message=request.message,
                target=request.target,
            )
            session.add(step_row)
            session.flush()
        return step_row
    except IntegrityError as error:
        session.rollback()
        existing_step = find_step_by_idempotency_key(
            session,
            experiment_id,
            request.idempotency_key,
        )
        if existing_step is None:
            raise error
        if existing_step.request_fingerprint != fingerprint:
            session.rollback()
            raise IdempotencyConflictError(request.idempotency_key) from error
        # expunge-before-rollback 순서 의존성은 create_experiment_event와 동일 (위 주석 참고).
        session.expunge(existing_step)
        session.rollback()
        return existing_step


def _step_state_fingerprint(step: ExperimentStep) -> str:
    """현재 저장된 Step 상태의 digest를 계산한다.

    저장된 `request_fingerprint` 컬럼을 쓰지 않는다 — 그 값은 **생성 시점** payload
    (`step_kind`·`step_type` 포함)의 digest라 key 집합이 다르다.
    """
    return _request_fingerprint(
        {"status": step.status, "message": step.message, "target": step.target}
    )


def _finalized_step_or_conflict(
    step: ExperimentStep,
    requested_fingerprint: str,
) -> ExperimentStep:
    """확정된 Step에 대한 재시도만 통과시키고 다른 payload는 거부한다."""
    if _step_state_fingerprint(step) == requested_fingerprint:
        return step
    raise StepAlreadyFinalizedError(step.id)


def update_experiment_step(
    session: Session,
    experiment_id: uuid.UUID,
    step_id: uuid.UUID,
    request: ExperimentStepUpdate,
) -> ExperimentStep:
    """작업 단계를 전체 교체로 갱신하고 터미널 확정을 원자적으로 보장한다.

    비터미널 사이의 전이는 자유롭게 허용한다. 터미널로 전이할 때만 조건부 UPDATE를 걸어
    검사-후-실행 사이의 창을 없앤다 — 그러지 않으면 두 요청이 동시에 서로 다른 터미널
    상태를 써도 둘 다 통과해 나중에 커밋한 쪽이 조용히 이긴다.
    """
    requested_fingerprint = _request_fingerprint(
        {
            "status": request.status.value,
            "message": request.message,
            "target": request.target,
        }
    )
    terminal_values = [status.value for status in TERMINAL_STEP_STATUSES]
    with session.begin():
        if find_experiment_step(session, experiment_id, step_id) is None:
            raise ExperimentStepNotFoundError(step_id)

        # 조건을 **모든** 갱신에 건다. 터미널로 전이할 때만 걸면 두 가지가 새어 나간다.
        #   1) 검사-후-실행 사이에 다른 트랜잭션이 터미널을 확정하는 창
        #   2) 세션이 expire_on_commit=False라, 위 SELECT가 identity map의 stale 객체를
        #      돌려줄 수 있다. stale 값이 비터미널이면 확정된 Step을 조용히 덮어쓴다.
        # 비터미널 사이의 갱신은 이 조건에 걸리지 않으므로 "비터미널 자유 전이"는 그대로다.
        result = session.execute(
            update(ExperimentStep)
            .where(
                ExperimentStep.id == step_id,
                ExperimentStep.status.not_in(terminal_values),
            )
            .values(
                status=request.status.value,
                message=request.message,
                target=request.target,
            )
            .execution_options(synchronize_session=False)
        )
        # 판정 근거는 세션 캐시가 아니라 새 SELECT여야 한다. refresh는 항상 SQL을 발행하므로
        # 방금 다른 트랜잭션이 커밋한 값을 본다.
        step = find_experiment_step(session, experiment_id, step_id)
        assert step is not None  # 같은 transaction 안에서 위 존재 확인을 통과했다
        session.refresh(step)
        if result.rowcount == 0:
            return _finalized_step_or_conflict(step, requested_fingerprint)
        return step


def list_experiment_logs(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    limit: int,
    after_id: uuid.UUID | None = None,
    log_type: str | None = None,
) -> ExperimentLogPageResult:
    """created_at 우선·동률 시 UUID tie-breaker 순으로 정렬한 Log polling page를 반환한다.

    tie-breaker인 `gen_random_uuid()`는 insert 순서와 무관한 난수라, 동률에서는 실제
    append 순서를 보존하지 않는다(알려진 한계, spec의 "알려진 한계" 절 참고).
    """
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
    # for_update=True로 experiment row를 잠그므로 PostgreSQL에서는 같은 idempotency_key의
    # 동시 promote 요청 대부분이 이 lock만으로 직렬화된다. 그런데 _transition_experiment도
    # 동일한 lock을 쓰면서 create_experiment_event/create_experiment_log는 여전히
    # IntegrityError 복구를 둔다 — lock이 이론적 상한을 보장하지 않는 이상(예: 연결 재시도로
    # 같은 요청이 서로 다른 트랜잭션으로 두 번 들어오는 경우) 세 경로 모두 같은 방어를
    # 두는 편이 일관적이라 promote에도 동일한 복구를 추가한다.
    try:
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
        # expunge-before-rollback 순서 의존성은 create_experiment_event와 동일 (해당 주석 참고).
        session.expunge(existing_event)
        session.rollback()
        experiment = get_experiment(session, experiment_id)
        return experiment
