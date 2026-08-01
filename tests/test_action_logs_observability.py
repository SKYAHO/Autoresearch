import json
import logging
from dataclasses import dataclass

import pytest

import autoresearch.action_logs.observability as observability
from autoresearch.action_logs.observability import ActionLogTelemetryReporter


def _metrics(**overrides):
    values = {
        "work_sequence": 0,
        "queue_wait_ms": 1.0,
        "request_elapsed_ms": 20.0,
        "parse_elapsed_ms": 2.0,
        "checkpoint_write_elapsed_ms": 3.0,
        "checkpoint_rows": 24,
        "progress_write_elapsed_ms": 4.0,
        "submit_elapsed_ms": 0.5,
        "total_elapsed_ms": 30.5,
        "completed_work": 1,
        "failed_work": 0,
        "active_workers": 1,
        "pending_work": 100,
    }
    values.update(overrides)
    return values


@dataclass(frozen=True)
class _StreamingSnapshot:
    phase: str
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


def _streaming_snapshot(**overrides) -> _StreamingSnapshot:
    values = {
        "phase": "generating",
        "active_users": 0,
        "buffered_drafts": 0,
        "buffered_events": 0,
        "in_flight_work": 0,
        "activated_users": 0,
        "total_users": 100,
        "submitted_work": 0,
        "total_work": None,
        "completed_work": 0,
        "failed_work": 0,
        "pending_work": None,
    }
    values.update(overrides)
    return _StreamingSnapshot(**values)


def test_streaming_telemetry_emits_retention_and_progress_fields(
    caplog,
    monkeypatch,
) -> None:
    """10초 미만의 중간 관측은 생략하되 시작·종료와 보존 수량은 남긴다."""

    logger = logging.getLogger("test.action_log.streaming.aggregate")
    now = [0.0]
    monkeypatch.setattr(observability, "monotonic", lambda: now[0])
    reporter = observability.ActionLogStreamingTelemetryReporter(
        logger=logger,
        detail_max_work=2,
        aggregate_interval_sec=10.0,
    )
    assert reporter.detailed_candidate is False
    start = _streaming_snapshot()
    progress_snapshot = _streaming_snapshot(
        active_users=4,
        buffered_drafts=48,
        in_flight_work=2,
        activated_users=4,
        submitted_work=4,
        completed_work=2,
    )
    finish = _streaming_snapshot(
        phase="finalizing",
        total_users=100,
        activated_users=100,
        submitted_work=100,
        total_work=100,
        completed_work=100,
        pending_work=0,
    )

    with caplog.at_level(logging.INFO, logger=logger.name):
        reporter.start(start)
        now[0] = 1.0
        reporter.record_work(
            work_sequence=0,
            queue_wait_ms=1.0,
            request_elapsed_ms=20.0,
            parse_elapsed_ms=2.0,
            total_elapsed_ms=30.0,
        )
        reporter.observe(progress_snapshot)
        now[0] = 10.0
        reporter.record_work(
            work_sequence=1,
            queue_wait_ms=2.0,
            request_elapsed_ms=21.0,
            parse_elapsed_ms=3.0,
            total_elapsed_ms=31.0,
        )
        reporter.observe(progress_snapshot)
        now[0] = 11.0
        reporter.finish(finish)

    events = [json.loads(record.message) for record in caplog.records]
    progress_events = [
        event
        for event in events
        if event["event"] == "action_log_streaming_progress"
    ]
    assert len(progress_events) == 3
    assert progress_events[0]["completed_work"] == 0
    assert progress_events[-1]["phase"] == "finalizing"

    progress = progress_events[1]
    assert progress["event"] == "action_log_streaming_progress"
    assert progress["phase"] == "generating"
    assert progress["active_users"] == 4
    assert progress["buffered_drafts"] == 48
    assert progress["buffered_events"] == 0
    assert progress["in_flight_work"] == 2
    assert progress["activated_users"] == 4
    assert progress["total_users"] == 100
    assert progress["submitted_work"] == 4
    assert progress["total_work"] is None
    assert progress["completed_work"] == 2
    assert progress["failed_work"] == 0
    assert "pending_work" in progress
    assert progress["pending_work"] is None
    assert progress_events[0]["throughput_per_min"] == 0.0
    assert progress["throughput_per_min"] == 12.0


