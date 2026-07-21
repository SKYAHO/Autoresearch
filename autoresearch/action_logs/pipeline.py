"""VirtualUser + TrendingVideo pool로 Phase 1(historical) event log를 생성한다.

흐름: 유저 단위 격리(LLM 판단) → 전역 2% CTR 정규화 → 이벤트 확장(_expand_events:
노출마다 impression 1행, 클릭 선정분엔 click/view(+like)를 추가 배치) →
parquet/warehouse/quarantine 저장. 한 유저의 실패가 배치를 죽이지 않는다.
"""
__arch__ = {
    "stage": "action_logs",
    "role": "가상 사용자 노출 판정을 이벤트 로그 초안과 저장 산출물로 변환합니다.",
    "owns": [
        "유저별 action log draft 생성과 격리",
        "클릭 정규화와 이벤트 확장",
        "parquet·warehouse·quarantine 출력",
    ],
    "not_owns": [
        "정책별 노출 후보 선택",
        "CTR 모델 학습",
    ],
}
import json
import logging
import math
import random
from collections import defaultdict
from collections.abc import Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import UTC, timedelta
from pathlib import Path
from time import monotonic
from typing import Callable, Literal, Protocol

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

from autoresearch.action_logs.candidate import build_candidates
from autoresearch.action_logs.observability import (
    ActionLogTelemetryReporter,
    action_log_work_log_context,
)
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
from autoresearch.action_logs.video_source import _MAX_DURATION, nominal_duration_sec


logger = logging.getLogger(__name__)

# 클릭 세션(impression 직후 click→view→like)이 impression 시각 뒤로 늘어날 수 있는 최대 초.
# like 지연 상한은 max(2, watch)이고, watch = round(watch_fraction × duration)이며 duration은
# nominal_duration_sec가 _MAX_DURATION으로 캡한다 → 세션 span의 상한이 _MAX_DURATION에 결합된다.
_CLICK_DELAY_MAX_SEC = 30
_VIEW_DELAY_MAX_SEC = 5
_MAX_SESSION_SPAN_SEC = _CLICK_DELAY_MAX_SEC + _VIEW_DELAY_MAX_SEC + max(2, _MAX_DURATION)
# impression을 history_end에서 최소 이만큼(시간, 올림) 이전에 두면 위 세션 span을 항상 흡수해
# 모든 후속 이벤트가 history_end를 넘지 않는다. _MAX_DURATION을 키우면 자동으로 여유가 늘어난다.
_MIN_IMPRESSION_HOURS = max(1, math.ceil(_MAX_SESSION_SPAN_SEC / 3600))


class ActionLogGenerator(Protocol):
    """pipeline이 generator 구현을 동일 방식으로 호출하기 위한 인터페이스."""

    model_name: str

    def generate(self, virtual_user: dict, videos: list[dict]) -> str:
        """유저 1명 × 후보 영상 목록에 대한 raw judgments JSON text를 반환한다."""

        ...


class ActionLogGenerationError(RuntimeError):
    """격리 비율이 임계치를 넘어 전량/대량 실패로 판정될 때 발생한다."""


@dataclass(frozen=True)
class ActionLogDraftGenerationResult:
    """LLM judgment draft 생성 결과와 quarantine 요약."""

    drafts: list[ImpressionDraft]
    quarantine: list[QuarantineRecord]
    total_work: int

    @property
    def summary(self) -> dict[str, int]:
        counts = {"api_error": 0, "invalid_json": 0, "schema_fail": 0}
        for record in self.quarantine:
            counts[record.error_type] += 1
        return {
            "drafts": len(self.drafts),
            "quarantined_users": len(self.quarantine),
            "total_work": self.total_work,
            **counts,
        }


@dataclass(frozen=True)
class ActionLogProgressSnapshot:
    """LLM chunk 생성 진행률을 외부 reporter로 전달하기 위한 스냅샷."""

    status: Literal["running", "success", "failed"]
    completed_chunks: int
    total_chunks: int
    success_chunks: int
    failed_chunks: int
    quarantined_chunks: int


ActionLogProgressCallback = Callable[[ActionLogProgressSnapshot], float | None]
ActionLogWorkIdFactory = Callable[[str, int], str]
ActionLogCheckpointCallback = Callable[[str, int, list[ImpressionDraft]], None]

