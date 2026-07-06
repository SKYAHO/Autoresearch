"""VirtualUser + TrendingVideo pool로 Phase 1(historical) event log를 생성한다.

흐름: 유저 단위 격리(LLM 판단) → 전역 2% CTR 정규화 → 이벤트 확장(_expand_events:
노출마다 impression 1행, 클릭 선정분엔 click/view(+like)를 추가 배치) →
parquet/warehouse/quarantine 저장. 한 유저의 실패가 배치를 죽이지 않는다.
"""
import json
import logging
import random
from collections import defaultdict
from datetime import UTC, timedelta
from pathlib import Path
from typing import Protocol

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

from autoresearch.action_logs.candidate import build_candidates
from autoresearch.action_logs.schema import (
    ACTION_LOG_SCHEMA_VERSION,
    PROMPT_VERSION,
    SOURCE_HISTORICAL,
    EventGenerationRequest,
    EventGenerationResult,
    EventLog,
    EventLogBatch,
    ImpressionDraft,
    QuarantineRecord,
)
from autoresearch.action_logs.video_source import nominal_duration_sec


logger = logging.getLogger(__name__)


class ActionLogGenerator(Protocol):
    """pipeline이 generator 구현을 동일 방식으로 호출하기 위한 인터페이스."""

    model_name: str

    def generate(self, virtual_user: dict, videos: list[dict]) -> str:
        """유저 1명 × 후보 영상 목록에 대한 raw judgments JSON text를 반환한다."""

        ...


class ActionLogGenerationError(RuntimeError):
    """격리 비율이 임계치를 넘어 전량/대량 실패로 판정될 때 발생한다."""


