"""action log(event log) 생성 파이프라인의 데이터 계약.

출력 스키마·규칙은 `docs/AGENT_SIMULATOR_SPEC.md`(Single Source of Truth)를 따른다.
이번 구현은 Phase 1(historical)만 다룬다.
"""
from datetime import UTC, datetime
import logging
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


logger = logging.getLogger(__name__)

ACTION_LOG_SCHEMA_VERSION = "action_log_schema_v1"
PROMPT_VERSION = "action_log_ctr_v1"
SOURCE_HISTORICAL = "historical"

# exposure_type 씨앗: 관련 후보 / 다양성 랜덤.
EXPOSURE_TOP_RANKED = "top_ranked"
EXPOSURE_EXPLORATION = "exploration"


class EventLog(BaseModel):
    """한 row = 특정 사용자에게 특정 영상이 노출된 사건 1회(impression=1).

    `docs/AGENT_SIMULATOR_SPEC.md`의 events 테이블 계약을 그대로 따른다.
    """

    event_id: str
    event_timestamp: datetime
    user_id: str
    video_id: str
    clicked: int
    watch_time_sec: int = Field(ge=0)
    liked: int
    search_keyword: str | None = None
    source: Literal["historical", "online_simulated"] = SOURCE_HISTORICAL
    rank: int | None = None
    exposure_type: Literal["top_ranked", "exploration"] | None = None

    @field_validator("clicked", "liked")
    @classmethod
    def binary_only(cls, value: int) -> int:
        """clicked/liked는 0 또는 1만 허용한다."""

        if value not in (0, 1):
            raise ValueError("clicked/liked must be 0 or 1")
        return value

    @model_validator(mode="after")
    def enforce_no_click_constraints(self) -> "EventLog":
        """clicked=0이면 watch_time_sec=0·liked=0을 강제한다(SSOT 제약)."""

        if self.clicked == 0 and (self.watch_time_sec != 0 or self.liked != 0):
            raise ValueError("clicked=0 requires watch_time_sec=0 and liked=0")
        return self

    def to_warehouse_row(self) -> dict[str, object]:
        """Data Warehouse 적재용 flat row(타임스탬프는 ISO 문자열)."""

        return {
            "event_id": self.event_id,
            "event_timestamp": self.event_timestamp.isoformat(),
            "user_id": self.user_id,
            "video_id": self.video_id,
            "clicked": self.clicked,
            "watch_time_sec": self.watch_time_sec,
            "liked": self.liked,
            "search_keyword": self.search_keyword,
            "source": self.source,
            "rank": self.rank,
            "exposure_type": self.exposure_type,
        }


class ImpressionDraft(BaseModel):
    """LLM 판단 결과(전역 2% 정규화 전 중간 산출물). 저장되지 않는다."""

    user_id: str
    video_id: str
    click_propensity: float = Field(ge=0.0, le=1.0)
    watch_fraction: float = Field(ge=0.0, le=1.0)
    would_like: bool
    search_keyword: str | None = None
    exposure_type: Literal["top_ranked", "exploration"]
    duration_sec: int = Field(ge=1)


class EventGenerationRequest(BaseModel):
    """action log 배치 생성 입력 조건과 출력 경로."""

    target_ctr: float = 0.02
    candidates_per_user: int = 24
    exploration_ratio: float = 0.2
    history_days: int = 30
    history_end: datetime = Field(
        default_factory=lambda: datetime.now(UTC).replace(microsecond=0)
    )
    max_events_per_user_per_day: int = 8
    seed: int = 42
    max_quarantine_ratio: float = 0.5
    output_path: str = "asset/action_log/event_log.parquet"
    warehouse_output_path: str = "data/generated/event_log.jsonl"
    quarantine_output_path: str = "data/generated/event_log_quarantine.jsonl"

    @field_validator("target_ctr", "exploration_ratio", "max_quarantine_ratio")
    @classmethod
    def ratio_0_1(cls, value: float) -> float:
        """비율 파라미터는 0~1 범위여야 한다."""

        if not 0.0 <= value <= 1.0:
            raise ValueError("ratio must be between 0 and 1")
        return value

    @field_validator("candidates_per_user", "max_events_per_user_per_day", "history_days")
    @classmethod
    def positive(cls, value: int) -> int:
        """후보 수/일 상한/기간은 1 이상이어야 한다."""

        if value < 1:
            raise ValueError("must be at least 1")
        return value


class QuarantineRecord(BaseModel):
    """생성 실패로 격리된 유저. 후처리를 위해 원본과 raw 응답을 보존한다."""

    user_id: str = ""
    virtual_user: dict[str, object] = Field(default_factory=dict)
    raw_llm_response: str = ""
    error_type: Literal["api_error", "invalid_json", "schema_fail"]
    error_message: str = ""


class EventLogBatch(BaseModel):
    """생성된 event log와 요청 정보를 함께 보관하는 batch 결과."""

    schema_version: str
    prompt_version: str
    request: EventGenerationRequest
    events: list[EventLog]
    generated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).replace(microsecond=0).isoformat()
    )

    @property
    def summary(self) -> dict[str, float]:
        """총 event 수, 클릭 수, 전역 CTR을 계산한다."""

        total = len(self.events)
        clicks = sum(1 for e in self.events if e.clicked == 1)
        return {
            "total_events": total,
            "clicks": clicks,
            "ctr": round(clicks / total, 4) if total else 0.0,
        }


class EventGenerationResult(BaseModel):
    """유효 batch와 격리 유저를 함께 담는 배치 실행 결과."""

    batch: EventLogBatch
    quarantine: list[QuarantineRecord] = Field(default_factory=list)

    @property
    def summary(self) -> dict[str, float]:
        counts = {"api_error": 0, "invalid_json": 0, "schema_fail": 0}
        for record in self.quarantine:
            counts[record.error_type] += 1
        return {
            **self.batch.summary,
            "quarantined_users": len(self.quarantine),
            **counts,
        }