# 유저별 노출 후보를 외부에서 결정할 때 쓰는 주입 지점. (virtual_user, user_rng)를
# 받아 video dict 목록을 반환한다. None이면 기존 build_candidates 휴리스틱을 쓴다.
CandidateProvider = Callable[[dict, random.Random], list[dict]]


@dataclass(frozen=True)
class ActionLogCheckpointPart:
    """durable checkpoint part에서 복원한 한 work의 성공 draft."""

    work_id: str
    work_order: int
    drafts: list[ImpressionDraft]


@dataclass(frozen=True)
class _ActionLogWorkItem:
    """결정론적 원본 순서를 가진 유저 후보 chunk 작업."""

    work_id: str
    user_id: str
    virtual_user: dict
    candidates: list[dict]


@dataclass(frozen=True)
class _ActionLogCallResult:
    """worker가 완결한 생성·검증 결과와 서로 겹치지 않는 timing."""

    work_sequence: int
    submitted_at: float
    started_at: float
    request_elapsed_ms: float
    parse_elapsed_ms: float
    raw_text: str = ""
    drafts: list[ImpressionDraft] | None = None
    error_type: Literal["api_error", "invalid_json", "schema_fail"] | None = None
    error: Exception | None = None


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
        pa.field("policy", pa.string()),
        pa.field("ctr_score", pa.float64()),
        pa.field("is_exploration", pa.bool_()),
        pa.field("policy_version", pa.string()),
        pa.field("exposure_source", pa.string()),
        pa.field("schema_version", pa.string()),
        pa.field("prompt_version", pa.string()),
        pa.field("llm_model", pa.string()),
        pa.field("generated_at", pa.string()),
    ]
)

ACTION_LOG_DRAFT_PARQUET_SCHEMA = pa.schema(
    [
        pa.field("user_id", pa.string()),
        pa.field("video_id", pa.string()),
        pa.field("click_propensity", pa.float64()),
        pa.field("watch_fraction", pa.float64()),
        pa.field("would_like", pa.bool_()),
        pa.field("duration_sec", pa.int64()),
    ]
)

ACTION_LOG_CHECKPOINT_PARQUET_SCHEMA = pa.schema(
    [
        pa.field("work_id", pa.string()),
        pa.field("work_order", pa.int64()),
        *ACTION_LOG_DRAFT_PARQUET_SCHEMA,
    ]
)


def _clamp01(value: object) -> float:
    """소프트 신호를 0~1로 클램프(경미한 범위 이탈은 격리 대신 보정)."""

    return max(0.0, min(1.0, float(value)))


WOULD_LIKE_CLICK_THRESHOLD = 0.7
WOULD_LIKE_WATCH_THRESHOLD = 0.6


def derive_would_like(click_propensity: float, watch_fraction: float) -> bool:
    """click/watch 신호로 좋아요(만족) 여부를 결정론적으로 파생한다.

    LLM 출력 토큰 절감을 위해 would_like는 응답에서 제거하고 코드로 판정한다.
    임계값은 like 이벤트 볼륨에 직접 영향을 주므로 캘리브레이션 대상이다.
    """

    return (
        click_propensity >= WOULD_LIKE_CLICK_THRESHOLD
        and watch_fraction >= WOULD_LIKE_WATCH_THRESHOLD
    )


