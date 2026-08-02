"""Agent Orchestration 실험 워크벤치의 FastAPI HTTP 경계를 제공한다.

전체 파이프라인에서 Streamlit 워크벤치와 Agent가 Experiment service를 호출하는
HTTP·OpenAPI 경계를 담당한다. 인증 구현, SQLAlchemy transaction 세부사항과 상태 전이
판단은 각각 app 조립부와 service 계층의 책임이다.
"""

from __future__ import annotations

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from agent_orchestration.app.database import get_db_session
from agent_orchestration.app.experiments.models import ExperimentStatus
from agent_orchestration.app.experiments.schemas import (
    ExperimentCreate,
    ExperimentEventCreate,
    ExperimentEventPageResponse,
    ExperimentEventResponse,
    ExperimentLogCreate,
    ExperimentLogPageResponse,
    ExperimentLogResponse,
    ExperimentMetadataResponse,
    ExperimentPageResponse,
    ExperimentResponse,
    PromotionRequest,
    StatusUpdateRequest,
)
from agent_orchestration.app.experiments.service import (
    create_experiment,
    create_experiment_event,
    create_experiment_log,
    get_experiment,
    get_experiment_metadata,
    list_experiment_events,
    list_experiment_logs,
    list_experiments,
    promote_experiment,
    update_experiment_status,
)
from agent_orchestration.app.schemas import ErrorResponse


router = APIRouter(prefix="/experiments", tags=["experiments"])
SessionDependency = Annotated[Session, Depends(get_db_session)]
_UNAUTHORIZED_RESPONSE = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "Invalid orchestration API token.",
        "model": ErrorResponse,
    }
}
_NOT_FOUND_RESPONSE = {
    status.HTTP_404_NOT_FOUND: {"description": "Experiment was not found.", "model": ErrorResponse}
}
_CONFLICT_RESPONSE = {
    status.HTTP_409_CONFLICT: {"description": "Experiment state or idempotency conflict.", "model": ErrorResponse}
}


@router.post(
    "",
    response_model=ExperimentResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_UNAUTHORIZED_RESPONSE,
)
def post_experiment(request: ExperimentCreate, session: SessionDependency) -> ExperimentResponse:
    """새 실험과 최초 Event·metadata를 생성한다."""
    return ExperimentResponse.model_validate(create_experiment(session, request))


@router.get("", response_model=ExperimentPageResponse, responses=_UNAUTHORIZED_RESPONSE)
def get_experiments(
    session: SessionDependency,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    status_filter: ExperimentStatus | None = Query(default=None, alias="status"),
) -> ExperimentPageResponse:
    """상태 필터와 offset pagination을 적용해 실험을 조회한다."""
    page = list_experiments(session, limit=limit, offset=offset, status=status_filter)
    return ExperimentPageResponse(
        items=[ExperimentResponse.model_validate(item) for item in page.items],
        total=page.total,
        limit=limit,
        offset=offset,
    )


@router.get("/{experiment_id}", response_model=ExperimentResponse, responses={**_UNAUTHORIZED_RESPONSE, **_NOT_FOUND_RESPONSE})
def get_experiment_by_id(experiment_id: uuid.UUID, session: SessionDependency) -> ExperimentResponse:
    """실험의 최신 상태를 조회한다."""
    return ExperimentResponse.model_validate(get_experiment(session, experiment_id))


@router.patch(
    "/{experiment_id}/status",
    response_model=ExperimentResponse,
    responses={**_UNAUTHORIZED_RESPONSE, **_NOT_FOUND_RESPONSE, **_CONFLICT_RESPONSE},
)
def patch_experiment_status(
    experiment_id: uuid.UUID,
    request: StatusUpdateRequest,
    session: SessionDependency,
) -> ExperimentResponse:
    """PROMOTED를 제외한 일반 상태 전이를 수행한다."""
    return ExperimentResponse.model_validate(update_experiment_status(session, experiment_id, request))


