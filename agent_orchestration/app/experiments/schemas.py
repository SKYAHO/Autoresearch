"""Agent Orchestration 실험 API의 Pydantic 계약을 정의한다.

전체 파이프라인에서 FastAPI 호출자와 실험 service 사이의 입력·응답 형태를 검증한다.
DB query, 상태 전이와 인증은 담당하지 않는다.

실험 생성·조회, 일반 상태 event, polling log와 수동 승격에 필요한 엄격한 Pydantic v2
모델을 제공한다.
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Annotated, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_orchestration.app.experiments.models import (
    ExperimentStatus,
    StepKind,
    StepStatus,
)


MetadataKey = Annotated[str, Field(min_length=1, max_length=64)]
MetadataValue = Annotated[str, Field(max_length=8192)]
MAX_STEP_TARGET_BYTES = 4096


def validate_step_target_size(value: dict | None) -> dict | None:
    """Step `target`의 직렬화 크기를 제한한다.

    생성과 갱신 스키마가 **같은 함수를 공유**한다. 한쪽에만 걸면 PATCH로 무제한 target이
    들어와, 제한 근거(저장된 상태가 1초 polling으로 반복 조회됨)가 성립하지 않는다.
    """
    if value is None:
        return None
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_STEP_TARGET_BYTES:
        raise ValueError(
            f"target must be at most {MAX_STEP_TARGET_BYTES} bytes when serialized"
        )
    return value


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

    hypothesis: str = Field(min_length=1, max_length=8192)
    agent_session_id: str | None = Field(default=None, max_length=64)
    metadata: dict[MetadataKey, MetadataValue] = Field(default_factory=dict)

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
    issue_number: int | None
    issue_branch: str | None
    created_at: datetime
    updated_at: datetime


class StatusUpdateRequest(BaseModel):
    """PROMOTED를 제외한 일반 상태 변경 요청."""

    model_config = ConfigDict(extra="forbid")

    status: GeneralTransitionStatus
    reason: str | None = Field(default=None, max_length=8192)
    metric_snapshot: dict | None = None


class ExperimentEventCreate(BaseModel):
    """멱등성이 보장되는 일반 상태 event 요청."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    to_status: GeneralTransitionStatus
    reason: str | None = Field(default=None, max_length=8192)
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


class ExperimentEventPageResponse(BaseModel):
    """Event polling 결과와 다음 cursor."""

    items: list[ExperimentEventResponse]
    next_cursor: uuid.UUID | None


class ExperimentLogCreate(BaseModel):
    """멱등성이 보장되는 실행 Log 생성 요청."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    log_type: str = Field(default="stdout", min_length=1, max_length=32)
    content: str = Field(min_length=1, max_length=8192)


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


class ExperimentStepCreate(BaseModel):
    """멱등성이 보장되는 작업 단계 생성 요청."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    step_kind: StepKind
    step_type: str = Field(min_length=1, max_length=64)
    status: StepStatus = StepStatus.STARTED
    message: str | None = Field(default=None, max_length=500)
    target: dict | None = None

    _validate_target = field_validator("target")(validate_step_target_size)

    @field_validator("step_type")
    @classmethod
    def step_type_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("step_type must not be blank")
        return stripped


class ExperimentStepUpdate(BaseModel):
    """작업 단계의 진행 상태를 갱신하는 **전체 교체** 요청.

    부분 병합이 아니다 — 생략된 `message`/`target`은 이전 값 유지가 아니라 `null`로
    갱신된다. 호출자는 갱신할 때마다 그 시점의 상태 전체를 보낸다.
    """

    model_config = ConfigDict(extra="forbid")

    status: StepStatus
    message: str | None = Field(default=None, max_length=500)
    target: dict | None = None

    # 생성 스키마와 같은 함수를 공유한다 — 한쪽만 막으면 PATCH로 무제한 target이 들어온다.
    _validate_target = field_validator("target")(validate_step_target_size)


class ExperimentStepResponse(BaseModel):
    """내부 fingerprint를 제외한 작업 단계 응답.

    relationship 필드를 두지 않는다 — `IntegrityError` 복구 경로가 expunge 후 rollback한
    객체를 그대로 직렬화하므로, relationship이 있으면 만료된 세션에서 지연 로딩이 터진다.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    experiment_id: uuid.UUID
    idempotency_key: str
    step_kind: StepKind
    step_type: str
    status: StepStatus
    message: str | None
    target: dict | None
    created_at: datetime
    updated_at: datetime


class ExperimentStepPageResponse(BaseModel):
    """Step polling 결과와 다음 cursor."""

    items: list[ExperimentStepResponse]
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


class IssuePublicationRequest(BaseModel):
    """가설을 `[AR]` 이슈로 발행하는 요청."""

    model_config = ConfigDict(extra="forbid")

    allowed_scope: tuple[
        Literal["prod_model_contract", "feast_definition", "promotion"], ...
    ] = ()
    # 저장된 본문이 파서를 통과하지 못해 고착됐을 때만 쓴다. issue_number가 이미 있으면
    # 무시된다 — 발행된 이슈의 본문을 바꾸는 것은 이 endpoint의 책임이 아니다.
    regenerate: bool = False


class IssuePublicationResponse(BaseModel):
    """발행 결과 좌표."""

    model_config = ConfigDict(extra="forbid")

    issue_number: int
    issue_url: str
    issue_branch: str


class PromotionRequest(BaseModel):
    """운영자가 merge·배포 근거를 남기는 전용 수동 승격 요청."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=8192)
    deployment_metadata: dict | None = None

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason must not be blank")
        return stripped