def _build_user_drafts(
    virtual_user: dict,
    candidates: list[dict],
    raw_text: str,
) -> list[ImpressionDraft]:
    """LLM raw judgments를 파싱해 후보별 ImpressionDraft를 만든다.

    응답은 인덱스 포맷({"j": [[index, click_propensity, watch_fraction], ...]})이며,
    index는 후보의 0-base 배열 위치다. 각 판정을 index로 후보에 재결합하므로 LLM이
    순서를 바꿔 반환해도 오정렬되지 않는다. index 집합이 정확히 0..n-1(각 1회)이 아니면
    (개수 불일치·범위 이탈·중복·누락) 라벨 무결성을 보장할 수 없어 ValueError로
    격리(schema_fail)한다. would_like는 click/watch로부터 코드에서 파생한다.

    json.JSONDecodeError -> invalid_json. 구조/타입 오류(ValueError/KeyError/TypeError/
    AttributeError/ValidationError) -> schema_fail.
    """
    data = json.loads(raw_text)  # invalid_json
    judgments = data["j"]  # KeyError/TypeError
    n = len(candidates)
    if not isinstance(judgments, list) or len(judgments) != n:
        got = len(judgments) if isinstance(judgments, list) else "non-list"
        raise ValueError(f"judgment count mismatch: got {got}, expected {n}")

    by_index: dict[int, tuple[object, object]] = {}
    for entry in judgments:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            raise ValueError(f"judgment entry must be [index, cp, wf]: {entry!r}")
        raw_index = entry[0]
        # bool은 int의 subclass라 명시적으로 배제. 정수값 float(3.0)은 허용.
        if isinstance(raw_index, bool) or not isinstance(raw_index, (int, float)):
            raise ValueError(f"judgment index must be an integer: {raw_index!r}")
        if float(raw_index) != int(raw_index):
            raise ValueError(f"judgment index must be an integer: {raw_index!r}")
        index = int(raw_index)
        if not 0 <= index < n:
            raise ValueError(f"judgment index out of range: {index} (n={n})")
        if index in by_index:
            raise ValueError(f"duplicate judgment index: {index}")
        by_index[index] = (entry[1], entry[2])
    # len==n + 범위 [0,n) + 중복 없음 => index 집합은 정확히 0..n-1 (누락도 배제).

    user_id = str(virtual_user.get("user_id", ""))
    drafts: list[ImpressionDraft] = []
    for i, video in enumerate(candidates):
        cp_raw, wf_raw = by_index[i]
        vid = video["video_id"]
        prop = _clamp01(cp_raw)
        frac = _clamp01(wf_raw)
        drafts.append(
            ImpressionDraft(
                user_id=user_id,
                video_id=vid,
                click_propensity=prop,
                watch_fraction=frac,
                would_like=derive_would_like(prop, frac),
                duration_sec=nominal_duration_sec(vid),
            )
        )
    return drafts