def test_streaming_telemetry_bounds_aggregate_percentile_sample_and_reports_actual_window_count(
    caplog,
    monkeypatch,
) -> None:
    """interval 집계는 100,000 work에서도 고정 크기 telemetry 상태만 보관한다."""

    logger = logging.getLogger("test.action_log.streaming.bounded_aggregate")
    now = [0.0]
    monkeypatch.setattr(observability, "monotonic", lambda: now[0])
    reporter = observability.ActionLogStreamingTelemetryReporter(
        logger=logger,
        detail_max_work=0,
        aggregate_interval_sec=10.0,
    )
    final = _streaming_snapshot(
        total_users=100_000,
        activated_users=100_000,
        submitted_work=100_000,
        total_work=100_000,
        completed_work=100_000,
        pending_work=0,
    )

    with caplog.at_level(logging.INFO, logger=logger.name):
        reporter.start(_streaming_snapshot(total_users=100_000))
        for work_sequence in range(100_000):
            reporter.record_work(
                work_sequence=work_sequence,
                queue_wait_ms=1.0,
                request_elapsed_ms=2.0,
                parse_elapsed_ms=3.0,
                total_elapsed_ms=4.0,
            )

        retained_metric_containers = [
            value
            for value in vars(reporter).values()
            if isinstance(value, (dict, list))
        ]
        assert all(len(value) <= 2_048 for value in retained_metric_containers)
        assert observability.STREAMING_TELEMETRY_PERCENTILE_SAMPLE_MAX_WORK == 2_048
        assert reporter._aggregation_window_work == 100_000
        assert len(reporter._latency_sample) == 2_048
        assert reporter._detail_metrics == []

        now[0] = 10.0
        reporter.observe(final)

    progress_events = [
        json.loads(record.message)
        for record in caplog.records
        if json.loads(record.message)["event"] == "action_log_streaming_progress"
    ]
    progress = progress_events[-1]
    assert progress["aggregation_window_work"] == 100_000
    assert progress["aggregation_sample_work"] == 2_048
    assert progress["queue_wait_ms"] == 1.0
    assert progress["request_elapsed_ms"] == 2.0
    assert progress["parse_elapsed_ms"] == 3.0
    assert progress["total_elapsed_ms"] == 4.0
    assert reporter._aggregation_window_work == 0
    assert reporter._latency_sample == []


def test_streaming_telemetry_keeps_exact_percentiles_within_sample_cap(
    caplog,
    monkeypatch,
) -> None:
    """sample cap 미만 interval은 모든 latency를 보관해 legacy percentile을 그대로 낸다."""

    logger = logging.getLogger("test.action_log.streaming.exact_percentiles")
    now = [0.0]
    monkeypatch.setattr(observability, "monotonic", lambda: now[0])
    reporter = observability.ActionLogStreamingTelemetryReporter(
        logger=logger,
        detail_max_work=0,
        aggregate_interval_sec=10.0,
    )
    latencies = [80.0, 10.0, 60.0, 20.0, 40.0]
    complete = _streaming_snapshot(
        total_users=5,
        activated_users=5,
        submitted_work=5,
        total_work=5,
        completed_work=5,
        pending_work=0,
    )

    with caplog.at_level(logging.INFO, logger=logger.name):
        reporter.start(_streaming_snapshot(total_users=5))
        for work_sequence, total_elapsed_ms in enumerate(latencies):
            reporter.record_work(
                work_sequence=work_sequence,
                queue_wait_ms=1.0,
                request_elapsed_ms=2.0,
                parse_elapsed_ms=3.0,
                total_elapsed_ms=total_elapsed_ms,
            )
        now[0] = 10.0
        reporter.observe(complete)

    progress = [
        json.loads(record.message)
        for record in caplog.records
        if json.loads(record.message)["event"] == "action_log_streaming_progress"
    ][-1]
    assert progress["aggregation_window_work"] == 5
    assert progress["aggregation_sample_work"] == 5
    assert progress["latency_p50_ms"] == 40.0
    assert progress["latency_p95_ms"] == 80.0


