"""Agent Orchestration 실험 API의 Pydantic 계약을 정의한다.

전체 파이프라인에서 FastAPI 호출자와 실험 service 사이의 입력·응답 형태를 검증한다.
DB query, 상태 전이와 인증은 담당하지 않는다.

실험 생성·조회, 일반 상태 event, polling log와 수동 승격에 필요한 엄격한 Pydantic v2
모델을 제공한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_orchestration.app.experiments.models import ExperimentStatus


MetadataKey = Annotated[str, Field(min_length=1, max_length=64)]
GeneralTransitionStatus = Literal[
    ExperimentStatus.RUNNING,
    ExperimentStatus.EVALUATING,
    ExperimentStatus.PASSED,
    ExperimentStatus.FAILED,
    ExperimentStatus.ERROR,
]


class ExperimentCreate(BaseModel):
    """새 실험과 초기 metadata 생성 요청."""

    model_config = ConfigDict(extra="forbid")

    hypothesis: str = Field(min_length=1)
    agent_session_id: str | None = Field(default=None, max_length=64)
    metadata: dict[MetadataKey, str] = Field(default_factory=dict)

    @field_validator("hypothesis")
    @classmethod
    def hypothesis_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("hypothesis must not be blank")
        return stripped


class ExperimentResponse(BaseModel):
    """최신 상태를 포함한 실험 응답."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    hypothesis: str
    status: ExperimentStatus
    metric_summary: dict | None
    agent_session_id: str | None
    created_at: datetime
    updated_at: datetime


class StatusUpdateRequest(BaseModel):
    """PROMOTED를 제외한 일반 상태 변경 요청."""

    model_config = ConfigDict(extra="forbid")

    status: GeneralTransitionStatus
    reason: str | None = None
    metric_snapshot: dict | None = None


class ExperimentEventCreate(BaseModel):
    """멱등성이 보장되는 일반 상태 event 요청."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    to_status: GeneralTransitionStatus
    reason: str | None = None
    metric_snapshot: dict | None = None


class ExperimentEventResponse(BaseModel):
    """fingerprint를 숨긴 상태 event 응답."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    experiment_id: uuid.UUID
    idempotency_key: str
    from_status: ExperimentStatus | None
    to_status: ExperimentStatus
    reason: str | None
    metric_snapshot: dict | None
    created_at: datetime


class ExperimentEventListQuery(BaseModel):
    """1초 polling용 Event cursor 조회 조건."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=100, ge=1, le=200)
    after_id: uuid.UUID | None = None


class ExperimentEventPageResponse(BaseModel):
    """Event polling 결과와 다음 cursor."""

    items: list[ExperimentEventResponse]
    next_cursor: uuid.UUID | None


class ExperimentLogCreate(BaseModel):
    """멱등성이 보장되는 실행 Log 생성 요청."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    log_type: str = Field(default="stdout", min_length=1, max_length=32)
    content: str = Field(min_length=1)


class ExperimentLogListQuery(BaseModel):
    """1초 polling용 Log cursor 조회 조건."""

    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=100, ge=1, le=100)
    after_id: uuid.UUID | None = None
    log_type: str | None = Field(default=None, min_length=1, max_length=32)


class ExperimentLogResponse(BaseModel):
    """내부 fingerprint를 제외한 실행 Log 응답."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    experiment_id: uuid.UUID
    idempotency_key: str
    log_type: str
    content: str
    created_at: datetime


class ExperimentLogPageResponse(BaseModel):
    """Log polling 결과와 다음 cursor."""

    items: list[ExperimentLogResponse]
    next_cursor: uuid.UUID | None


class ExperimentPageResponse(BaseModel):
    """Experiment offset pagination 응답."""

    items: list[ExperimentResponse]
    total: int
    limit: int
    offset: int


class ExperimentMetadataResponse(BaseModel):
    """실험 metadata key-value 응답."""

    entries: dict[str, str]


class PromotionRequest(BaseModel):
    """운영자가 merge·배포 근거를 남기는 전용 수동 승격 요청."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1)
    deployment_metadata: dict | None = None

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason must not be blank")
        return stripped
