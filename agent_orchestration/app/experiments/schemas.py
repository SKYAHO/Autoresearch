"""Agent Orchestration 실험 API의 Pydantic 계약을 정의한다.

전체 파이프라인에서 FastAPI 호출자와 실험 service 사이의 입력·응답 형태를 검증한다.
DB query, 상태 전이와 인증은 담당하지 않는다.

실험 생성·조회, 일반 상태 event, polling log, 수동 승격, `[AR]` 이슈 발행에 필요한
엄격한 Pydantic v2 모델을 제공한다. 이슈 발행 요청이 싣는 사전등록 필드의 정의와
검증 규칙은 `issue_authoring.IssueSubmission`이 소유한다.
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Annotated, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_orchestration.app.experiments.issue_authoring import IssueSubmission
from agent_orchestration.app.experiments.models import (
    ExperimentStatus,
    StepKind,
    StepStatus,
)


MetadataKey = Annotated[str, Field(min_length=1, max_length=64)]
MetadataValue = Annotated[str, Field(max_length=8192)]
MAX_STEP_TARGET_BYTES = 4096
# `metric_snapshot`은 전문이 아니라 요약이다. 전문(`metrics.json`)은 GCS에 있고 이 값은
# 워크벤치가 매 polling마다 읽어 화면에 편다. 상한이 없으면 seed·조건이 늘어날 때
# 조용히 커져 목록 화면까지 느려진다.
MAX_METRIC_SNAPSHOT_BYTES = 16384
# 리포트 본문의 저장 상한(UTF-8 바이트). executor가 먼저 자르고
# (`executor/report.py`) service가 한 번 더 자른다 — **둘 다 거절이 아니라 절단이다.**
# 거절 경로를 남기면 리포트 내용이 지표 보고를 죽이는 결합이 되살아난다(spec 결정 3).
MAX_REPORT_MARKDOWN_BYTES = 65536
# 요청 본문 폭주만 막는 성긴 상한이다. **문자 수**라 위 바이트 상한과 단위가 다르며,
# DB에 들어갈 크기를 정하는 것은 service의 절단이다.
MAX_REPORT_MARKDOWN_CHARS = 262144
GitSha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


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


class CandidateReportRequest(BaseModel):
    """Executor가 원격 검증한 candidate SHA와 봉인된 좌표를 보고한다."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    issue_number: int = Field(ge=1)
    issue_branch: str = Field(min_length=1, max_length=255)
    base_dev_sha: GitSha
    candidate_sha: GitSha

    @model_validator(mode="after")
    def issue_branch_must_match_issue_number(self) -> CandidateReportRequest:
        """branch 이름에 든 이슈 번호와 별도 좌표가 갈리면 요청 단계에서 막는다.

        API가 봉인하는 이름은 `exp/<이슈번호>`다(#589). slug 접미사는 그 변경 이전에
        발행된 실험이 DB에 들고 있는 형식이므로 함께 받는다 — 여기서 막으면 진행 중인
        실험의 candidate 보고가 fail-closed된다.
        """
        prefix = f"exp/{self.issue_number}"
        if self.issue_branch != prefix and not self.issue_branch.startswith(f"{prefix}-"):
            raise ValueError("issue_branch must be 'exp/{issue_number}' or start with it.")
        return self


class ExecutorResultReportRequest(BaseModel):
    """Executor가 봉인된 채점 코드로 만든 실험 지표를 완주 보고와 함께 제출한다.

    **상태를 인자로 받지 않는다.** 이 endpoint의 의미는 "실험이 완주했고 결과가 나왔다"
    하나로 고정돼 있어, 호출자가 도달할 상태를 고를 수 없다. 실행이 실패한 경우는
    executor가 보고하지 못하므로(프로세스가 죽는다) launcher의 Job 회수가 `ERROR`로
    처리한다 — 죽는 쪽이 자기 죽음을 보고하는 경로를 신뢰하지 않는다.
    """

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=128)
    # 이미 candidate 보고로 저장된 SHA와 대조한다. 어긋나면 다른 실행의 결과이거나
    # 좌표가 뒤섞인 것이므로 받지 않는다.
    candidate_sha: GitSha
    metric_snapshot: dict
    # 에이전트가 쓴 `report.md` 본문. 없이 보고해도 성립한다 — 리포트 실패가 지표
    # 게시를 막지 않는다는 성질이 여기서 유지된다. 크기·내용 검증을 여기서 하지 않는
    # 이유는 spec 결정 3에 있다: 이 필드로 요청을 거절하면 리포트가 지표를 죽인다.
    report_markdown: str | None = Field(default=None, max_length=MAX_REPORT_MARKDOWN_CHARS)

    @field_validator("metric_snapshot")
    @classmethod
    def metric_snapshot_must_be_bounded(cls, value: dict) -> dict:
        """빈 요약과 무한정 큰 요약을 모두 거부한다."""
        if not value:
            raise ValueError("metric_snapshot must not be empty")
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_METRIC_SNAPSHOT_BYTES:
            raise ValueError(
                f"metric_snapshot must be at most {MAX_METRIC_SNAPSHOT_BYTES} bytes "
                "when serialized"
            )
        return value


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
    base_dev_sha: str | None
    candidate_sha: str | None
    executor_job_name: str | None
    created_at: datetime
    updated_at: datetime


class ExperimentReportResponse(BaseModel):
    """실험 리포트 본문 응답.

    `ExperimentResponse`와 분리한 이유는 그것이 5초 polling으로 반복 조회되고 목록
    화면에도 실리기 때문이다. 수십 KB 본문을 거기 실으면 목록까지 느려진다.
    """

    model_config = ConfigDict(from_attributes=True)

    experiment_id: uuid.UUID
    # 리포트가 아직 없으면 `None`이다. 실험이 없는 것과 구별되며, 그 경우는 404다.
    report_markdown: str | None


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
    """사전등록 필드를 `[AR]` 이슈로 발행하는 요청."""

    model_config = ConfigDict(extra="forbid")

    # 지표·guardrail을 호출자가 선언한다(#536). 형식 위반은 이슈가 열리기 전에 422로
    # 끊긴다 — `IssueSubmission`이 파서와 같은 규칙을 검사한다.
    #
    # `allowed_scope`는 #570에서 없앴다. 허용 범위 heading이 본문에서 빠져 값을 실을
    # 곳이 없고, 받아만 두고 버리면 호출자는 권한을 준 줄 안다. 화면에 범위를 다시
    # 노출할 때 본문 heading과 함께 되살린다.
    fields: IssueSubmission


class IssuePublicationResponse(BaseModel):
    """발행 결과 좌표와 migration 이후 존재할 수 있는 기준 SHA."""

    model_config = ConfigDict(extra="forbid")

    issue_number: int
    issue_url: str
    issue_branch: str
    base_dev_sha: str | None


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
