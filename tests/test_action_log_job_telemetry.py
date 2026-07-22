import json
import logging

import autoresearch.jobs.action_log as action_log_job
from autoresearch.jobs._telemetry import (
    ACTION_LOG_TELEMETRY_LOGGERS,
    configure_action_log_telemetry_logging,
)


_SINGLE_ARGS = [
    "--mode",
    "single",
    "--partition-date",
    "2026-07-13",
    "--youtube-base-path",
    "gs://test-bucket/data_lake/youtube",
    "--virtual-users-path",
    "gs://test-bucket/asset/virtual_users.parquet",
    "--output-base-path",
    "gs://test-bucket/data_lake/action_log",
    "--click-threshold",
    "0.5",
]


def _reset_telemetry_loggers() -> None:
    for logger_name in ACTION_LOG_TELEMETRY_LOGGERS:
        telemetry_logger = logging.getLogger(logger_name)
        for handler in list(telemetry_logger.handlers):
            telemetry_logger.removeHandler(handler)
            handler.close()
        telemetry_logger.setLevel(logging.NOTSET)
        telemetry_logger.propagate = True


def test_configuration_is_idempotent_and_forwards_only_safe_json(capsys):
    _reset_telemetry_loggers()
    try:
        configure_action_log_telemetry_logging()
        configure_action_log_telemetry_logging()
        telemetry_logger = logging.getLogger(ACTION_LOG_TELEMETRY_LOGGERS[0])

        assert len(telemetry_logger.handlers) == 1

        telemetry_logger.info(json.dumps({"event": "progress", "completed": 1}))
        telemetry_logger.info("not-json")
        telemetry_logger.info(
            json.dumps({"event": "unsafe", "nested": {"user_id": "vu-1"}})
        )

        lines = capsys.readouterr().out.splitlines()
        assert [json.loads(line) for line in lines] == [
            {"completed": 1, "event": "progress"}
        ]
    finally:
        _reset_telemetry_loggers()


def test_main_keeps_job_summary_as_last_stdout_event(monkeypatch, capsys):
    _reset_telemetry_loggers()

    def fake_run(args):
        logging.getLogger(ACTION_LOG_TELEMETRY_LOGGERS[0]).info(
            json.dumps({"event": "progress", "completed": 1})
        )
        return {"status": "succeeded"}

    try:
        monkeypatch.setattr(action_log_job, "_run", fake_run)

        assert action_log_job.main(_SINGLE_ARGS) == 0

        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert [event["event"] for event in events] == ["progress", "job_summary"]
    finally:
        _reset_telemetry_loggers()