def _try_build_user_drafts(
    virtual_user: dict,
    candidates: list[dict],
    raw_text: str,
) -> tuple[
    list[ImpressionDraft] | None,
    Literal["invalid_json", "schema_fail"] | None,
    Exception | None,
]:
    """raw 응답을 draft로 파싱하고 격리 분류를 값으로 반환한다."""

    try:
        return _build_user_drafts(virtual_user, candidates, raw_text), None, None
    except json.JSONDecodeError as exc:
        return None, "invalid_json", exc
    except (
        ValidationError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        return None, "schema_fail", exc


def _generate_action_log_work(
    generator: ActionLogGenerator,
    item: _ActionLogWorkItem,
    *,
    work_sequence: int,
    submitted_at: float,
    shard_index: int | None,
    detailed_telemetry: bool,
) -> _ActionLogCallResult:
    """한 worker에서 최초 요청부터 선택적 schema 교정과 검증까지 완결한다.

    request 시간은 generator 호출만, parse 시간은 draft 검증만 각각 누적한다.
    schema retry API 오류가 나더라도 최초 응답의 검증 시간과 최종 예외를 함께
    보존해 coordinator가 실제 최종 상태로 격리할 수 있게 한다.
    """

    started_at = monotonic()
    request_elapsed_ms = 0.0
    parse_elapsed_ms = 0.0
    raw_text = ""

    with action_log_work_log_context(
        shard_index=shard_index,
        work_sequence=work_sequence,
        detailed=detailed_telemetry,
    ):
        request_started_at = monotonic()
        try:
            raw_text = generator.generate(item.virtual_user, item.candidates)
        except Exception as exc:  # noqa: BLE001 - worker API boundary
            request_elapsed_ms += (monotonic() - request_started_at) * 1000
            return _ActionLogCallResult(
                work_sequence=work_sequence,
                submitted_at=submitted_at,
                started_at=started_at,
                request_elapsed_ms=request_elapsed_ms,
                parse_elapsed_ms=parse_elapsed_ms,
                error_type="api_error",
                error=exc,
            )
        request_elapsed_ms += (monotonic() - request_started_at) * 1000

        parse_started_at = monotonic()
        drafts, error_type, parse_error = _try_build_user_drafts(
            item.virtual_user,
            item.candidates,
            raw_text,
        )
        parse_elapsed_ms += (monotonic() - parse_started_at) * 1000

        schema_retry = getattr(generator, "generate_schema_retry", None)
        if drafts is None and callable(schema_retry):
            assert error_type is not None
            logger.warning(
                "Retrying action log judgment after response validation failure",
                extra={
                    "user_id": item.user_id,
                    "error_type": error_type,
                    "model_name": getattr(generator, "model_name", "unknown"),
                },
            )
            retry_started_at = monotonic()
            try:
                raw_text = schema_retry(
                    item.virtual_user,
                    item.candidates,
                    error_type=error_type,
                )
            except Exception as exc:  # noqa: BLE001 - schema retry API boundary
                request_elapsed_ms += (monotonic() - retry_started_at) * 1000
                return _ActionLogCallResult(
                    work_sequence=work_sequence,
                    submitted_at=submitted_at,
                    started_at=started_at,
                    request_elapsed_ms=request_elapsed_ms,
                    parse_elapsed_ms=parse_elapsed_ms,
                    raw_text=raw_text,
                    error_type="api_error",
                    error=exc,
                )
            request_elapsed_ms += (monotonic() - retry_started_at) * 1000

            parse_started_at = monotonic()
            drafts, error_type, parse_error = _try_build_user_drafts(
                item.virtual_user,
                item.candidates,
                raw_text,
            )
            parse_elapsed_ms += (monotonic() - parse_started_at) * 1000

    if drafts is None:
        assert error_type is not None and parse_error is not None
    else:
        assert error_type is None and parse_error is None
    return _ActionLogCallResult(
        work_sequence=work_sequence,
        submitted_at=submitted_at,
        started_at=started_at,
        request_elapsed_ms=request_elapsed_ms,
        parse_elapsed_ms=parse_elapsed_ms,
        raw_text=raw_text,
        drafts=drafts,
        error_type=error_type,
        error=parse_error,
    )



def _chunked(seq: list, size: int):
    """size>0이면 seq를 size 단위로 쪼개고, 아니면 통째로 하나만 내보낸다."""

    if size and size > 0:
        for start in range(0, len(seq), size):
            yield seq[start : start + size]
    else:
        yield seq


def _generate_drafts_isolated(
    generator: ActionLogGenerator,
    virtual_users: list[dict],
    videos: list[dict],
    request: EventGenerationRequest,
    progress_callback: ActionLogProgressCallback | None = None,
    work_id_factory: ActionLogWorkIdFactory | None = None,
    completed_work: dict[str, list[ImpressionDraft]] | None = None,
    checkpoint_callback: ActionLogCheckpointCallback | None = None,
    shard_index: int | None = None,
    candidate_provider: CandidateProvider | None = None,
) -> tuple[list[ImpressionDraft], list[QuarantineRecord], int]:
    """LLM 판정을 (유저×후보청크) 단위로 격리·병렬 생성한다.

    후보를 chunk_size로 쪼개 각 청크가 독립 LLM 콜(작은 context)이 되게 하고,
    콜은 max_concurrency로 병렬 실행한다. user_id·조립은 원본(유저,청크) 순서로
    처리하므로 병렬이어도 결정론. 한 청크 실패가 배치를 죽이지 않는다.
    반환: (drafts, quarantine, 총 작업(청크) 수).
    """

    # 1) 결정론적 작업 목록: (work_id, user_id, virtual_user, chunk_candidates)
    work: list[_ActionLogWorkItem] = []
    for index, virtual_user in enumerate(virtual_users):
        user_id = str(virtual_user.get("user_id", f"user_{index}"))
        user_rng = random.Random(f"{request.seed}:{user_id}")
        if candidate_provider is not None:
            candidates = candidate_provider(virtual_user, user_rng)
        else:
            candidates = build_candidates(
                virtual_user,
                videos,
                request.candidates_per_user,
                request.exploration_ratio,
                user_rng,
                personalized_ratio=request.personalized_ratio,
                popular_ratio=request.popular_ratio,
            )
        if not candidates:
            continue
        for chunk_index, chunk in enumerate(_chunked(candidates, request.chunk_size)):
            work_id = (
                work_id_factory(user_id, chunk_index)
                if work_id_factory is not None
                else f"work_{len(work):08d}"
            )
            work.append(
                _ActionLogWorkItem(
                    work_id=work_id,
                    user_id=user_id,
                    virtual_user=virtual_user,
                    candidates=chunk,
                )
            )

    work_ids = [item.work_id for item in work]
    if len(work_ids) != len(set(work_ids)):
        raise ValueError("duplicate action log work_id")

    # 2) 최초 LLM 콜부터 선택적 schema retry와 파싱까지 work 단위로 병렬화한다.
    # 결과는 작업 index별로 보관해 최종 조립 순서는 기존처럼 원본 순서를 유지한다.
    drafts_by_index: dict[int, list[ImpressionDraft]] = {}
    quarantine_by_index: dict[int, QuarantineRecord] = {}
    total_chunks = len(work)
    restored_work = completed_work or {}
    for index, item in enumerate(work):
        restored = restored_work.get(item.work_id)
        if restored is not None:
            drafts_by_index[index] = restored
    completed_chunks = len(drafts_by_index)
    success_chunks = len(drafts_by_index)
    failed_chunks = 0
    quarantined_chunks = 0
    telemetry = ActionLogTelemetryReporter(
        logger=logger,
        shard_index=shard_index,
        total_work=total_chunks,
        initial_completed_work=completed_chunks,
    )

    def _emit_progress(status: Literal["running", "success", "failed"]) -> float:
        if progress_callback is None:
            return 0.0
        snapshot = ActionLogProgressSnapshot(
            status=status,
            completed_chunks=completed_chunks,
            total_chunks=total_chunks,
            success_chunks=success_chunks,
            failed_chunks=failed_chunks,
            quarantined_chunks=quarantined_chunks,
        )
        started_at = monotonic()
        try:
            reported_elapsed_ms = progress_callback(snapshot)
        except Exception:  # noqa: BLE001 - progress reporting must not fail generation
            logger.warning("Action log progress callback failed", exc_info=True)
            return (monotonic() - started_at) * 1000
        if isinstance(reported_elapsed_ms, (int, float)) and not isinstance(
            reported_elapsed_ms,
            bool,
        ):
            return float(reported_elapsed_ms)
        return (monotonic() - started_at) * 1000

    def _call(i: int, submitted_at: float) -> _ActionLogCallResult:
        return _generate_action_log_work(
            generator,
            work[i],
            work_sequence=i,
            submitted_at=submitted_at,
            shard_index=shard_index,
            detailed_telemetry=telemetry.detailed,
        )

    _emit_progress("running")
    telemetry.start(
        completed_work=completed_chunks,
        failed_work=failed_chunks,
        active_workers=0,
        pending_work=total_chunks - completed_chunks,
    )
    pending_indices = iter(i for i in range(total_chunks) if i not in drafts_by_index)
    max_workers = max(1, request.max_concurrency)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures: dict[Future[_ActionLogCallResult], tuple[int, float]] = {}

        def _submit_next() -> bool:
            try:
                index = next(pending_indices)
            except StopIteration:
                return False
            submitted_at = monotonic()
            futures[executor.submit(_call, index, submitted_at)] = (
                index,
                submitted_at,
            )
            return True

        for _ in range(max_workers):
            if not _submit_next():
                break

        while futures:
            done, _pending = wait(futures, return_when=FIRST_COMPLETED)
            completed_batch: list[tuple[_ActionLogCallResult, float, int]] = []
            for future in sorted(done, key=lambda item: futures[item][0]):
                i, _submitted_at = futures.pop(future)
                item = work[i]
                # generator의 외부 API 오류는 worker가 명시적인 결과로 변환한다.
                # 여기까지 전파된 예외는 내부 버그이므로 api_error로 위장하지 않는다.
                call_result = future.result()

                succeeded_drafts = call_result.drafts
                failure: QuarantineRecord | None = None
                if succeeded_drafts is None:
                    assert call_result.error_type is not None
                    assert call_result.error is not None
                    failure = QuarantineRecord(
                        user_id=item.user_id,
                        virtual_user=item.virtual_user,
                        raw_llm_response=call_result.raw_text,
                        error_type=call_result.error_type,
                        error_message=str(call_result.error),
                    )

                checkpoint_write_elapsed_ms = 0.0
                checkpoint_rows = 0
                if succeeded_drafts is not None:
                    if checkpoint_callback is not None:
                        checkpoint_started_at = monotonic()
                        try:
                            checkpoint_callback(item.work_id, i, succeeded_drafts)
                        finally:
                            checkpoint_write_elapsed_ms = (
                                monotonic() - checkpoint_started_at
                            ) * 1000
                    checkpoint_rows = len(succeeded_drafts)
                    drafts_by_index[i] = succeeded_drafts
                    success_chunks += 1
                else:
                    assert failure is not None
                    quarantine_by_index[i] = failure
                    failed_chunks += 1
                    quarantined_chunks += 1
                completed_chunks += 1
                completed_batch.append(
                    (
                        call_result,
                        checkpoint_write_elapsed_ms,
                        checkpoint_rows,
                    )
                )

            progress_write_elapsed_ms = _emit_progress("running")
            submit_elapsed_by_work: list[float] = []
            for _ in completed_batch:
                submit_started_at = monotonic()
                _submit_next()
                submit_elapsed_by_work.append(
                    (monotonic() - submit_started_at) * 1000
                )
            active_workers = len(futures)
            pending_work = max(
                0,
                total_chunks - completed_chunks - active_workers,
            )
            last_batch_index = len(completed_batch) - 1
            for batch_index, (
                call_result,
                checkpoint_write_elapsed_ms,
                checkpoint_rows,
            ) in enumerate(completed_batch):
                telemetry.record(
                    work_sequence=call_result.work_sequence,
                    queue_wait_ms=(
                        call_result.started_at - call_result.submitted_at
                    )
                    * 1000,
                    request_elapsed_ms=call_result.request_elapsed_ms,
                    parse_elapsed_ms=call_result.parse_elapsed_ms,
                    checkpoint_write_elapsed_ms=checkpoint_write_elapsed_ms,
                    checkpoint_rows=checkpoint_rows,
                    progress_write_elapsed_ms=(
                        progress_write_elapsed_ms
                        if batch_index == last_batch_index
                        else 0.0
                    ),
                    submit_elapsed_ms=submit_elapsed_by_work[batch_index],
                    total_elapsed_ms=(
                        monotonic() - call_result.submitted_at
                    )
                    * 1000,
                    completed_work=completed_chunks,
                    failed_work=failed_chunks,
                    active_workers=active_workers,
                    pending_work=pending_work,
                )

    telemetry.finish(
        completed_work=completed_chunks,
        failed_work=failed_chunks,
    )

    # 3) 조립은 원본 순서로 단일 스레드에서(결정론). 실패는 quarantine.
    drafts: list[ImpressionDraft] = []
    quarantine: list[QuarantineRecord] = []
    for i in range(total_chunks):
        if i in quarantine_by_index:
            quarantine.append(quarantine_by_index[i])
        else:
            drafts.extend(drafts_by_index[i])
    return drafts, quarantine, total_chunks


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


def normalize_clicks(drafts: list[ImpressionDraft], target_ctr: float) -> set[int]:
    """전역 CTR 정규화의 공개 진입점 — 외부 배치(정책 시뮬레이션)가 합동 pool에
    한 번만 적용할 수 있게 _clicked_indices를 노출한다."""

    return _clicked_indices(drafts, target_ctr)


@dataclass(frozen=True)
class ExposureMetadata:
    """정책 시뮬레이션 노출 1건의 로그 태깅 메타데이터. 키는 (user_id, video_id)."""

    policy: Literal["baseline", "model"]
    rank: int
    ctr_score: float | None
    is_exploration: bool | None
    policy_version: str | None
    exposure_source: Literal["model", "trending", "random"] | None = None


def _expand_events(
    drafts: list[ImpressionDraft],
    clicked: set[int],
    request: EventGenerationRequest,
    *,
    metadata: Mapping[tuple[str, str], ExposureMetadata] | None = None,
    source: str = SOURCE_HISTORICAL,
    event_id_prefix: str = "evt",
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
        meta = metadata.get((user_id, video_id)) if metadata else None
        events.append(
            EventLog(
                event_id=f"{event_id_prefix}_{seq:08d}",
                event_timestamp=timestamp,
                user_id=user_id,
                event_type=event_type,
                video_id=video_id,
                watch_time_sec=watch,
                rank=meta.rank if meta else None,
                source=source,
                policy=meta.policy if meta else None,
                ctr_score=meta.ctr_score if meta else None,
                is_exploration=meta.is_exploration if meta else None,
                policy_version=meta.policy_version if meta else None,
                exposure_source=meta.exposure_source if meta else None,
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
                # history_end에서 최소 _MIN_IMPRESSION_HOURS시간 이전 → 후속 click/view/like가
                # 세션 최대 span(_MAX_SESSION_SPAN_SEC)만큼 밀려도 history_end를 넘지 않는다.
                hours=urng.randint(_MIN_IMPRESSION_HOURS, 23),
                minutes=urng.randint(0, 59),
                seconds=urng.randint(0, 59),
            )
            _emit(impression_ts, user_id, "impression", draft.video_id)
            if idx not in clicked:
                continue
            click_ts = impression_ts + timedelta(seconds=urng.randint(1, _CLICK_DELAY_MAX_SEC))
            _emit(click_ts, user_id, "click", draft.video_id)
            watch = max(1, round(draft.watch_fraction * draft.duration_sec))
            view_ts = click_ts + timedelta(seconds=urng.randint(1, _VIEW_DELAY_MAX_SEC))
            _emit(view_ts, user_id, "view", draft.video_id, watch=watch)
            last_ts = view_ts
            if draft.would_like:
                like_ts = view_ts + timedelta(seconds=urng.randint(1, max(2, watch)))
                _emit(like_ts, user_id, "like", draft.video_id)
                last_ts = like_ts
            # window 불변식 가드: 세션 마지막 이벤트도 history_end를 넘지 않는다.
            # _MIN_IMPRESSION_HOURS가 _MAX_DURATION 기반이라 성립하며, 이 결합이 깨지면
            # (예: _MAX_DURATION을 여유보다 크게 올리면) 여기서 조기에 실패한다.
            assert last_ts <= end, (
                f"session event {last_ts} exceeded history_end {end} — "
                "check _MIN_IMPRESSION_HOURS vs _MAX_SESSION_SPAN_SEC"
            )
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
                "policy": event.policy,
                "ctr_score": event.ctr_score,
                "is_exploration": event.is_exploration,
                "policy_version": event.policy_version,
                "exposure_source": event.exposure_source,
                "schema_version": batch.schema_version,
                "prompt_version": batch.prompt_version,
                "llm_model": model_name,
                "generated_at": batch.generated_at,
            }
        )
    return rows


def _draft_rows(drafts: list[ImpressionDraft]) -> list[dict]:
    """ImpressionDraft 목록을 shard work parquet row로 변환한다."""

    return [draft.model_dump() for draft in drafts]


def write_action_log_draft_parquet(
    drafts: list[ImpressionDraft],
    output_path: str | Path,
    *,
    filesystem=None,
) -> None:
    """Shard work parquet으로 저장할 LLM judgment draft를 쓴다."""

    if filesystem is None:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(
        _draft_rows(drafts),
        schema=ACTION_LOG_DRAFT_PARQUET_SCHEMA,
    )
    pq.write_table(table, output_path, filesystem=filesystem)


def read_action_log_draft_parquet(
    input_path: str | Path,
    *,
    filesystem=None,
) -> list[ImpressionDraft]:
    """Shard work parquet을 ImpressionDraft 목록으로 읽는다."""

    table = pq.read_table(input_path, filesystem=filesystem)
    return [ImpressionDraft.model_validate(row) for row in table.to_pylist()]


def write_action_log_checkpoint_part(
    work_id: str,
    work_order: int,
    drafts: list[ImpressionDraft],
    output_path: str | Path,
    *,
    filesystem=None,
) -> None:
    """성공한 work 하나를 immutable checkpoint parquet part로 쓴다."""

    if not drafts:
        raise ValueError("checkpoint part requires at least one draft")
    if filesystem is None:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"work_id": work_id, "work_order": work_order, **draft.model_dump()}
        for draft in drafts
    ]
    table = pa.Table.from_pylist(rows, schema=ACTION_LOG_CHECKPOINT_PARQUET_SCHEMA)
    pq.write_table(table, output_path, filesystem=filesystem)


