"""Action-log micro work의 안전한 구조화 telemetry 유틸리티."""

from __future__ import annotations

import json
import logging
import math
import os
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic
from typing import Iterator


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
    payload.update({key: value for key, value in fields.items() if value is not None})
    logger.log(
        level,
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value not in {None, ""} else default


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value not in {None, ""} else default


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


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
            )
        )
        resolved_interval_sec = (
            aggregate_interval_sec
            if aggregate_interval_sec is not None
            else _env_float(
                "ACTION_LOG_TELEMETRY_INTERVAL_SEC",
                DEFAULT_TELEMETRY_INTERVAL_SEC,
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
                latency_p50_ms=round(_percentile(self._total_latencies, 0.50), 3),
                latency_p95_ms=round(_percentile(self._total_latencies, 0.95), 3),
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
            "latency_p50_ms": round(_percentile(self._total_latencies, 0.50), 3),
            "latency_p95_ms": round(_percentile(self._total_latencies, 0.95), 3),
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
