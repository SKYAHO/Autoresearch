"""Action-log LLM 판정 구간의 안전한 구조화 telemetry 유틸리티.

[파이프라인] 일일 추천 배치의 노출 후보 조립 뒤, action log 출력 최종화 전
LLM 판정 worker와 single coordinator가 운영 상태를 기록하는 구간을 담당한다.

[기능] shard micro-work progress와 bounded single-mode retention progress를
식별자·원문 없이 JSON event로 기록하고, 상세/집계 telemetry 설정을 검증한다.

[비책임] LLM 요청·draft/event 생성과 output writer
(autoresearch/action_logs/pipeline.py), OpenRouter client 호출
(autoresearch/action_logs/llm_generator.py), 일일 publish
(autoresearch/action_logs/daily.py).
"""

from __future__ import annotations

import json
import logging
import math
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic
from typing import Iterator, Literal, Protocol


DEFAULT_TELEMETRY_DETAIL_MAX_WORK = 100
DEFAULT_TELEMETRY_INTERVAL_SEC = 15.0


@dataclass(frozen=True)
class ActionLogWorkLogContext:
    """worker thread의 안전한 로그 식별자와 상세 로그 정책."""

    shard_index: int
    work_sequence: int
    detailed: bool


_WORK_LOG_CONTEXT: ContextVar[ActionLogWorkLogContext | None] = ContextVar(
    "action_log_work_log_context",
    default=None,
)


@contextmanager
def action_log_work_log_context(
    *,
    shard_index: int | None,
    work_sequence: int,
    detailed: bool,
) -> Iterator[None]:
    """OpenRouter 호출 동안 worker-safe 구조화 로그 context를 설정한다."""

    token = _WORK_LOG_CONTEXT.set(
        ActionLogWorkLogContext(
            shard_index=-1 if shard_index is None else shard_index,
            work_sequence=work_sequence,
            detailed=detailed,
        )
    )
    try:
        yield
    finally:
        _WORK_LOG_CONTEXT.reset(token)