def read_action_log_checkpoint_part(
    input_path: str | Path,
    *,
    filesystem=None,
) -> ActionLogCheckpointPart:
    """checkpoint parquet part를 work identity와 draft 목록으로 읽는다."""

    rows = pq.read_table(input_path, filesystem=filesystem).to_pylist()
    if not rows:
        raise ValueError(f"empty checkpoint part: {input_path}")
    work_ids = {str(row["work_id"]) for row in rows}
    work_orders = {int(row["work_order"]) for row in rows}
    if len(work_ids) != 1 or len(work_orders) != 1:
        raise ValueError(f"mixed work identity in checkpoint part: {input_path}")
    drafts = [
        ImpressionDraft.model_validate(
            {key: value for key, value in row.items() if key not in {"work_id", "work_order"}}
        )
        for row in rows
    ]
    return ActionLogCheckpointPart(
        work_id=work_ids.pop(),
        work_order=work_orders.pop(),
        drafts=drafts,
    )


def write_event_log_parquet(
    batch: EventLogBatch,
    model_name: str,
    output_path: str | Path,
    *,
    filesystem=None,
) -> None:
    """EventLogBatch를 명시적 Arrow schema의 Parquet 파일로 저장한다."""

    if filesystem is None:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pylist(_event_rows(batch, model_name), schema=EVENT_LOG_PARQUET_SCHEMA)
    pq.write_table(table, output_path, filesystem=filesystem)


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


