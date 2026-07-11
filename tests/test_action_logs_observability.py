import json
import logging

import pytest

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
