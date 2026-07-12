"""action log(event log) 생성 파이프라인의 데이터 계약.

출력 스키마·규칙은 `docs/AGENT_SIMULATOR_SPEC.md`(Single Source of Truth)를 따른다.
이번 구현은 Phase 1(historical)만 다룬다.
"""
from datetime import UTC, date, datetime
import logging
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


logger = logging.getLogger(__name__)

ACTION_LOG_SCHEMA_VERSION = "action_log_schema_v1"
PROMPT_VERSION = "action_log_ctr_v4"
SOURCE_HISTORICAL = "historical"


class EventLog(BaseModel):
    """한 row = 한 이벤트. event_type ∈ {impression, click, view, like}.

    라벨(clicked)은 저장하지 않는다. clicked는 downstream이 impression↔click join으로
    파생한다. 노출마다 impression 1행, 클릭 선정분엔 click/view(+like) 행이 추가된다.
    설계: docs/superpowers/specs/2026-07-06-event-log-long-format-design.md
    """

    event_id: str
    event_timestamp: datetime
    user_id: str
    event_type: Literal["impression", "click", "view", "like"]
    video_id: str
    watch_time_sec: int | None = None
    rank: int | None = None
    source: Literal["historical", "online_simulated"] = SOURCE_HISTORICAL

    @model_validator(mode="after")
    def watch_time_only_for_view(self) -> "EventLog":
        """watch_time_sec는 view 이벤트일 때만 non-null(>=0), 그 외엔 null이어야 한다."""

        if self.event_type == "view":
            if self.watch_time_sec is None or self.watch_time_sec < 0:
                raise ValueError("view event requires watch_time_sec >= 0")
        elif self.watch_time_sec is not None:
            raise ValueError(f"{self.event_type} event must have watch_time_sec=None")
        return self

    def to_warehouse_row(self) -> dict[str, object]:
        """Data Warehouse 적재용 flat row(타임스탬프는 ISO 문자열)."""

        return {
            "event_id": self.event_id,
            "event_timestamp": self.event_timestamp.isoformat(),
            "user_id": self.user_id,
            "event_type": self.event_type,
            "video_id": self.video_id,
            "watch_time_sec": self.watch_time_sec,
            "rank": self.rank,
            "source": self.source,
        }


class ImpressionDraft(BaseModel):
    """LLM 판단 결과(전역 CTR 정규화 전 shard parquet 중간 산출물).

    draft 1건 = 후보(노출) 1건 = impression 1행에 대응한다. shard 생성과
    merge 사이에서는 `ACTION_LOG_DRAFT_PARQUET_SCHEMA` 계약으로 저장된다.
    """

    user_id: str
    video_id: str
    click_propensity: float = Field(ge=0.0, le=1.0)
    watch_fraction: float = Field(ge=0.0, le=1.0)
    would_like: bool
    duration_sec: int = Field(ge=1)


class ActionLogShardManifest(BaseModel):
    """완료된 action log shard의 생성·병합 데이터 계약."""

    manifest_version: str = "action_log_shard_manifest_v1"
    partition_date: date
    interval_start: datetime | None = None
    interval_end: datetime | None = None
    shard_index: int = Field(ge=0)
    shard_count: int = Field(ge=1)
    generator: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    generator_config: dict[str, object] = Field(default_factory=dict)
    candidates_per_user: int = Field(ge=1)
    target_ctr: float = Field(ge=0.0, le=1.0)
    personalized_ratio: float = Field(ge=0.0, le=1.0)
    popular_ratio: float = Field(ge=0.0, le=1.0)
    exploration_ratio: float = Field(ge=0.0, le=1.0)
    seed: int
    chunk_size: int = Field(ge=0)
    max_quarantine_ratio: float = Field(ge=0.0, le=1.0)
    history_end: datetime
    total_work: int = Field(ge=0)
    completed_work: int = Field(ge=0)
    quarantine_count: int = Field(ge=0)
    schema_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    input_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def completed_work_matches_total(self) -> "ActionLogShardManifest":
        """완료 manifest에는 모든 work의 성공 또는 격리 결과가 있어야 한다."""

        if self.completed_work != self.total_work:
            raise ValueError("completed_work must equal total_work")
        if self.quarantine_count > self.completed_work:
            raise ValueError("quarantine_count cannot exceed completed_work")
        if self.shard_index >= self.shard_count:
            raise ValueError("shard_index must be less than shard_count")
        return self


class EventGenerationRequest(BaseModel):
    """action log 배치 생성 입력 조건과 출력 경로."""

    target_ctr: float = 0.02
    candidates_per_user: int = 24
    personalized_ratio: float = 0.7
    popular_ratio: float = 0.2
    exploration_ratio: float = 0.1
    history_days: int = 30
    history_start: datetime | None = None
    history_end: datetime = Field(
        default_factory=lambda: datetime.now(UTC).replace(microsecond=0)
    )
    max_events_per_user_per_day: int = 8
    seed: int = 42
    max_concurrency: int = 1
    chunk_size: int = 0
    max_quarantine_ratio: float = 0.5
    output_path: str = "asset/action_log/event_log.parquet"
    warehouse_output_path: str = "data/generated/event_log.jsonl"
    quarantine_output_path: str = "data/generated/event_log_quarantine.jsonl"

    @field_validator(
        "target_ctr",
        "personalized_ratio",
        "popular_ratio",
        "exploration_ratio",
        "max_quarantine_ratio",
    )
    @classmethod
    def ratio_0_1(cls, value: float) -> float:
        """비율 파라미터는 0~1 범위여야 한다."""

        if not 0.0 <= value <= 1.0:
            raise ValueError("ratio must be between 0 and 1")
        return value

    @field_validator("candidates_per_user", "max_events_per_user_per_day", "history_days", "max_concurrency")
    @classmethod
    def positive(cls, value: int) -> int:
        """후보 수/일 상한/기간/동시성은 1 이상이어야 한다."""

        if value < 1:
            raise ValueError("must be at least 1")
        return value

    @field_validator("chunk_size")
    @classmethod
    def non_negative_chunk(cls, value: int) -> int:
        """chunk_size는 0(청킹 없음) 또는 양수여야 한다."""

        if value < 0:
            raise ValueError("chunk_size must be >= 0")
        return value

    @model_validator(mode="after")
    def history_window_is_ordered(self) -> "EventGenerationRequest":
        """명시적인 생성 구간은 종료 시각보다 앞서야 합니다."""

        if self.history_start is not None and self.history_start >= self.history_end:
            raise ValueError("history_start must be earlier than history_end")
        return self


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
        """총 event 수, impression/click 행 수, 전역 CTR(clicks/impressions)을 계산한다."""

        impressions = sum(1 for e in self.events if e.event_type == "impression")
        clicks = sum(1 for e in self.events if e.event_type == "click")
        return {
            "total_events": len(self.events),
            "impressions": impressions,
            "clicks": clicks,
            "ctr": round(clicks / impressions, 4) if impressions else 0.0,
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