def test_streaming_telemetry_emits_details_only_after_small_total_is_known(
    caplog,
    monkeypatch,
) -> None:
    """작은 실행의 완료 metric은 정확한 총량 뒤 전역 work 순서로만 기록한다."""

    logger = logging.getLogger("test.action_log.streaming.detail")
    now = [0.0]
    monkeypatch.setattr(observability, "monotonic", lambda: now[0])
    reporter = observability.ActionLogStreamingTelemetryReporter(
        logger=logger,
        detail_max_work=2,
        aggregate_interval_sec=10.0,
    )
    assert reporter.detailed_candidate is False
    unknown_total = _streaming_snapshot(
        active_users=2,
        in_flight_work=1,
        activated_users=2,
        submitted_work=1,
        completed_work=1,
    )
    known_total = _streaming_snapshot(
        active_users=0,
        activated_users=2,
        total_users=2,
        submitted_work=2,
        total_work=2,
        completed_work=2,
        pending_work=0,
    )
    finish = _streaming_snapshot(
        phase="finalizing",
        activated_users=2,
        total_users=2,
        submitted_work=2,
        total_work=2,
        completed_work=2,
        pending_work=0,
    )

    with caplog.at_level(logging.INFO, logger=logger.name):
        reporter.start(_streaming_snapshot(total_users=2))
        reporter.note_submission(1)
        now[0] = 1.0
        reporter.record_work(
            work_sequence=1,
            queue_wait_ms=1.0,
            request_elapsed_ms=6.0,
            parse_elapsed_ms=1.0,
            total_elapsed_ms=10.0,
        )
        reporter.observe(unknown_total)
        now[0] = 10.0
        reporter.observe(unknown_total)
        reporter.note_submission(2)
        now[0] = 11.0
        reporter.record_work(
            work_sequence=0,
            queue_wait_ms=2.0,
            request_elapsed_ms=35.0,
            parse_elapsed_ms=3.0,
            total_elapsed_ms=50.0,
        )
        reporter.observe(known_total)
        assert reporter.detailed_candidate is True
        now[0] = 12.0
        reporter.finish(finish)

    events = [json.loads(record.message) for record in caplog.records]
    micro = [
        event
        for event in events
        if event["event"] == "action_log_micro_work_complete"
    ]
    assert [event["work_sequence"] for event in micro] == [0, 1]
    assert [event["total_elapsed_ms"] for event in micro] == [50.0, 10.0]

    progress = [
        event
        for event in events
        if event["event"] == "action_log_streaming_progress"
    ]
    assert progress[-1]["aggregation_window_work"] == 1
    assert progress[-1]["latency_p50_ms"] == 50.0
    serialized = json.dumps(events, ensure_ascii=False)
    assert "user_id" not in serialized
    assert "raw_text" not in serialized
    assert "prompt" not in serialized


def test_streaming_telemetry_discards_details_above_threshold(
    caplog,
    monkeypatch,
) -> None:
    """세 번째 submit은 이미 보관한 상세 metric도 폐기해 큰 실행을 상세화하지 않는다."""

    logger = logging.getLogger("test.action_log.streaming.threshold")
    now = [0.0]
    monkeypatch.setattr(observability, "monotonic", lambda: now[0])
    reporter = observability.ActionLogStreamingTelemetryReporter(
        logger=logger,
        detail_max_work=2,
        aggregate_interval_sec=10.0,
    )
    finish = _streaming_snapshot(
        phase="finalizing",
        activated_users=3,
        total_users=3,
        submitted_work=3,
        total_work=3,
        completed_work=3,
        pending_work=0,
    )

    with caplog.at_level(logging.INFO, logger=logger.name):
        reporter.start(_streaming_snapshot(total_users=3))
        reporter.note_submission(1)
        reporter.record_work(
            work_sequence=0,
            queue_wait_ms=1.0,
            request_elapsed_ms=10.0,
            parse_elapsed_ms=1.0,
            total_elapsed_ms=12.0,
        )
        reporter.note_submission(2)
        reporter.record_work(
            work_sequence=1,
            queue_wait_ms=1.0,
            request_elapsed_ms=11.0,
            parse_elapsed_ms=1.0,
            total_elapsed_ms=13.0,
        )
        reporter.note_submission(3)
        assert reporter.detailed_candidate is False
        reporter.record_work(
            work_sequence=2,
            queue_wait_ms=1.0,
            request_elapsed_ms=12.0,
            parse_elapsed_ms=1.0,
            total_elapsed_ms=14.0,
        )
        now[0] = 1.0
        reporter.finish(finish)

    events = [json.loads(record.message) for record in caplog.records]
    assert not any(
        event["event"] == "action_log_micro_work_complete" for event in events
    )


def test_emit_action_log_event_preserves_none_filtering_unless_requested(
    caplog,
) -> None:
    """기존 payload는 None을 생략하고 streaming만 알 수 없는 수량을 null로 남긴다."""

    logger = logging.getLogger("test.action_log.streaming.none_fields")
    with caplog.at_level(logging.INFO, logger=logger.name):
        observability.emit_action_log_event(
            logger,
            logging.INFO,
            "legacy_event",
            optional_value=None,
        )
        observability.emit_action_log_event(
            logger,
            logging.INFO,
            "streaming_event",
            include_none_fields=True,
            optional_value=None,
        )

    legacy, streaming = [json.loads(record.message) for record in caplog.records]
    assert "optional_value" not in legacy
    assert streaming["optional_value"] is None
    assert "include_none_fields" not in streaming


