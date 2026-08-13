"""Executor 보고용 FastAPI 내부 HTTP 경계를 제공한다.

전체 파이프라인에서 원격 Git 검증을 마친 executor가 candidate SHA를 보고해 평가 단계로
넘기는 구간과, 채점이 끝난 뒤 실험 지표를 보고해 완주를 확정하는 구간의 내부 API를
담당한다. 일반 Experiment workbench API는 `router.py`, 인증 구현은 `app.main`,
transaction·상태 전이는 `service.py`의 책임이다.
"""

from __future__ import annotations

from typing import Annotated
import uuid

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from agent_orchestration.app.database import get_db_session
from agent_orchestration.app.experiments.schemas import (
    CandidateReportRequest,
    ExecutorResultReportRequest,
    ExperimentResponse,
)
from agent_orchestration.app.experiments.service import (
    record_candidate,
    record_experiment_result,
)
from agent_orchestration.app.schemas import ErrorResponse


router = APIRouter(prefix="/internal/executor/experiments", tags=["executor"])
SessionDependency = Annotated[Session, Depends(get_db_session)]
_UNAUTHORIZED_RESPONSE = {
    status.HTTP_401_UNAUTHORIZED: {
        "description": "Invalid orchestration executor API token.",
        "model": ErrorResponse,
    }
}
_NOT_FOUND_RESPONSE = {
    status.HTTP_404_NOT_FOUND: {"description": "Experiment was not found.", "model": ErrorResponse}
}
_CONFLICT_RESPONSE = {
    status.HTTP_409_CONFLICT: {
        "description": "Experiment state or idempotency conflict.",
        "model": ErrorResponse,
    }
}


@router.post(
    "/{experiment_id}/candidate",
    response_model=ExperimentResponse,
    responses={**_UNAUTHORIZED_RESPONSE, **_NOT_FOUND_RESPONSE, **_CONFLICT_RESPONSE},
)
def post_executor_candidate(
    experiment_id: uuid.UUID,
    request: CandidateReportRequest,
    session: SessionDependency,
) -> ExperimentResponse:
    """검증된 candidate SHA를 저장하고 RUNNING에서 EVALUATING으로 전이한다."""
    return ExperimentResponse.model_validate(record_candidate(session, experiment_id, request))


@router.post(
    "/{experiment_id}/result",
    response_model=ExperimentResponse,
    responses={**_UNAUTHORIZED_RESPONSE, **_NOT_FOUND_RESPONSE, **_CONFLICT_RESPONSE},
)
def post_executor_result(
    experiment_id: uuid.UUID,
    request: ExecutorResultReportRequest,
    session: SessionDependency,
) -> ExperimentResponse:
    """완주한 실험의 지표를 저장하고 EVALUATING에서 PASSED로 전이한다."""
    return ExperimentResponse.model_validate(
        record_experiment_result(session, experiment_id, request)
    )