@router.post(
    "/{experiment_id}/events",
    response_model=ExperimentEventResponse,
    status_code=status.HTTP_201_CREATED,
    responses={**_UNAUTHORIZED_RESPONSE, **_NOT_FOUND_RESPONSE, **_CONFLICT_RESPONSE},
)
def post_experiment_event(
    experiment_id: uuid.UUID,
    request: ExperimentEventCreate,
    session: SessionDependency,
) -> ExperimentEventResponse:
    """멱등 상태 Event를 추가한다."""
    return ExperimentEventResponse.model_validate(create_experiment_event(session, experiment_id, request))


@router.get(
    "/{experiment_id}/events",
    response_model=ExperimentEventPageResponse,
    responses={**_UNAUTHORIZED_RESPONSE, **_NOT_FOUND_RESPONSE},
)
def get_experiment_events(
    experiment_id: uuid.UUID,
    session: SessionDependency,
    limit: int = Query(default=100, ge=1, le=200),
    after_id: uuid.UUID | None = Query(default=None),
) -> ExperimentEventPageResponse:
    """1초 polling에 쓸 새 Event page를 조회한다."""
    page = list_experiment_events(session, experiment_id, limit=limit, after_id=after_id)
    return ExperimentEventPageResponse(
        items=[ExperimentEventResponse.model_validate(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.post(
    "/{experiment_id}/logs",
    response_model=ExperimentLogResponse,
    status_code=status.HTTP_201_CREATED,
    responses={**_UNAUTHORIZED_RESPONSE, **_NOT_FOUND_RESPONSE, **_CONFLICT_RESPONSE},
)
def post_experiment_log(
    experiment_id: uuid.UUID,
    request: ExperimentLogCreate,
    session: SessionDependency,
) -> ExperimentLogResponse:
    """상태와 무관한 멱등 실행 Log를 추가한다."""
    return ExperimentLogResponse.model_validate(create_experiment_log(session, experiment_id, request))


@router.get(
    "/{experiment_id}/logs",
    response_model=ExperimentLogPageResponse,
    responses={**_UNAUTHORIZED_RESPONSE, **_NOT_FOUND_RESPONSE},
)
def get_experiment_logs(
    experiment_id: uuid.UUID,
    session: SessionDependency,
    limit: int = Query(default=100, ge=1, le=100),
    after_id: uuid.UUID | None = Query(default=None),
    log_type: str | None = Query(default=None, min_length=1, max_length=32),
) -> ExperimentLogPageResponse:
    """1초 polling에 쓸 새 Log page를 조회한다."""
    page = list_experiment_logs(
        session,
        experiment_id,
        limit=limit,
        after_id=after_id,
        log_type=log_type,
    )
    return ExperimentLogPageResponse(
        items=[ExperimentLogResponse.model_validate(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


@router.get(
    "/{experiment_id}/metadata",
    response_model=ExperimentMetadataResponse,
    responses={**_UNAUTHORIZED_RESPONSE, **_NOT_FOUND_RESPONSE},
)
def get_experiment_metadata_by_id(
    experiment_id: uuid.UUID,
    session: SessionDependency,
) -> ExperimentMetadataResponse:
    """실험 metadata를 key-value mapping으로 반환한다."""
    return ExperimentMetadataResponse(entries=get_experiment_metadata(session, experiment_id))


@router.post(
    "/{experiment_id}/promote",
    response_model=ExperimentResponse,
    responses={**_UNAUTHORIZED_RESPONSE, **_NOT_FOUND_RESPONSE, **_CONFLICT_RESPONSE},
)
def post_experiment_promotion(
    experiment_id: uuid.UUID,
    request: PromotionRequest,
    session: SessionDependency,
) -> ExperimentResponse:
    """운영 근거가 있는 PASSED 실험을 수동 승격한다."""
    return ExperimentResponse.model_validate(promote_experiment(session, experiment_id, request))