def _raise_if_quarantine_exceeds(
    quarantine: list[QuarantineRecord],
    total_work: int,
    request: EventGenerationRequest,
    user_count: int,
) -> None:
    """전량/대량 실패가 조용히 성공 처리되지 않도록 quarantine 비율을 검증한다."""

    if not total_work:
        return

    quarantine_ratio = len(quarantine) / total_work
    if quarantine_ratio <= request.max_quarantine_ratio:
        return

    write_quarantine_jsonl(quarantine, request.quarantine_output_path)
    raise ActionLogGenerationError(
        f"quarantine ratio {quarantine_ratio:.2f} exceeds max_quarantine_ratio "
        f"{request.max_quarantine_ratio:.2f} "
        f"(quarantined={len(quarantine)}, total_chunks={total_work}, "
        f"users={user_count})"
    )


def generate_action_log_drafts(
    request: EventGenerationRequest,
    virtual_users: list[dict],
    videos: list[dict],
    generator: ActionLogGenerator,
    progress_callback: ActionLogProgressCallback | None = None,
    *,
    enforce_quarantine_limit: bool = True,
    work_id_factory: ActionLogWorkIdFactory | None = None,
    completed_work: dict[str, list[ImpressionDraft]] | None = None,
    checkpoint_callback: ActionLogCheckpointCallback | None = None,
    shard_index: int | None = None,
    candidate_provider: CandidateProvider | None = None,
) -> ActionLogDraftGenerationResult:
    """유저 단위 LLM 판단을 실행하고 전역 CTR 정규화 전 draft를 반환한다.

    단일 실행은 quarantine 비율을 즉시 검증한다. shard 실행은 성공 draft를
    보존하기 위해 이 검증을 merge 단계의 전역 합산 뒤로 미룰 수 있다.
    """

    logger.info(
        "Starting action log draft generation",
        extra={
            "users": len(virtual_users),
            "videos": len(videos),
            "target_ctr": request.target_ctr,
            "candidates_per_user": request.candidates_per_user,
            "seed": request.seed,
        },
    )

    drafts, quarantine, total_work = _generate_drafts_isolated(
        generator,
        virtual_users,
        videos,
        request,
        progress_callback,
        work_id_factory,
        completed_work,
        checkpoint_callback,
        shard_index,
        candidate_provider,
    )
    if enforce_quarantine_limit:
        _raise_if_quarantine_exceeds(quarantine, total_work, request, len(virtual_users))
    result = ActionLogDraftGenerationResult(
        drafts=drafts,
        quarantine=quarantine,
        total_work=total_work,
    )
    logger.info("Generated action log drafts", extra=result.summary)
    return result