def test_large_run_emits_throttled_aggregate_instead_of_micro_logs(caplog):
    logger = logging.getLogger("test.action_log.aggregate")
    reporter = ActionLogTelemetryReporter(
        logger=logger,
        shard_index=4,
        total_work=101,
        initial_completed_work=0,
        detail_max_work=100,
        aggregate_interval_sec=10.0,
    )

    with caplog.at_level(logging.INFO, logger=logger.name):
        reporter.start(
            completed_work=0,
            failed_work=0,
            active_workers=0,
            pending_work=101,
        )
        reporter.record(**_metrics())
        reporter.record(
            **_metrics(
                work_sequence=100,
                completed_work=101,
                active_workers=0,
                pending_work=0,
            )
        )
        reporter.finish(completed_work=101, failed_work=0)

    events = [json.loads(record.message) for record in caplog.records]
    assert not any(
        event["event"] == "action_log_micro_work_complete" for event in events
    )
    progress = [
        event for event in events if event["event"] == "action_log_shard_progress"
    ]
    assert len(progress) == 2
    assert progress[-1]["log_mode"] == "aggregate"
    assert progress[-1]["aggregation_window_work"] == 2
    assert progress[-1]["completed_work"] == progress[-1]["total_work"] == 101
    assert progress[-1]["shard_index"] == 4
    assert progress[-1]["request_elapsed_ms"] == 20.0
    assert progress[-1]["checkpoint_write_elapsed_ms"] == 3.0
    assert progress[-1]["progress_write_elapsed_ms"] == 4.0
    assert progress[-1]["checkpoint_rows"] == 48


@pytest.mark.parametrize("interval", [9.999, 30.001])
def test_aggregate_interval_must_stay_in_operational_range(interval):
    with pytest.raises(ValueError, match="between 10 and 30"):
        ActionLogTelemetryReporter(
            logger=logging.getLogger("test.action_log.invalid"),
            shard_index=0,
            total_work=101,
            initial_completed_work=0,
            aggregate_interval_sec=interval,
        )


@pytest.mark.parametrize(
    ("name", "value", "total_work", "expected_detailed", "reason", "fallback"),
    [
        (
            "ACTION_LOG_TELEMETRY_DETAIL_MAX_WORK",
            "private-invalid-integer",
            100,
            True,
            "invalid_number",
            100,
        ),
        (
            "ACTION_LOG_TELEMETRY_DETAIL_MAX_WORK",
            "-1",
            100,
            True,
            "out_of_range",
            100,
        ),
        (
            "ACTION_LOG_TELEMETRY_INTERVAL_SEC",
            "private-invalid-float",
            101,
            False,
            "invalid_number",
            15.0,
        ),
        (
            "ACTION_LOG_TELEMETRY_INTERVAL_SEC",
            "5",
            101,
            False,
            "out_of_range",
            15.0,
        ),
        (
            "ACTION_LOG_TELEMETRY_INTERVAL_SEC",
            "nan",
            101,
            False,
            "out_of_range",
            15.0,
        ),
    ],
)
def test_invalid_telemetry_env_falls_back_without_exposing_raw_value(
    monkeypatch,
    caplog,
    name,
    value,
    total_work,
    expected_detailed,
    reason,
    fallback,
):
    logger = logging.getLogger("test.action_log.env_fallback")
    monkeypatch.delenv("ACTION_LOG_TELEMETRY_DETAIL_MAX_WORK", raising=False)
    monkeypatch.delenv("ACTION_LOG_TELEMETRY_INTERVAL_SEC", raising=False)
    monkeypatch.setenv(name, value)

    with caplog.at_level(logging.WARNING, logger=logger.name):
        reporter = ActionLogTelemetryReporter(
            logger=logger,
            shard_index=0,
            total_work=total_work,
            initial_completed_work=0,
        )

    assert reporter.detailed is expected_detailed
    events = [json.loads(record.message) for record in caplog.records]
    assert events == [
        {
            "event": "action_log_telemetry_config_fallback",
            "fallback": fallback,
            "reason": reason,
            "setting": name,
            "shard_index": -1,
            "work_sequence": -1,
        }
    ]
    assert "value" not in events[0]
