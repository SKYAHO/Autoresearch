"""에이전트 실험 상태 추적의 Pydantic 데이터 계약.

[파이프라인] 에이전트 실험 축(가설 제출 → 진행 보고 → 최종 리포트)의 경계
계약을 정의한다. 사용자·에이전트·대시보드가 주고받는 모든 본문이 여기를 통과한다.

[제공 기능] 제출 요청, 에이전트→서버 진행 보고, 최종 리포트, 조회 응답의
스키마와 검증 규칙을 제공한다. 저장 형식(이벤트 레코드)도 같은 모델로 직렬화해
파일과 API가 하나의 계약을 공유한다.

[비책임] 이벤트 저장·읽기(`src/experiments/store.py`), 상태 파생 규칙
(`src/experiments/service.py`), HTTP 상태 코드 매핑(`src/experiments/api.py`).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

MetricValue = float | int | str | bool

# 에이전트가 자유 서술하는 단계 라벨의 상한. 대시보드 한 줄 표시를 전제로 한다.
STAGE_MAX_LENGTH = 60
MESSAGE_MAX_LENGTH = 500


class ExperimentState(StrEnum):
    """실험 수명 상태. 대시보드 필터·집계의 기준이며 값 집합을 고정한다.

    자유 서술 단계 라벨은 `stage`가 따로 담는다 — 에이전트 루프가 바뀌어도
    이 enum은 그대로 두려는 의도적 분리다(#338 spec).
    """

    SUBMITTED = "submitted"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ExperimentVerdict(StrEnum):
    """최종 리포트의 판정. 성공/실패 이분법 대신 '결론 유형'을 담는다."""

    SUPPORTED = "supported"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    ERROR = "error"


class HypothesisSubmission(BaseModel):
    """가설 제출 요청 본문. 기본 기능 ①."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: Annotated[str, Field(min_length=1, max_length=120)]
    hypothesis: Annotated[str, Field(min_length=1, max_length=4000)]
    submitted_by: Annotated[str, Field(min_length=1, max_length=120)] | None = None
    labels: dict[str, str] = Field(default_factory=dict)


class StatusUpdate(BaseModel):
    """에이전트 → 서버 진행 보고 본문. 기본 기능 ②.

    `stage`는 자유 서술이고 `state`는 고정 enum이다. `progress`는 0~1 비율로,
    에이전트가 단계 수를 모르면 생략한다(단조 증가를 강제하지 않는다 — 재시도로
    되돌아가는 실험 루프가 정상 경로이기 때문).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: Annotated[str, Field(min_length=1, max_length=STAGE_MAX_LENGTH)]
    message: Annotated[str, Field(max_length=MESSAGE_MAX_LENGTH)] | None = None
    progress: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    metrics: dict[str, MetricValue] = Field(default_factory=dict)


class FinalReport(BaseModel):
    """최종 리포트 본문. 기본 기능 ③.

    아티팩트는 본문에 담지 않고 참조만 받는다(MLflow run·GCS URI 등) — 리포트
    저장소는 이 모듈 소유가 아니다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdict: ExperimentVerdict
    summary: Annotated[str, Field(min_length=1, max_length=4000)]
    metrics: dict[str, MetricValue] = Field(default_factory=dict)
    artifact_refs: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list
    )


class EventKind(StrEnum):
    """이벤트 레코드 종류. 저장 파일과 API가 공유한다."""

    SUBMITTED = "submitted"
    STATUS = "status"
    REPORT = "report"


class ExperimentEvent(BaseModel):
    """append-only 이벤트 레코드 하나. 저장 파일의 한 줄에 대응한다.

    payload는 종류별 본문(`HypothesisSubmission`/`StatusUpdate`/`FinalReport`)을
    직렬화한 dict다. 새 종류를 추가할 때 기존 파일을 마이그레이션하지 않아도
    되게 느슨하게 둔다.
    """

    model_config = ConfigDict(frozen=True)

    seq: Annotated[int, Field(ge=1)]
    kind: EventKind
    at: datetime
    payload: dict[str, object]


class ExperimentSummary(BaseModel):
    """목록 화면 한 행. 상세를 열지 않고도 판단할 최소 정보만 담는다."""

    model_config = ConfigDict(frozen=True)

    experiment_id: str
    title: str
    state: ExperimentState
    stage: str | None
    progress: float | None
    verdict: ExperimentVerdict | None
    submitted_at: datetime
    updated_at: datetime
    event_count: int


class ExperimentDetail(BaseModel):
    """상세 화면. 파생 상태 + 원본 이벤트 전체."""

    model_config = ConfigDict(frozen=True)

    experiment_id: str
    title: str
    hypothesis: str
    submitted_by: str | None
    labels: dict[str, str]
    state: ExperimentState
    stage: str | None
    progress: float | None
    submitted_at: datetime
    updated_at: datetime
    report: FinalReport | None
    events: list[ExperimentEvent]


class SubmissionAccepted(BaseModel):
    """제출 응답. 이후 보고·조회에 쓸 실험 id를 돌려준다."""

    model_config = ConfigDict(frozen=True)

    experiment_id: str
    state: ExperimentState
    submitted_at: datetime