def expand_action_log_drafts(
    request: EventGenerationRequest,
    drafts: list[ImpressionDraft],
    quarantine: list[QuarantineRecord] | None = None,
) -> EventGenerationResult:
    """전체 draft에 전역 CTR 정규화와 long event 확장을 적용한다."""

    clicked = _clicked_indices(drafts, request.target_ctr)
    events = _expand_events(drafts, clicked, request)

    batch = EventLogBatch(
        schema_version=ACTION_LOG_SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        request=request,
        events=events,
    )
    result = EventGenerationResult(batch=batch, quarantine=quarantine or [])
    logger.info("Generated action log batch", extra=result.summary)
    return result


def generate_action_log_batch(
    request: EventGenerationRequest,
    virtual_users: list[dict],
    videos: list[dict],
    generator: ActionLogGenerator,
    progress_callback: ActionLogProgressCallback | None = None,
) -> EventGenerationResult:
    """유저 단위 격리 생성 → 전역 2% 정규화 → 조립 → 파일 저장을 실행한다."""

    draft_result = generate_action_log_drafts(
        request,
        virtual_users,
        videos,
        generator,
        progress_callback,
    )
    result = expand_action_log_drafts(
        request,
        draft_result.drafts,
        draft_result.quarantine,
    )

    output_path = Path(request.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_event_log_parquet(result.batch, generator.model_name, output_path)
    write_event_log_warehouse_jsonl(result.batch, request.warehouse_output_path)
    write_quarantine_jsonl(draft_result.quarantine, request.quarantine_output_path)
    logger.info(
        "Wrote action log outputs",
        extra={"output_path": str(output_path), **result.summary},
    )
    return result