def emit_action_log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    detailed_only: bool = False,
    include_none_fields: bool = False,
    **fields: object,
) -> None:
    """Airflow stdout에서 바로 읽을 수 있는 한 줄 JSON event를 기록한다.

    호출자는 secret, prompt, raw response, user/persona 식별자를 fields에 넘기지
    않아야 한다. 이 함수는 현재 work context의 shard/sequence만 자동 추가한다.
    """

    context = _WORK_LOG_CONTEXT.get()
    if detailed_only and context is not None and not context.detailed:
        return
    payload: dict[str, object] = {
        "event": event,
        "shard_index": context.shard_index if context is not None else -1,
        "work_sequence": context.work_sequence if context is not None else -1,
    }
    payload.update(
        {
            key: value
            for key, value in fields.items()
            if include_none_fields or value is not None
        }
    )
    logger.log(
        level,
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _warn_config_fallback(
    logger: logging.Logger,
    *,
    name: str,
    default: int | float,
    reason: str,
) -> None:
    emit_action_log_event(
        logger,
        logging.WARNING,
        "action_log_telemetry_config_fallback",
        setting=name,
        fallback=default,
        reason=reason,
    )


def _env_int(
    name: str,
    default: int,
    *,
    logger: logging.Logger,
    minimum: int | None = None,
) -> int:
    value = os.environ.get(name)
    if value in {None, ""}:
        return default
    try:
        parsed = int(value)
    except ValueError:
        _warn_config_fallback(
            logger,
            name=name,
            default=default,
            reason="invalid_number",
        )
        return default
    if minimum is not None and parsed < minimum:
        _warn_config_fallback(
            logger,
            name=name,
            default=default,
            reason="out_of_range",
        )
        return default
    return parsed


def _env_float(
    name: str,
    default: float,
    *,
    logger: logging.Logger,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    value = os.environ.get(name)
    if value in {None, ""}:
        return default
    try:
        parsed = float(value)
    except ValueError:
        _warn_config_fallback(
            logger,
            name=name,
            default=default,
            reason="invalid_number",
        )
        return default
    if (
        not math.isfinite(parsed)
        or (minimum is not None and parsed < minimum)
        or (maximum is not None and parsed > maximum)
    ):
        _warn_config_fallback(
            logger,
            name=name,
            default=default,
            reason="out_of_range",
        )
        return default
    return parsed


def _percentile(ordered_values: list[float], percentile: float) -> float:
    if not ordered_values:
        return 0.0
    index = max(0, math.ceil(percentile * len(ordered_values)) - 1)
    return ordered_values[index]


def _latency_percentiles(values: list[float]) -> tuple[float, float]:
    ordered = sorted(values)
    return _percentile(ordered, 0.50), _percentile(ordered, 0.95)


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


class _StreamingRetentionSnapshot(Protocol):
    """single-mode coordinator가 reporter에 전달하는 식별자 없는 보존 수량."""

    phase: Literal["generating", "finalizing"]
    active_users: int
    buffered_drafts: int
    buffered_events: int
    in_flight_work: int
    activated_users: int
    total_users: int
    submitted_work: int
    total_work: int | None
    completed_work: int
    failed_work: int
    pending_work: int | None


class ActionLogStreamingTelemetryReporter:
    """bounded single-mode retention progress와 확인된 작은 실행의 timing을 기록한다."""

    def __init__(
        self,
        *,
        logger: logging.Logger,
        detail_max_work: int | None = None,
        aggregate_interval_sec: float | None = None,
    ) -> None:
        resolved_detail_max_work = (
            detail_max_work
            if detail_max_work is not None
            else _env_int(
                "ACTION_LOG_TELEMETRY_DETAIL_MAX_WORK",
                DEFAULT_TELEMETRY_DETAIL_MAX_WORK,
                logger=logger,
                minimum=0,
            )
        )
        resolved_interval_sec = (
            aggregate_interval_sec
            if aggregate_interval_sec is not None
            else _env_float(
                "ACTION_LOG_TELEMETRY_INTERVAL_SEC",
                DEFAULT_TELEMETRY_INTERVAL_SEC,
                logger=logger,
                minimum=10.0,
                maximum=30.0,
            )
        )
        if resolved_detail_max_work < 0:
            raise ValueError("ACTION_LOG_TELEMETRY_DETAIL_MAX_WORK must be at least 0")
        if not 10.0 <= resolved_interval_sec <= 30.0:
            raise ValueError(
                "ACTION_LOG_TELEMETRY_INTERVAL_SEC must be between 10 and 30"
            )

        self._logger = logger
        self._detail_max_work = resolved_detail_max_work
        self._aggregate_interval_sec = resolved_interval_sec
        self._last_emit_at = monotonic()
        self._window: list[dict[str, float]] = []
        self._detail_metrics: list[dict[str, float | int]] = []
        self._detail_disabled = resolved_detail_max_work == 0
        self._provider_exhausted = False
        self._exact_total_work: int | None = None
        self._details_emitted = False

    @property
    def detailed_candidate(self) -> bool:
        """worker context에 상세 로그를 허용해도 되는지 반환한다."""

        return (
            not self._detail_disabled
            and self._provider_exhausted
            and self._exact_total_work is not None
            and self._exact_total_work <= self._detail_max_work
        )

    def start(self, snapshot: _StreamingRetentionSnapshot) -> None:
        """streaming coordinator가 활성 user를 채우기 전의 시작 상태를 강제 기록한다."""

        self._observe(snapshot, force=True)

    def note_submission(self, submitted_work: int) -> None:
        """다음 work의 상세 context를 고르기 전에 detail 상한을 적용한다."""

        if submitted_work > self._detail_max_work:
            self._disable_details()

    def record_work(
        self,
        *,
        work_sequence: int,
        queue_wait_ms: float,
        request_elapsed_ms: float,
        parse_elapsed_ms: float,
        total_elapsed_ms: float,
    ) -> None:
        """완료된 work의 식별자 없는 timing을 현재 interval과 detail buffer에 보관한다."""

        window_metrics = {
            "queue_wait_ms": queue_wait_ms,
            "request_elapsed_ms": request_elapsed_ms,
            "parse_elapsed_ms": parse_elapsed_ms,
            "total_elapsed_ms": total_elapsed_ms,
        }
        self._window.append(window_metrics)
        if self._detail_disabled or len(self._detail_metrics) >= self._detail_max_work:
            return
        self._detail_metrics.append(
            {"work_sequence": work_sequence, **window_metrics}
        )

    def observe(self, snapshot: _StreamingRetentionSnapshot) -> None:
        """현재 retention snapshot을 interval throttle에 따라 기록한다."""

        self._observe(snapshot, force=False)

    def finish(self, snapshot: _StreamingRetentionSnapshot) -> None:
        """마지막 progress와 안전하게 확정된 detail metric을 강제 기록한다."""

        self._observe(snapshot, force=True)
        self._emit_details(snapshot)

    def _disable_details(self) -> None:
        self._detail_disabled = True
        self._detail_metrics.clear()

    def _observe(self, snapshot: _StreamingRetentionSnapshot, *, force: bool) -> None:
        if snapshot.total_work is not None:
            self._provider_exhausted = True
            self._exact_total_work = snapshot.total_work
            if snapshot.total_work > self._detail_max_work:
                self._disable_details()
        self._emit_progress(snapshot, force=force)

    def _emit_progress(
        self,
        snapshot: _StreamingRetentionSnapshot,
        *,
        force: bool,
    ) -> None:
        now = monotonic()
        if not force and now - self._last_emit_at < self._aggregate_interval_sec:
            return

        total_latencies = [metrics["total_elapsed_ms"] for metrics in self._window]
        latency_p50_ms, latency_p95_ms = _latency_percentiles(total_latencies)
        fields: dict[str, object] = {
            "phase": snapshot.phase,
            "log_mode": "aggregate",
            "active_users": snapshot.active_users,
            "buffered_drafts": snapshot.buffered_drafts,
            "buffered_events": snapshot.buffered_events,
            "in_flight_work": snapshot.in_flight_work,
            "activated_users": snapshot.activated_users,
            "total_users": snapshot.total_users,
            "submitted_work": snapshot.submitted_work,
            "total_work": snapshot.total_work,
            "completed_work": snapshot.completed_work,
            "failed_work": snapshot.failed_work,
            "pending_work": snapshot.pending_work,
            "aggregation_window_work": len(self._window),
            "latency_p50_ms": round(latency_p50_ms, 3),
            "latency_p95_ms": round(latency_p95_ms, 3),
        }
        for name in (
            "queue_wait_ms",
            "request_elapsed_ms",
            "parse_elapsed_ms",
            "total_elapsed_ms",
        ):
            fields[name] = round(
                _average([metrics[name] for metrics in self._window]),
                3,
            )
        emit_action_log_event(
            self._logger,
            logging.INFO,
            "action_log_streaming_progress",
            include_none_fields=True,
            **fields,
        )
        self._window.clear()
        self._last_emit_at = now

    def _emit_details(self, snapshot: _StreamingRetentionSnapshot) -> None:
        if (
            self._details_emitted
            or self._detail_disabled
            or not self._provider_exhausted
            or self._exact_total_work is None
            or self._exact_total_work > self._detail_max_work
            or len(self._detail_metrics) != self._exact_total_work
        ):
            return

        for metrics in sorted(
            self._detail_metrics,
            key=lambda metric: int(metric["work_sequence"]),
        ):
            emit_action_log_event(
                self._logger,
                logging.INFO,
                "action_log_micro_work_complete",
                log_mode="detailed",
                work_sequence=int(metrics["work_sequence"]),
                total_work=self._exact_total_work,
                completed_work=snapshot.completed_work,
                failed_work=snapshot.failed_work,
                queue_wait_ms=round(float(metrics["queue_wait_ms"]), 3),
                request_elapsed_ms=round(float(metrics["request_elapsed_ms"]), 3),
                parse_elapsed_ms=round(float(metrics["parse_elapsed_ms"]), 3),
                total_elapsed_ms=round(float(metrics["total_elapsed_ms"]), 3),
            )
        self._detail_metrics.clear()
        self._details_emitted = True


class ActionLogTelemetryReporter:
    """micro-work 상세 로그와 대규모 실행 집계 로그를 같은 계약으로 출력한다."""

    def __init__(
        self,
        *,
        logger: logging.Logger,
        shard_index: int | None,
        total_work: int,
        initial_completed_work: int,
        detail_max_work: int | None = None,
        aggregate_interval_sec: float | None = None,
    ) -> None:
        resolved_detail_max_work = (
            detail_max_work
            if detail_max_work is not None
            else _env_int(
                "ACTION_LOG_TELEMETRY_DETAIL_MAX_WORK",
                DEFAULT_TELEMETRY_DETAIL_MAX_WORK,
                logger=logger,
                minimum=0,
            )
        )
        resolved_interval_sec = (
            aggregate_interval_sec
            if aggregate_interval_sec is not None
            else _env_float(
                "ACTION_LOG_TELEMETRY_INTERVAL_SEC",
                DEFAULT_TELEMETRY_INTERVAL_SEC,
                logger=logger,
                minimum=10.0,
                maximum=30.0,
            )
        )
        if resolved_detail_max_work < 0:
            raise ValueError("ACTION_LOG_TELEMETRY_DETAIL_MAX_WORK must be at least 0")
        if not 10.0 <= resolved_interval_sec <= 30.0:
            raise ValueError(
                "ACTION_LOG_TELEMETRY_INTERVAL_SEC must be between 10 and 30"
            )

        self._logger = logger
        self._shard_index = -1 if shard_index is None else shard_index
        self._total_work = total_work
        self._initial_completed_work = initial_completed_work
        self._detailed = total_work <= resolved_detail_max_work
        self._aggregate_interval_sec = resolved_interval_sec
        self._started_at = monotonic()
        self._last_emit_at = self._started_at
        self._last_emitted_completed = -1
        self._total_latencies: list[float] = []
        self._window: list[dict[str, float]] = []

    @property
    def detailed(self) -> bool:
        """현재 실행이 micro-work 상세 로그 모드인지 반환한다."""

        return self._detailed

    def start(
        self,
        *,
        completed_work: int,
        failed_work: int,
        active_workers: int,
        pending_work: int,
    ) -> None:
        """복원된 checkpoint를 포함한 shard 시작 상태를 기록한다."""

        self._emit_progress(
            completed_work=completed_work,
            failed_work=failed_work,
            active_workers=active_workers,
            pending_work=pending_work,
            force=True,
        )

    def record(
        self,
        *,
        work_sequence: int,
        queue_wait_ms: float,
        request_elapsed_ms: float,
        parse_elapsed_ms: float,
        checkpoint_write_elapsed_ms: float,
        checkpoint_rows: int,
        progress_write_elapsed_ms: float,
        submit_elapsed_ms: float,
        total_elapsed_ms: float,
        completed_work: int,
        failed_work: int,
        active_workers: int,
        pending_work: int,
    ) -> None:
        """완료된 work timing을 누적하고 상세 또는 throttle 집계 event를 기록한다."""

        metrics = {
            "queue_wait_ms": queue_wait_ms,
            "request_elapsed_ms": request_elapsed_ms,
            "parse_elapsed_ms": parse_elapsed_ms,
            "checkpoint_write_elapsed_ms": checkpoint_write_elapsed_ms,
            "progress_write_elapsed_ms": progress_write_elapsed_ms,
            "submit_elapsed_ms": submit_elapsed_ms,
            "total_elapsed_ms": total_elapsed_ms,
        }
        self._total_latencies.append(total_elapsed_ms)
        self._window.append({**metrics, "checkpoint_rows": float(checkpoint_rows)})

        if self._detailed:
            latency_p50_ms, latency_p95_ms = _latency_percentiles(
                self._total_latencies
            )
            emit_action_log_event(
                self._logger,
                logging.INFO,
                "action_log_micro_work_complete",
                shard_index=self._shard_index,
                work_sequence=work_sequence,
                log_mode="detailed",
                checkpoint_rows=checkpoint_rows,
                completed_work=completed_work,
                total_work=self._total_work,
                failed_work=failed_work,
                active_workers=active_workers,
                pending_work=pending_work,
                throughput_per_min=self._throughput(completed_work),
                latency_p50_ms=round(latency_p50_ms, 3),
                latency_p95_ms=round(latency_p95_ms, 3),
                eta_seconds=self._eta_seconds(completed_work),
                **{key: round(value, 3) for key, value in metrics.items()},
            )

        self._emit_progress(
            completed_work=completed_work,
            failed_work=failed_work,
            active_workers=active_workers,
            pending_work=pending_work,
            force=completed_work == self._total_work,
        )

    def finish(
        self,
        *,
        completed_work: int,
        failed_work: int,
        active_workers: int = 0,
        pending_work: int = 0,
    ) -> None:
        """마지막 집계 상태를 중복 없이 강제로 기록한다."""

        self._emit_progress(
            completed_work=completed_work,
            failed_work=failed_work,
            active_workers=active_workers,
            pending_work=pending_work,
            force=True,
        )

    def _throughput(self, completed_work: int) -> float:
        elapsed_minutes = max((monotonic() - self._started_at) / 60.0, 1e-9)
        processed = max(0, completed_work - self._initial_completed_work)
        return round(processed / elapsed_minutes, 3)

    def _eta_seconds(self, completed_work: int) -> float | None:
        elapsed_seconds = max(monotonic() - self._started_at, 1e-9)
        processed = max(0, completed_work - self._initial_completed_work)
        if processed == 0:
            return None
        per_second = processed / elapsed_seconds
        return round(max(0, self._total_work - completed_work) / per_second, 3)

    def _emit_progress(
        self,
        *,
        completed_work: int,
        failed_work: int,
        active_workers: int,
        pending_work: int,
        force: bool,
    ) -> None:
        now = monotonic()
        interval_elapsed = now - self._last_emit_at >= self._aggregate_interval_sec
        if not force and (self._detailed or not interval_elapsed):
            return
        if force and completed_work == self._last_emitted_completed and not self._window:
            return

        latency_p50_ms, latency_p95_ms = _latency_percentiles(
            self._total_latencies
        )
        fields: dict[str, object] = {
            "shard_index": self._shard_index,
            "work_sequence": -1,
            "log_mode": "detailed" if self._detailed else "aggregate",
            "aggregation_window_work": len(self._window),
            "completed_work": completed_work,
            "total_work": self._total_work,
            "failed_work": failed_work,
            "active_workers": active_workers,
            "pending_work": pending_work,
            "throughput_per_min": self._throughput(completed_work),
            "latency_p50_ms": round(latency_p50_ms, 3),
            "latency_p95_ms": round(latency_p95_ms, 3),
            "eta_seconds": self._eta_seconds(completed_work),
        }
        for name in (
            "queue_wait_ms",
            "request_elapsed_ms",
            "parse_elapsed_ms",
            "checkpoint_write_elapsed_ms",
            "progress_write_elapsed_ms",
            "submit_elapsed_ms",
            "total_elapsed_ms",
        ):
            fields[name] = round(
                _average([metrics[name] for metrics in self._window]),
                3,
            )
        fields["checkpoint_rows"] = int(
            sum(metrics["checkpoint_rows"] for metrics in self._window)
        )
        emit_action_log_event(
            self._logger,
            logging.INFO,
            "action_log_shard_progress",
            **fields,
        )
        self._window.clear()
        self._last_emit_at = now
        self._last_emitted_completed = completed_work
