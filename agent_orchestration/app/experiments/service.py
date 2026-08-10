"""Agent Orchestration 실험 생성·조회·상태 쓰기와 이슈 발행 유스케이스를 제공한다.

전체 파이프라인에서 검증된 API 입력을 SQLAlchemy transaction으로 실험·event·log·metadata에
반영하는 구간과, 호출자가 제출한 사전등록 필드를 `[AR]` 이슈로 발행하는 조립→저장→발행
절차를 담당한다. API wrapper가 transaction을 소유하는 기존 경계와 launcher가 이미 연
transaction에 상태·event 쓰기를 합치는 primitive를 함께 제공한다. 발행 전에
baseline-reader App으로 `dev` SHA를 읽어 본문·제목과 함께 조건부 UPDATE로 최초 값만
봉인한다. HTTP 인증·상태 코드 변환, 실제 학습/Job 실행, 본문 조립(issue_authoring)과
GitHub 인증·REST/`gh` 전송 자체는 담당하지 않는다.

완주 보고가 실은 `report_markdown`은 정규화(`normalize_report_markdown`, 거절 아닌 절단)
후 지표 커밋과 **다른 트랜잭션**(`_store_report_markdown`)에 적재한다. 리포트 쓰기 실패가
이미 커밋된 지표를 되돌리면 안 되기 때문이다. 조회는 `get_experiment_report`가 담당하며,
실험은 있지만 리포트가 아직 없는 경우를 오류가 아닌 `None`으로 구별해 돌려준다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_orchestration.app.experiments.exceptions import (
    CandidateConflictError,
    ExperimentNotFoundError,
    ExperimentStepNotFoundError,
    IdempotencyConflictError,
    IssuePublicationLimitError,
    PromotionRequiresDedicatedEndpointError,
    StepAlreadyFinalizedError,
)
from agent_orchestration.app.experiments.github_issues import (
    GitHubIssueError,
    create_issue,
    find_issue_by_marker,
)
from agent_orchestration.app.experiments.issue_authoring import (
    build_issue_body,
    build_issue_title,
    marker_for,
)
from agent_orchestration.app.experiments.models import (
    Experiment,
    ExperimentEvent,
    ExperimentLog,
    ExperimentMetadata,
    ExperimentStatus,
    ExperimentStep,
    StepKind,
    TERMINAL_STEP_STATUSES,
)
from agent_orchestration.app.experiments.repository import (
    find_experiment,
    find_experiment_events,
    find_experiment_report,
    find_event_by_idempotency_key,
    find_experiment_logs,
    find_experiment_metadata,
    find_experiment_step,
    find_experiment_steps,
    find_experiments,
    find_log_by_idempotency_key,
    find_step_by_idempotency_key,
)
from agent_orchestration.app.experiments.schemas import (
    CandidateReportRequest,
    ExecutorResultReportRequest,
    ExperimentCreate,
    ExperimentEventCreate,
    ExperimentLogCreate,
    ExperimentStepCreate,
    ExperimentStepUpdate,
    IssuePublicationRequest,
    MAX_REPORT_MARKDOWN_BYTES,
    PromotionRequest,
    StatusUpdateRequest,
)
from agent_orchestration.app.experiments.transition_service import validate_transition
from agent_orchestration.github_app import (
    GitHubAppCredentials,
    GitHubAppError,
    create_installation_token,
)
from agent_orchestration.github_refs import GitHubRefError, GitHubRefs

logger = logging.getLogger(__name__)

# service가 상한을 넘겨 자를 때 본문 끝에 남기는 고정 문구다. executor의 문구와 문안을
# 다르게 두어 **어느 계층이 잘랐는지**가 화면에서 구분되게 한다.
_REPORT_TRUNCATION_NOTE = "\n\n[하네스] 리포트가 상한을 넘어 API에서 잘렸습니다.\n"


def normalize_report_markdown(text: str) -> str:
    """DB에 저장할 수 있는 형태로 리포트 본문을 정규화한다.

    **거절하지 않는다.** 이 함수가 예외를 올리면 그 예외가 완주 보고를 죽이고, 그것은
    "리포트는 지표에 종속된다"는 계약과 정반대다(spec 결정 3).

    NUL을 지우는 이유는 PostgreSQL이 text 값에 `U+0000`을 저장하지 못하기 때문이다.
    `report.md`는 `read_text(errors="replace")`로 읽히는데 그 옵션은 잘못된 UTF-8만
    바꿀 뿐 정상 디코드되는 0x00은 그대로 통과시킨다.
    """
    cleaned = text.replace("\x00", "")
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= MAX_REPORT_MARKDOWN_BYTES:
        return cleaned
    budget = MAX_REPORT_MARKDOWN_BYTES - len(_REPORT_TRUNCATION_NOTE.encode("utf-8"))
    return encoded[:budget].decode("utf-8", errors="ignore") + _REPORT_TRUNCATION_NOTE


# 학습 기간은 KST 날짜 경계로 계산한다. UTC로 계산하면 한국 시각 오전 9시 이전에
# 발행된 실험이 하루 앞선 구간을 보게 된다.


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


@dataclass(frozen=True)
class ExperimentStepPageResult:
    """polling용 Step page와 다음 cursor."""

    items: list[ExperimentStep]
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


def get_experiment_report(
    session: Session,
    experiment_id: uuid.UUID,
) -> str | None:
    """존재하는 실험의 리포트 본문을 반환한다.

    실험이 없으면 `ExperimentNotFoundError`다. 실험은 있고 리포트가 없으면 `None`이며
    그것은 오류가 아니다 — 완주 전 실험, 리포트를 끄고 돌린 배포, Codex가 실패한
    실행이 모두 여기 해당한다.
    """
    experiment = find_experiment_report(session, experiment_id)
    if experiment is None:
        raise ExperimentNotFoundError(experiment_id)
    return experiment.report_markdown


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


def transition_experiment_in_transaction(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    requested: ExperimentStatus,
    reason: str | None,
    metric_snapshot: dict | None,
    idempotency_key: str,
    check_idempotency: bool,
) -> tuple[Experiment, ExperimentEvent]:
    """호출자가 연 transaction 안에서 상태와 event를 원자적으로 갱신한다.

    이 함수는 commit이나 rollback을 수행하지 않는다. API service wrapper와 launcher처럼
    추가 쓰기를 같은 원자 단위로 묶어야 하는 호출자가 transaction 수명을 소유한다.
    """
    _require_general_transition(requested)
    request_fingerprint = _request_fingerprint(
        {
            "to_status": requested.value,
            "reason": reason,
            "metric_snapshot": metric_snapshot,
        }
    )
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


def _transition_experiment(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    requested: ExperimentStatus,
    reason: str | None,
    metric_snapshot: dict | None,
    idempotency_key: str,
    check_idempotency: bool,
) -> tuple[Experiment, ExperimentEvent]:
    """기존 service 호출을 위해 transaction을 열고 공용 전이 primitive를 호출한다."""
    with session.begin():
        return transition_experiment_in_transaction(
            session,
            experiment_id,
            requested=requested,
            reason=reason,
            metric_snapshot=metric_snapshot,
            idempotency_key=idempotency_key,
            check_idempotency=check_idempotency,
        )


def update_experiment_status(
    session: Session,
    experiment_id: uuid.UUID,
    request: StatusUpdateRequest,
) -> Experiment:
    """클라이언트 멱등성을 제공하지 않는 일반 상태 변경을 수행한다."""
    requested = ExperimentStatus(request.status)
    experiment, _event = _transition_experiment(
        session,
        experiment_id,
        requested=requested,
        reason=request.reason,
        metric_snapshot=request.metric_snapshot,
        idempotency_key=f"status-update:{uuid.uuid4()}",
        check_idempotency=False,
    )
    return experiment


def record_candidate(
    session: Session,
    experiment_id: uuid.UUID,
    request: CandidateReportRequest,
) -> Experiment:
    """검증된 candidate SHA를 한 번 기록하고 EVALUATING으로 원자 전이한다.

    executor가 제출한 이슈·branch·baseline 좌표가 발행 시 봉인한 Experiment와 같은지
    확인한다. 같은 candidate 보고의 재시도는 기존 event를 먼저 조회해, 이미
    EVALUATING 상태여도 성공으로 반환한다.
    """
    payload = {
        "issue_number": request.issue_number,
        "issue_branch": request.issue_branch,
        "base_dev_sha": request.base_dev_sha,
        "candidate_sha": request.candidate_sha,
    }
    fingerprint = _request_fingerprint(payload)
    expected_event_key = f"executor-candidate:{experiment_id}"
    if request.idempotency_key != expected_event_key:
        raise CandidateConflictError()
    event_key = request.idempotency_key
    with session.begin():
        experiment = find_experiment(session, experiment_id, for_update=True)
        if experiment is None:
            raise ExperimentNotFoundError(experiment_id)

        existing_event = find_event_by_idempotency_key(session, experiment_id, event_key)
        if existing_event is not None:
            if existing_event.request_fingerprint != fingerprint:
                raise IdempotencyConflictError(event_key)
            return experiment

        if (
            experiment.candidate_sha not in (None, request.candidate_sha)
            or experiment.issue_number != request.issue_number
            or experiment.issue_branch != request.issue_branch
            or experiment.base_dev_sha != request.base_dev_sha
        ):
            raise CandidateConflictError()

        current = ExperimentStatus(experiment.status)
        validate_transition(current, ExperimentStatus.EVALUATING)
        experiment.candidate_sha = request.candidate_sha
        experiment.status = ExperimentStatus.EVALUATING.value
        session.add(
            ExperimentEvent(
                experiment_id=experiment.id,
                idempotency_key=event_key,
                request_fingerprint=fingerprint,
                from_status=current.value,
                to_status=ExperimentStatus.EVALUATING.value,
            )
        )
        session.flush()
    return experiment


# 상태만으로는 "무엇이 끝났는지"를 알 수 없어 타임라인에 남기는 고정 문구다. 호출자가
# 문자열을 실어 보내지 않는 이유는 event `reason`이 워크벤치에 그대로 표시되기 때문이다 —
# executor는 지표를 보고하지, 화면에 쓸 문장을 정하지 않는다.
EXECUTOR_RESULT_REASON = "executor completed the experiment and reported metrics"


def record_experiment_result(
    session: Session,
    experiment_id: uuid.UUID,
    request: ExecutorResultReportRequest,
) -> Experiment:
    """완주한 실험의 지표를 저장하고 EVALUATING에서 PASSED로 원자 전이한다.

    `PASSED`는 **실험이 완주하고 결과가 나왔다**는 뜻이다(계약 결정 6). 가설의 성패는
    상태가 아니라 `report.md`가 서술하므로 이 endpoint는 도달할 상태를 인자로 받지
    않는다.

    이미 저장된 `candidate_sha`와 대조해 다른 실행의 결과를 받지 않는다. 같은 보고의
    재시도는 event 조회로 흡수되지만, **같은 실험에 다른 지표를 보내면 409**다 —
    한 실험의 결과는 하나이고, 덮어쓰기를 허용하면 어느 숫자가 실제 실행의 것인지
    말할 수 없게 된다.

    `report_markdown`은 **다른 트랜잭션**에 쓴다. 리포트 쓰기 실패가 지표 커밋을 되돌리면
    안 되기 때문이며, 그 근거는 `_store_report_markdown`에 있다.
    """
    expected_event_key = f"executor-result:{experiment_id}"
    if request.idempotency_key != expected_event_key:
        raise CandidateConflictError()
    with session.begin():
        experiment = find_experiment(session, experiment_id, for_update=True)
        if experiment is None:
            raise ExperimentNotFoundError(experiment_id)
        # candidate 보고가 선행하므로 `candidate_sha`는 이미 있어야 한다. 비어 있으면
        # 순서가 어긋난 것이고, 다르면 다른 실행의 산출물이다.
        if experiment.candidate_sha != request.candidate_sha:
            raise CandidateConflictError()
        transition_experiment_in_transaction(
            session,
            experiment_id,
            requested=ExperimentStatus.PASSED,
            reason=EXECUTOR_RESULT_REASON,
            metric_snapshot=request.metric_snapshot,
            idempotency_key=expected_event_key,
            check_idempotency=True,
        )
    # 지표는 위에서 이미 커밋됐다. 리포트는 여기서부터 독립이다 — 이 아래에서 무엇이
    # 터져도 완주 보고는 성립한다.
    if request.report_markdown is not None:
        _store_report_markdown(session, experiment_id, request.report_markdown)
    return experiment


def _store_report_markdown(
    session: Session, experiment_id: uuid.UUID, raw_markdown: str
) -> None:
    """리포트 본문을 지표 커밋과 **다른 트랜잭션**에 쓴다.

    같은 트랜잭션에 두면 리포트 쓰기 실패가 지표까지 롤백시킨다. 그 실패는 가상이
    아니다 — PostgreSQL의 NUL 거부와, migration `0006` 이전에 코드가 뜬 배포 순서
    어긋남(deferred 컬럼이라 SELECT는 통과하고 UPDATE에서 터진다) 두 경로가 있다.

    **어떤 예외도 위로 올리지 않는다.** 여기서 예외가 나가면 이미 커밋된 지표 보고가
    200에서 500으로 바뀌고, executor는 그것을 실패로 읽어 Job이 ERROR로 회수된다.
    `with session.begin()`의 `__exit__`가 이미 rollback하므로 명시적 rollback은 넣지
    않는다(`_transition_experiment` 호출부의 주석과 같은 근거).

    **write-once.** 이미 값이 있으면 덮어쓰지 않는다. 지표는 다르면 409지만 리포트는
    그렇게 하지 않는다 — 재시도가 리포트 때문에 실패하면 지표 보고까지 잃는다. 대신
    다른 본문이 왔다는 사실은 로그에 남긴다. 본문 자체는 싣지 않는다(최대 64KB의 LLM
    산출물이다).
    """
    try:
        normalized = normalize_report_markdown(raw_markdown)
        if normalized != raw_markdown:
            logger.warning(
                "report_markdown normalized experiment_id=%s", experiment_id
            )
        with session.begin():
            experiment = find_experiment_report(session, experiment_id, for_update=True)
            if experiment is None:
                return
            if experiment.report_markdown is None:
                experiment.report_markdown = normalized
            elif experiment.report_markdown != normalized:
                logger.warning(
                    "report_markdown ignored: already set, mismatch on retry "
                    "experiment_id=%s",
                    experiment_id,
                )
    except Exception as error:  # noqa: BLE001 - 리포트 실패가 지표 커밋을 되돌리면 안 된다
        logger.error(
            "report_markdown write failed experiment_id=%s error_type=%s",
            experiment_id,
            type(error).__name__,
        )


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
        step = find_experiment_step(session, experiment_id, step_id)
        if step is None:
            raise ExperimentStepNotFoundError(step_id)

        # 조건을 **모든** 갱신에 건다. 터미널로 전이할 때만 걸면 두 가지가 새어 나간다.
        #   1) 검사-후-실행 사이에 다른 트랜잭션이 터미널을 확정하는 창
        #   2) 세션이 expire_on_commit=False라, 위 SELECT가 identity map의 stale 객체를
        #      돌려줄 수 있다. stale 값이 비터미널이면 확정된 Step을 조용히 덮어쓴다.
        # 비터미널 사이의 갱신은 이 조건에 걸리지 않으므로 "비터미널 자유 전이"는 그대로다.
        result = session.execute(
            update(ExperimentStep)
            .where(
                # experiment_id를 함께 건다 — 위 존재 확인과 같은 조건이라야 확인과 실행이
                # 한 곳에서 자명하고, 존재 확인이 옮겨지거나 캐시돼도 교차 실험 갱신이 열리지
                # 않는다. `rowcount == 0`의 의미는 "저장된 row가 이미 터미널"로 유지된다.
                ExperimentStep.experiment_id == experiment_id,
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
        # 방금 다른 트랜잭션이 커밋한 값을 본다. 위에서 받은 객체를 그대로 쓴다 — 같은
        # 조건으로 다시 SELECT해도 identity map이 같은 객체를 돌려주므로 왕복만 늘어난다.
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


def list_experiment_steps(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    limit: int,
    after_id: uuid.UUID | None = None,
    step_kind: StepKind | None = None,
) -> ExperimentStepPageResult:
    """created_at 우선·동률 시 UUID tie-breaker 순으로 정렬한 Step polling page를 반환한다.

    tie-breaker인 `gen_random_uuid()`는 insert 순서와 무관한 난수라, 동률에서는 실제
    append 순서를 보존하지 않는다(알려진 한계, spec의 "알려진 한계" 절 참고).
    """
    get_experiment(session, experiment_id)
    items = find_experiment_steps(
        session,
        experiment_id,
        limit=limit,
        after_id=after_id,
        step_kind=None if step_kind is None else step_kind.value,
    )
    return ExperimentStepPageResult(
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


async def resolve_dev_sha(settings: object) -> str:
    """baseline-reader App으로 현재 `heads/dev` SHA를 한 번 읽는다.

    App/REST 경계의 상세 오류는 모두 이미 정제되어 있지만, 기존 이슈 발행 HTTP handler가
    안정적으로 502로 변환할 수 있도록 발행 도메인의 안전한 사유로 다시 감싼다.
    """
    app_id = getattr(settings, "baseline_github_app_id", None)
    installation_id = getattr(settings, "baseline_github_app_installation_id", None)
    private_key_path = getattr(settings, "baseline_github_app_private_key_path", None)
    repository = getattr(settings, "github_repository", None)
    if (
        not isinstance(app_id, int)
        or app_id < 1
        or not isinstance(installation_id, int)
        or installation_id < 1
        or private_key_path is None
        or not isinstance(repository, str)
    ):
        raise GitHubIssueError("baseline_credentials_missing")
    try:
        token = await create_installation_token(
            GitHubAppCredentials(app_id, installation_id, private_key_path),
            permissions={"contents": "read"},
        )
        sha = await GitHubRefs().get_sha(
            repository,
            "heads/dev",
            token.value,
        )
    except (GitHubAppError, GitHubRefError) as error:
        raise GitHubIssueError("baseline_resolution_failed") from error
    if sha is None:
        raise GitHubIssueError("baseline_ref_not_found")
    return sha


def _branch_name_for(issue_number: int) -> str:
    """executor가 만들 브랜치 이름을 응답에 미리 싣는다.

    `tools/auto_research_issue_branch.py`의 `branch_name_for()`와 같은 규칙이다. 그
    모듈은 API 이미지에 없어 import할 수 없으므로 규칙을 복제한다 — 이 값은 표시용이며
    실제 브랜치는 launcher가 좌표를 전달한 executor Pod가 만든다. 동일성은
    `tests/test_experiment_issue_publication.py`의
    `test_branch_name_matches_the_canonical_rule`이 고정한다.

    제목은 쓰지 않는다(#589). 이슈 번호만으로 이미 유일하고, 제목 slug를 섞으면 브랜치
    이름 때문에 제목이 ASCII를 포함해야 해서 한글 제목이 발행에서 거부된다.
    """
    return f"exp/{issue_number}"


async def publish_experiment_issue(
    session: Session,
    settings: object,
    experiment_id: uuid.UUID,
    request: IssuePublicationRequest,
) -> Experiment:
    """사전등록 필드를 `[AR]` 이슈로 발행하고 lineage를 기록한다.

    본문은 ①에서 발행 **전에** 커밋한다. `대상 데이터 · 기간`이 발행 시점 KST 날짜로
    계산되므로, 저장하지 않으면 `gh` 실패 후 날짜가 바뀐 재발행이 다른 본문을 쓴다.
    저장해 두면 재발행이 같은 본문을 쓴다.
    """
    experiment = find_experiment(session, experiment_id)
    if experiment is None:
        raise ExperimentNotFoundError(experiment_id)

    # 멱등성 1차 — 이미 발행됐으면 아무것도 하지 않는다.
    if experiment.issue_number is not None:
        return experiment

    # `updated_at`으로 세면 안 된다 — `onupdate=func.now()`라 상태 전이·metric 기록 등
    # 발행과 무관한 UPDATE도 갱신하므로, 며칠 전 발행된 실험이 오늘 수정되면 "오늘
    # 발행"으로 잡혀 새 발행을 부당하게 막는다. 발행 시각 전용 컬럼을 쓴다.
    since = datetime.now(UTC) - timedelta(days=1)
    published_today = session.scalar(
        select(func.count())
        .select_from(Experiment)
        .where(Experiment.issue_published_at >= since)
    )
    if (published_today or 0) >= settings.issue_daily_limit:
        raise IssuePublicationLimitError(settings.issue_daily_limit)

    # ① 본문·제목·기준 SHA를 만들고 발행 전에 같은 transaction으로 커밋한다. 이
    # 커밋이 재시도 결정성의 근거다. 기준 SHA가 이미 있으면 최신 dev를 다시 읽지 않는다.
    # NULL을 본 요청이 여러 개여도 조건부 UPDATE 한 개만 성공하므로 최초 SHA는 이후
    # 요청에 덮이지 않는다. 외부 ref 조회는 UPDATE 전에 끝내 row lock을 잡은 채 GitHub를
    # 기다리지 않는다.
    stores_issue_definition = experiment.issue_body is None
    stores_baseline = experiment.base_dev_sha is None
    if stores_issue_definition:
        body = build_issue_body(experiment.id, request.fields)
        title = build_issue_title(request.fields)
    else:
        body = experiment.issue_body
        title = experiment.issue_title

    if stores_baseline:
        # 앞선 존재·상한 조회가 연 read transaction을 닫아 GitHub HTTP 대기 중 pool
        # connection을 점유하지 않는다. 아래 CAS가 NULL 조건을 다시 검사하므로 이 경계가
        # 기준 SHA의 최초 writer 불변식을 약화하지 않는다.
        session.commit()
        candidate_base_dev_sha = await resolve_dev_sha(settings)
    else:
        candidate_base_dev_sha = experiment.base_dev_sha
    if stores_baseline:
        # Core UPDATE가 새 transaction을 autobegin한다. WHERE 절에서 NULL 여부를 다시
        # 검사해야 PostgreSQL이 경쟁 writer 대기 후 최신 row에 조건을 재평가한다. ORM
        # 속성 대입은 stale NULL을 근거로 무조건 UPDATE해 먼저 봉인한 SHA를 덮을 수 있다.
        values: dict[str, object] = {"base_dev_sha": candidate_base_dev_sha}
        if stores_issue_definition:
            values.update(
                issue_body=body,
                # title도 같은 commit에 저장한다 — 저장하지 않으면 재발행 시 본문에서
                # 제목을 복원해야 하고, 그 복원 규칙(첫 문장 요약)이 호출자가 준 실제
                # title과 달라 재발행마다 제목이 흔들린다. 브랜치 이름은 이슈 번호에서만
                # 나오므로 제목에 걸려 있지 않다(#589).
                issue_title=title,
                issue_branch=None,
            )
        result = session.execute(
            update(Experiment)
            .where(
                Experiment.id == experiment.id,
                Experiment.base_dev_sha.is_(None),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        session.commit()

        # synchronize_session=False와 expire_on_commit=False이므로 identity map은 여전히
        # CAS 전 값을 가진다. 패배(rowcount=0) 요청은 반드시 DB 승자 값을 다시 읽어야
        # 하며, 승자도 같은 refresh 경로를 써 반환 객체가 실제 저장값과 일치하게 한다.
        session.refresh(experiment)
        session.commit()
        if result.rowcount == 0:
            base_dev_sha = experiment.base_dev_sha
        else:
            base_dev_sha = candidate_base_dev_sha
        if base_dev_sha is None:
            raise GitHubIssueError("baseline_freeze_failed")
        body = experiment.issue_body
        title = experiment.issue_title
    elif stores_issue_definition:
        # 기준 SHA만 이미 봉인된 legacy 행도 최초 본문·제목만 이긴다. ORM 대입은 동시
        # 요청의 마지막 commit이 먼저 저장된 정의를 덮을 수 있으므로 NULL-only CAS를 쓴다.
        session.execute(
            update(Experiment)
            .where(
                Experiment.id == experiment.id,
                Experiment.issue_body.is_(None),
            )
            .values(
                issue_body=body,
                issue_title=title,
                issue_branch=None,
            )
            .execution_options(synchronize_session=False)
        )
        session.commit()
        session.refresh(experiment)
        session.commit()
        body = experiment.issue_body
        title = experiment.issue_title

    if not isinstance(body, str) or not isinstance(title, str):
        raise GitHubIssueError("issue_definition_freeze_failed")

    # ② 발행.
    # gh 성공 후 응답이 소실된 경우를 위해 marker를 먼저 조회한다 — 멱등성
    # 3중 방어의 3번째 층이다. 이 조회가 실패하면 "발행되지 않았다"가 아니라
    # "발행됐는지 알 수 없다"이므로, 예외를 삼키고 create_issue로 넘어가면 이 층이
    # 없는 것과 같아져 중복 이슈를 만들 수 있다. 그래서 예외를 그대로 올려 요청을
    # 실패시킨다 — 호출자는 사유(예: `authentication_failed`)를 보고 무엇을 고칠지
    # 안다. 중복 이슈를 만드는 것보다 사람이 보는 편이 낫다.
    # 이미 기준선·본문이 봉인된 재시도도 앞선 존재·상한 조회로 read transaction이
    # 열려 있으므로, marker 조회와 create_issue의 외부 대기 전에 공통으로 닫는다.
    session.commit()
    existing = await find_issue_by_marker(settings, marker=marker_for(experiment.id))
    reference = existing or await create_issue(
        settings, title=title, body=body, labels=(TRIGGER_LABEL,)
    )

    experiment.issue_number = reference.number
    experiment.issue_branch = _branch_name_for(reference.number)
    experiment.issue_published_at = datetime.now(UTC)
    session.commit()
    return experiment
