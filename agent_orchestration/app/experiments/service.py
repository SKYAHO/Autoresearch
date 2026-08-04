"""Agent Orchestration 실험 생성·조회·상태 쓰기와 이슈 발행 유스케이스를 제공한다.

전체 파이프라인에서 검증된 API 입력을 SQLAlchemy transaction으로 실험·event·log·metadata에
반영하는 구간과, 가설을 `[AR]` 이슈로 발행하는 생성→저장→발행 2단계 절차를 담당한다.
HTTP 인증·상태 코드 변환, 실제 학습 실행, 본문 조립(issue_authoring)과 `gh` 호출
(github_issues) 자체는 담당하지 않는다.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_orchestration.app.experiments.exceptions import (
    ExperimentNotFoundError,
    IdempotencyConflictError,
    IssuePublicationLimitError,
    PromotionRequiresDedicatedEndpointError,
)
from agent_orchestration.app.experiments.github_issues import (
    create_issue,
    find_issue_by_marker,
)
from agent_orchestration.app.experiments.issue_authoring import (
    build_issue_body,
    build_issue_title,
    build_prompt,
    marker_for,
    parse_llm_fields,
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
    IssuePublicationRequest,
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


TRIGGER_LABEL = "auto-experiment"


def _branch_name_for(issue_number: int, title: str) -> str:
    """워크플로가 만들 브랜치 이름을 응답에 미리 싣는다.

    `tools/auto_research_issue_branch.py`의 `branch_name_for()`와 같은 규칙이다. 그
    모듈은 API 이미지에 없어 import할 수 없으므로 규칙을 복제한다 — 이 값은 표시용이며
    실제 브랜치는 워크플로가 만든다. 동일성은 `tests/test_experiment_issue_publication.py`의
    `test_branch_name_matches_the_workflow_rule`이 고정한다.
    """
    title_without_prefix = re.sub(r"^\s*\[AR\]\s*", "", title, flags=re.IGNORECASE)
    slug = re.sub(r"[^a-z0-9]+", "-", title_without_prefix.lower()).strip("-")
    if not slug:
        digest = hashlib.sha256(title_without_prefix.encode("utf-8")).hexdigest()[:12]
        slug = f"issue-{digest}"
    return f"exp/{issue_number}-{slug}"


async def publish_experiment_issue(
    session: Session,
    settings: object,
    experiment_id: uuid.UUID,
    request: IssuePublicationRequest,
    *,
    generate: Callable[[str], Awaitable[str]],
) -> Experiment:
    """가설을 `[AR]` 이슈로 발행하고 lineage를 기록한다.

    LLM은 비결정적이라, 발행 실패 후 재호출이 LLM을 다시 부르면 실험 정의(criteria_id·
    reproducibility_id의 근거가 되는 지표·guardrail 값)가 바뀔 수 있다. 그래서 본문을
    ①에서 발행 전에 커밋해 이 커밋을 경계로 앞쪽 실패는 재생성, 뒤쪽 실패는 재발행으로
    가른다.
    """
    experiment = find_experiment(session, experiment_id)
    if experiment is None:
        raise ExperimentNotFoundError(experiment_id)

    # 멱등성 1차 — 이미 발행됐으면 아무것도 하지 않는다. regenerate보다 우선한다.
    if experiment.issue_number is not None:
        return experiment

    since = datetime.now(UTC) - timedelta(days=1)
    published_today = session.scalar(
        select(func.count())
        .select_from(Experiment)
        .where(Experiment.issue_number.is_not(None), Experiment.updated_at >= since)
    )
    if (published_today or 0) >= settings.issue_daily_limit:
        raise IssuePublicationLimitError(settings.issue_daily_limit)

    # ① 본문을 만들고 발행 전에 커밋한다. 이 커밋이 재시도 결정성의 근거다.
    if experiment.issue_body is None or request.regenerate:
        response = await generate(build_prompt(experiment.hypothesis))
        fields = parse_llm_fields(response)
        body = build_issue_body(
            experiment.id,
            fields,
            settings.experiment_defaults,
            allowed_scope=request.allowed_scope,
        )
        title = build_issue_title(fields)
        # 이 시점에는 위 조회들이 autobegin으로 이미 transaction을 열어 두었으므로
        # `with session.begin():`을 다시 쓰면 "이미 시작된 transaction" 오류가 난다.
        # commit()으로 그 transaction을 끝맺는다 — 다음 statement가 필요하면 새
        # transaction을 autobegin한다. `expire_on_commit=False`(database.py)라 커밋
        # 후에도 방금 대입한 속성값을 그대로 읽을 수 있어 refresh가 필요하지 않다 —
        # refresh는 새 SELECT를 던져 또 다른 autobegin을 열어 두므로 오히려 다음
        # 호출자의 `session.begin()`과 충돌한다.
        experiment.issue_body = body
        experiment.issue_branch = None
        session.commit()
    else:
        body = experiment.issue_body
        title = _title_from_body(body, experiment.hypothesis)

    # ② 발행. gh 성공 후 응답이 소실된 경우를 위해 marker를 먼저 조회한다 — 멱등성
    # 3중 방어의 3번째 층이다. 이 조회가 실패하면 "발행되지 않았다"가 아니라
    # "발행됐는지 알 수 없다"이므로, 예외를 삼키고 create_issue로 넘어가면 이 층이
    # 없는 것과 같아져 중복 이슈를 만들 수 있다. 그래서 예외를 그대로 올려 요청을
    # 실패시킨다 — 호출자는 사유(예: `authentication_failed`)를 보고 무엇을 고칠지
    # 안다. 중복 이슈를 만드는 것보다 사람이 보는 편이 낫다.
    existing = await find_issue_by_marker(settings, marker=marker_for(experiment.id))
    reference = existing or await create_issue(
        settings, title=title, body=body, labels=(TRIGGER_LABEL,)
    )

    experiment.issue_number = reference.number
    experiment.issue_branch = _branch_name_for(reference.number, title)
    session.commit()
    return experiment


def _title_from_body(body: str, fallback: str) -> str:
    """저장된 본문으로 재발행할 때 제목을 복원한다."""
    match = re.search(r"^### 연구 가설\n(.+)$", body, re.MULTILINE)
    return f"[AR] {match.group(1).strip() if match else fallback.strip()}"
