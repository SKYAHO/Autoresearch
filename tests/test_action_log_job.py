import json
from types import SimpleNamespace

import pytest

import autoresearch.jobs.action_log as action_log_job


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
]


def _json_lines(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines() if line]


def test_main_emits_warning_before_success_summary(monkeypatch, capsys):
    monkeypatch.setattr(
        action_log_job,
        "_run",
        lambda args: {
            "status": "succeeded",
            "output_path": "test-bucket/data_lake/action_log/dt=2026-07-13/part-0.parquet",
            "warnings": [
                {
                    "event": "warning",
                    "warning_type": "quarantine_publish_failed",
                    "artifact": "quarantine",
                }
            ],
        },
    )

    assert action_log_job.main(_SINGLE_ARGS) == 0

    events = _json_lines(capsys.readouterr().out)
    assert [event["event"] for event in events] == ["warning", "job_summary"]
    assert events[0]["contract_version"] == "batch-contract-v1"
    assert events[-1]["status"] == "succeeded"
    assert events[-1]["partition_date"] == "2026-07-13"


def test_main_returns_exit_2_for_invalid_ratio_combination(monkeypatch, capsys):
    called = False

    def fail_if_called(args):
        nonlocal called
        called = True

    monkeypatch.setattr(action_log_job, "_run", fail_if_called)

    exit_code = action_log_job.main(
        [
            *_SINGLE_ARGS,
            "--personalized-ratio",
            "0.7",
            "--popular-ratio",
            "0.2",
            "--exploration-ratio",
            "0.100000002",
        ]
    )

    assert exit_code == 2
    assert called is False
    assert _json_lines(capsys.readouterr().out)[-1] == {
        "contract_version": "batch-contract-v1",
        "error_type": "invalid_arguments",
        "event": "job_summary",
        "job": "action_log",
        "partition_date": "2026-07-13",
        "status": "failed",
    }


@pytest.mark.parametrize(
    "path",
    [
        "C:/local/action_log",
        "gs://test-bucket//action_log",
        "gs://test-bucket/data/../action_log",
        "gs://test-bucket/data/action_log/",
    ],
)
def test_main_rejects_noncanonical_gcs_path(path, monkeypatch, capsys):
    monkeypatch.setattr(
        action_log_job, "_run", lambda args: pytest.fail("must not run")
    )
    args = list(_SINGLE_ARGS)
    args[-1] = path

    assert action_log_job.main(args) == 2
    assert _json_lines(capsys.readouterr().out)[-1]["status"] == "failed"


def test_merge_rejects_quarantine_input_before_runner(monkeypatch, capsys):
    monkeypatch.setattr(
        action_log_job, "_run", lambda args: pytest.fail("must not run")
    )

    exit_code = action_log_job.main(
        [
            "--mode",
            "merge",
            "--partition-date",
            "2026-07-13",
            "--shard-count",
            "5",
            "--shard-output-base-path",
            "gs://test-bucket/data/action_log_work",
            "--output-base-path",
            "gs://test-bucket/data/action_log",
            "--max-quarantine-ratio",
            "0.2",
            "--quarantine-base-path",
            "gs://test-bucket/data/quarantine",
        ]
    )

    assert exit_code == 2
    assert _json_lines(capsys.readouterr().out)[-1]["error_type"] == "invalid_arguments"


def test_main_maps_runtime_failure_to_exit_1_without_sensitive_detail(
    monkeypatch,
    capsys,
    caplog,
):
    def fail(args):
        raise RuntimeError("OPENROUTER_API_KEY=secret raw-response=user-123")

    monkeypatch.setattr(action_log_job, "_run", fail)

    assert action_log_job.main(_SINGLE_ARGS) == 1

    captured = capsys.readouterr()
    summary = _json_lines(captured.out)[-1]
    assert summary["error_type"] == "runtime_failure"
    assert summary["status"] == "failed"
    combined = captured.out + captured.err + caplog.text
    assert "secret" not in combined
    assert "raw-response" not in combined
    assert "user-123" not in combined


def test_run_maps_shard_arguments_to_domain_runner(monkeypatch):
    filesystem = object()
    captured = {}
    monkeypatch.setattr(action_log_job, "GcsFileSystem", lambda: filesystem)

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"status": "succeeded"}

    monkeypatch.setattr(action_log_job, "run_daily_action_log_shard", fake_run)
    parser = action_log_job._build_parser()
    args = parser.parse_args(
        [
            "--mode",
            "shard",
            "--partition-date",
            "2026-07-13",
            "--youtube-base-path",
            "gs://test-bucket/data/youtube",
            "--virtual-users-path",
            "gs://test-bucket/asset/users.parquet",
            "--output-base-path",
            "gs://test-bucket/data/action_log_work",
            "--progress-base-path",
            "gs://test-bucket/data/action_log_progress",
            "--checkpoint-base-path",
            "gs://test-bucket/data/action_log_checkpoints",
            "--shard-index",
            "0",
            "--shard-count",
            "5",
            "--overwrite",
        ]
    )
    action_log_job._validate_args(args)

    assert action_log_job._run(args) == {"status": "succeeded"}
    assert captured["filesystem"] is filesystem
    assert captured["shard_index"] == 0
    assert captured["shard_count"] == 5
    assert captured["overwrite"] is True


def test_run_maps_merge_without_quarantine_inputs(monkeypatch):
    filesystem = object()
    captured = {}
    monkeypatch.setattr(action_log_job, "GcsFileSystem", lambda: filesystem)

    def fake_merge(**kwargs):
        captured.update(kwargs)
        return {"status": "succeeded"}

    monkeypatch.setattr(action_log_job, "merge_daily_action_log_shards", fake_merge)
    args = SimpleNamespace(
        mode="merge",
        partition_date=action_log_job._partition_date("2026-07-13"),
        shard_count=5,
        shard_output_base_path="gs://test-bucket/data/action_log_work",
        output_base_path="gs://test-bucket/data/action_log",
        max_quarantine_ratio=0.2,
        overwrite=True,
    )

    assert action_log_job._run(args) == {"status": "succeeded"}
    assert captured == {
        "partition_date": args.partition_date,
        "shard_count": 5,
        "shard_output_base_path": "gs://test-bucket/data/action_log_work",
        "output_base_path": "gs://test-bucket/data/action_log",
        "filesystem": filesystem,
        "max_quarantine_ratio": 0.2,
        "overwrite": True,
    }


def test_version_reports_revision_and_contract(monkeypatch, capsys):
    monkeypatch.setattr(action_log_job, "_REVISION", "abc123")
    parser = action_log_job._build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--version"])

    assert exc_info.value.code == 0
    assert json.loads(capsys.readouterr().out) == {
        "application_revision": "abc123",
        "contract_version": "batch-contract-v1",
    }