EVENT_LOG_PARQUET_SCHEMA = pa.schema(
    [
        pa.field("event_id", pa.string()),
        pa.field("event_timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("user_id", pa.string()),
        pa.field("event_type", pa.string()),
        pa.field("video_id", pa.string()),
        pa.field("watch_time_sec", pa.int64()),
        pa.field("rank", pa.int64()),
        pa.field("source", pa.string()),
        pa.field("schema_version", pa.string()),
        pa.field("prompt_version", pa.string()),
        pa.field("llm_model", pa.string()),
        pa.field("generated_at", pa.string()),
    ]
)


def _clamp01(value: object) -> float:
    """소프트 신호를 0~1로 클램프(경미한 범위 이탈은 격리 대신 보정)."""

    return max(0.0, min(1.0, float(value)))


def _build_user_drafts(
    virtual_user: dict,
    candidates: list[dict],
    raw_text: str,
) -> list[ImpressionDraft]:
    """LLM raw judgments를 파싱해 후보별 ImpressionDraft를 만든다.

    json.JSONDecodeError -> invalid_json. 구조/타입 오류(ValueError/KeyError/TypeError/
    AttributeError/ValidationError) -> schema_fail. 판단이 누락된 후보는 비클릭 노출로 채운다.
    """
    data = json.loads(raw_text)  # invalid_json
    judgments = data["judgments"]  # KeyError/TypeError
    jmap = {str(j["video_id"]): j for j in judgments}

    user_id = str(virtual_user.get("user_id", ""))
    drafts: list[ImpressionDraft] = []
    for video in candidates:
        vid = video["video_id"]
        j = jmap.get(vid)
        if j is None:
            prop, frac, like = 0.0, 0.0, False
        else:
            prop = _clamp01(j.get("click_propensity", 0.0))
            frac = _clamp01(j.get("watch_fraction", 0.0))
            like = bool(j.get("would_like", False))
        drafts.append(
            ImpressionDraft(
                user_id=user_id,
                video_id=vid,
                click_propensity=prop,
                watch_fraction=frac,
                would_like=like,
                duration_sec=nominal_duration_sec(vid),
            )
        )
    return drafts


def _generate_drafts_isolated(
    generator: ActionLogGenerator,
    virtual_users: list[dict],
    videos: list[dict],
    request: EventGenerationRequest,
) -> tuple[list[ImpressionDraft], list[QuarantineRecord]]:
    """유저 단위로 판단 생성을 격리한다. 실패 유저는 quarantine으로 보낸다."""

    drafts: list[ImpressionDraft] = []
    quarantine: list[QuarantineRecord] = []
    for index, virtual_user in enumerate(virtual_users):
        user_id = str(virtual_user.get("user_id", f"user_{index}"))
        user_rng = random.Random(f"{request.seed}:{user_id}")
        candidates = build_candidates(
            virtual_user,
            videos,
            request.candidates_per_user,
            request.exploration_ratio,
            user_rng,
        )
        if not candidates:
            continue
        try:
            raw_text = generator.generate(virtual_user, candidates)
        except Exception as exc:  # noqa: BLE001 - API/transport failure isolation
            quarantine.append(
                QuarantineRecord(
                    user_id=user_id,
                    virtual_user=virtual_user,
                    raw_llm_response="",
                    error_type="api_error",
                    error_message=str(exc),
                )
            )
            continue
        try:
            drafts.extend(_build_user_drafts(virtual_user, candidates, raw_text))
        except json.JSONDecodeError as exc:
            quarantine.append(
                QuarantineRecord(
                    user_id=user_id,
                    virtual_user=virtual_user,
                    raw_llm_response=raw_text,
                    error_type="invalid_json",
                    error_message=str(exc),
                )
            )
        except (ValidationError, ValueError, KeyError, TypeError, AttributeError) as exc:
            quarantine.append(
                QuarantineRecord(
                    user_id=user_id,
                    virtual_user=virtual_user,
                    raw_llm_response=raw_text,
                    error_type="schema_fail",
                    error_message=str(exc),
                )
            )
    return drafts, quarantine


def _clicked_indices(drafts: list[ImpressionDraft], target_ctr: float) -> set[int]:
    """전역 2% 정규화: click_propensity 상위 round(ctr×N)개의 draft(=impression)
    인덱스를 '클릭'으로 선정해 반환한다."""

    total = len(drafts)
    n_click = round(target_ctr * total)
    order = sorted(
        range(total),
        key=lambda i: (-drafts[i].click_propensity, drafts[i].user_id, drafts[i].video_id),
    )
    return set(order[:n_click])


def _expand_events(
    drafts: list[ImpressionDraft],
    clicked: set[int],
    request: EventGenerationRequest,
) -> list[EventLog]:
    """draft + 클릭 결정 → long EventLog 스트림.

    노출마다 impression 1행. 클릭 선정분엔 같은 세션 흐름으로 click/view(+like)를
    impression 직후(초 단위 단조 증가)에 배치한다. 일일 상한은 impression 기준.
    """
    end = request.history_end
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)

    by_user: dict[str, list[int]] = defaultdict(list)
    for idx, draft in enumerate(drafts):
        by_user[draft.user_id].append(idx)

    events: list[EventLog] = []
    seq = 0

    def _emit(timestamp, user_id, event_type, video_id, watch=None):
        nonlocal seq
        events.append(
            EventLog(
                event_id=f"evt_{seq:08d}",
                event_timestamp=timestamp,
                user_id=user_id,
                event_type=event_type,
                video_id=video_id,
                watch_time_sec=watch,
                rank=None,
                source=SOURCE_HISTORICAL,
            )
        )
        seq += 1

    for user_id, indices in by_user.items():
        urng = random.Random(f"{request.seed}:ts:{user_id}")
        days = list(range(request.history_days))
        urng.shuffle(days)
        order = list(indices)
        urng.shuffle(order)
        cap = request.max_events_per_user_per_day
        for position, idx in enumerate(order):
            draft = drafts[idx]
            day = days[(position // cap) % len(days)]
            impression_ts = end - timedelta(
                days=day,
                hours=urng.randint(1, 23),  # 1h+ 여유로 후속 이벤트가 end를 넘지 않게
                minutes=urng.randint(0, 59),
                seconds=urng.randint(0, 59),
            )
            _emit(impression_ts, user_id, "impression", draft.video_id)
            if idx not in clicked:
                continue
            click_ts = impression_ts + timedelta(seconds=urng.randint(1, 30))
            _emit(click_ts, user_id, "click", draft.video_id)
            watch = max(1, round(draft.watch_fraction * draft.duration_sec))
            view_ts = click_ts + timedelta(seconds=urng.randint(1, 5))
            _emit(view_ts, user_id, "view", draft.video_id, watch=watch)
            if draft.would_like:
                like_ts = view_ts + timedelta(seconds=urng.randint(1, max(2, watch)))
                _emit(like_ts, user_id, "like", draft.video_id)
    return events


def _event_rows(batch: EventLogBatch, model_name: str) -> list[dict]:
    """EventLogBatch를 명시적 Parquet schema에 맞는 flat row로 변환한다."""

    rows = []
    for event in batch.events:
        rows.append(
            {
                "event_id": event.event_id,
                "event_timestamp": event.event_timestamp,
                "user_id": event.user_id,
                "event_type": event.event_type,
                "video_id": event.video_id,
                "watch_time_sec": event.watch_time_sec,
                "rank": event.rank,
                "source": event.source,
                "schema_version": batch.schema_version,
                "prompt_version": batch.prompt_version,
                "llm_model": model_name,
                "generated_at": batch.generated_at,
            }
        )
    return rows


def _write_event_log_parquet(batch: EventLogBatch, model_name: str, output_path: Path) -> None:
    """EventLogBatch를 명시적 Arrow schema의 Parquet 파일로 저장한다."""

    table = pa.Table.from_pylist(_event_rows(batch, model_name), schema=EVENT_LOG_PARQUET_SCHEMA)
    pq.write_table(table, output_path)


def write_event_log_warehouse_jsonl(batch: EventLogBatch, output_path: str | Path) -> None:
    """EventLogBatch를 Data Warehouse 적재용 JSONL row 파일로 저장한다."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for event in batch.events:
            file.write(json.dumps(event.to_warehouse_row(), ensure_ascii=False, default=str) + "\n")
    logger.info("Wrote warehouse event log", extra={"output_path": str(path), "total": len(batch.events)})


def write_quarantine_jsonl(records: list[QuarantineRecord], output_path: str | Path) -> None:
    """생성 실패로 격리된 유저를 후처리용 JSONL 파일로 저장한다."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record.model_dump(), ensure_ascii=False, default=str) + "\n")
    logger.info("Wrote quarantine output", extra={"output_path": str(path), "total": len(records)})


def generate_action_log_batch(
    request: EventGenerationRequest,
    virtual_users: list[dict],
    videos: list[dict],
    generator: ActionLogGenerator,
) -> EventGenerationResult:
    """유저 단위 격리 생성 → 전역 2% 정규화 → 조립 → 파일 저장을 실행한다."""

    logger.info(
        "Starting action log batch generation",
        extra={
            "users": len(virtual_users),
            "videos": len(videos),
            "target_ctr": request.target_ctr,
            "candidates_per_user": request.candidates_per_user,
            "seed": request.seed,
        },
    )

    drafts, quarantine = _generate_drafts_isolated(generator, virtual_users, videos, request)
    clicked = _clicked_indices(drafts, request.target_ctr)
    events = _expand_events(drafts, clicked, request)

    batch = EventLogBatch(
        schema_version=ACTION_LOG_SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        request=request,
        events=events,
    )
    result = EventGenerationResult(batch=batch, quarantine=quarantine)
    logger.info("Generated action log batch", extra=result.summary)

    # 전량/대량 실패 가드: 유저 단위 격리와 별개로, 배치 전체가 조용히 빈 결과로
    # 성공 종료하는 상황을 막는다. 격리 파일은 포렌식용으로 남기고 실패로 종료한다.
    if virtual_users:
        quarantine_ratio = len(quarantine) / len(virtual_users)
        if quarantine_ratio > request.max_quarantine_ratio:
            write_quarantine_jsonl(quarantine, request.quarantine_output_path)
            raise ActionLogGenerationError(
                f"quarantine ratio {quarantine_ratio:.2f} exceeds max_quarantine_ratio "
                f"{request.max_quarantine_ratio:.2f} "
                f"(valid_users={len(virtual_users) - len(quarantine)}, "
                f"quarantined={len(quarantine)}, users={len(virtual_users)})"
            )

    output_path = Path(request.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_event_log_parquet(batch, generator.model_name, output_path)
    write_event_log_warehouse_jsonl(batch, request.warehouse_output_path)
    write_quarantine_jsonl(quarantine, request.quarantine_output_path)
    logger.info(
        "Wrote action log outputs",
        extra={"output_path": str(output_path), **result.summary},
    )
    return result
