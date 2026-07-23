import argparse
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
    "--click-threshold",
    "0.5",
]

_MERGE_ARGS = [
    "--mode",
    "merge",
    "--partition-date",
    "2026-07-13",
    "--shard-count",
    "5",
    "--shard-output-base-path",
    "gs://test-bucket/data_lake/action_log_work",
    "--output-base-path",
    "gs://test-bucket/data_lake/action_log",
    "--max-quarantine-ratio",
    "0.2",
]

_MODE_ARGS = {"single": _SINGLE_ARGS, "merge": _MERGE_ARGS}


def _parse_valid_single_args(*extra: str) -> argparse.Namespace:
    parser = action_log_job._build_parser()
    args = parser.parse_args([*_SINGLE_ARGS, *extra])
    action_log_job._validate_args(args)
    return args


def _parse_args_for_mode(mode: str, *extra: str) -> argparse.Namespace:
    parser = action_log_job._build_parser()
    args = parser.parse_args([*_MODE_ARGS[mode], *extra])
    action_log_job._validate_args(args)
    return args


def test_exposure_source_defaults_to_model_for_single_and_shard():
    assert _parse_valid_single_args().exposure_source == "model"


def test_merge_rejects_exposure_arguments():
    with pytest.raises(action_log_job.BatchArgumentError):
        _parse_args_for_mode("merge", "--exposure-source", "model")
    with pytest.raises(action_log_job.BatchArgumentError):
        _parse_args_for_mode("merge", "--recommendations-table", "t")


def test_merge_parses_and_validates_without_click_threshold():
    # merge_daily_action_log_shards는 click_threshold를 소비하지 않으므로
    # merge 모드는 --click-threshold 없이도 파싱·검증에 성공해야 한다
    # (Airflow 등 실제 merge 호출부는 이 인자를 전달하지 않는다).
    args = _parse_args_for_mode("merge")
    assert args.click_threshold is None


def test_merge_rejects_click_threshold():
    with pytest.raises(action_log_job.BatchArgumentError):
        _parse_args_for_mode("merge", "--click-threshold", "0.5")


def test_single_mode_requires_click_threshold():
    assert _SINGLE_ARGS[-2:] == ["--click-threshold", "0.5"]
    args_without_threshold = _SINGLE_ARGS[:-2]
    parser = action_log_job._build_parser()
    args = parser.parse_args(args_without_threshold)
    assert args.click_threshold is None
    with pytest.raises(action_log_job.BatchArgumentError):
        action_log_job._validate_args(args)


def test_shard_mode_requires_click_threshold():
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
        ]
    )
    assert args.click_threshold is None
    with pytest.raises(action_log_job.BatchArgumentError):
        action_log_job._validate_args(args)


def test_heuristic_mode_rejects_recommendations_table():
    with pytest.raises(action_log_job.BatchArgumentError):
        _parse_args_for_mode(
            "single", "--exposure-source", "heuristic", "--recommendations-table", "t"
        )


def test_run_passes_factory_only_in_model_mode(monkeypatch):
    filesystem = object()
    monkeypatch.setattr(action_log_job, "GcsFileSystem", lambda: filesystem)
    captured: dict = {}

    def _fake_daily(**kwargs):
        captured.update(kwargs)
        return {"status": "succeeded"}

    monkeypatch.setattr(action_log_job, "run_daily_action_log", _fake_daily)

    action_log_job._run(_parse_valid_single_args())
    assert captured["candidate_provider_factory"] is not None

    captured.clear()
    action_log_job._run(
        _parse_valid_single_args("--exposure-source", "heuristic")
    )
    assert captured["candidate_provider_factory"] is None


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


def test_run_maps_single_cli_default_to_no_overwrite(monkeypatch):
    filesystem = object()
    captured = {}
    monkeypatch.setattr(action_log_job, "GcsFileSystem", lambda: filesystem)

    def fake_run(**kwargs):
        captured.update(kwargs)
        return {"status": "succeeded"}

    monkeypatch.setattr(action_log_job, "run_daily_action_log", fake_run)
    parser = action_log_job._build_parser()
    args = parser.parse_args(_SINGLE_ARGS)
    action_log_job._validate_args(args)

    assert action_log_job._run(args) == {"status": "succeeded"}
    assert captured["filesystem"] is filesystem
    assert captured["overwrite"] is False


@pytest.mark.parametrize(
    ("option", "expected"),
    [
        (["--overwrite"], True),
        (["--overwrite=true"], True),
        (["--overwrite=TRUE"], True),
        (["--overwrite=false"], False),
        (["--overwrite=FALSE"], False),
    ],
)
def test_parser_accepts_backward_compatible_overwrite_forms(option, expected):
    parser = action_log_job._build_parser()

    args = parser.parse_args([*_SINGLE_ARGS, *option])

    assert args.overwrite is expected


def test_main_returns_exit_2_for_invalid_overwrite_value(monkeypatch, capsys):
    monkeypatch.setattr(
        action_log_job, "_run", lambda args: pytest.fail("must not run")
    )

    assert action_log_job.main([*_SINGLE_ARGS, "--overwrite=1"]) == 2
    assert _json_lines(capsys.readouterr().out)[-1]["error_type"] == (
        "invalid_arguments"
    )


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
            "--click-threshold",
            "0.5",
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


def test_rerank_api_source_requires_rerank_url():
    with pytest.raises(action_log_job.BatchArgumentError, match="rerank-url"):
        _parse_args_for_mode("single", "--exposure-source", "rerank-api")


def test_rerank_api_source_defaults_timeout_and_parses():
    args = _parse_args_for_mode(
        "single", "--exposure-source", "rerank-api", "--rerank-url", "http://s:8000"
    )
    assert args.rerank_timeout_sec == 30.0


def test_rerank_url_rejected_with_other_sources():
    with pytest.raises(action_log_job.BatchArgumentError, match="rerank-api"):
        _parse_args_for_mode("single", "--rerank-url", "http://s:8000")
    with pytest.raises(action_log_job.BatchArgumentError, match="rerank-api"):
        _parse_args_for_mode(
            "single", "--exposure-source", "heuristic",
            "--rerank-timeout-sec", "5",
        )


def test_rerank_api_source_rejects_shard_mode():
    parser = action_log_job._build_parser()
    args = parser.parse_args(
        [
            "--mode", "shard", "--partition-date", "2026-07-13",
            "--youtube-base-path", "gs://test-bucket/data_lake/youtube",
            "--virtual-users-path", "gs://test-bucket/asset/vu.parquet",
            "--output-base-path", "gs://test-bucket/data_lake/action_log",
            "--click-threshold", "0.5",
            "--shard-index", "0", "--shard-count", "2",
            "--progress-base-path", "gs://test-bucket/progress",
            "--checkpoint-base-path", "gs://test-bucket/checkpoint",
            "--exposure-source", "rerank-api", "--rerank-url", "http://s:8000",
        ]
    )
    with pytest.raises(action_log_job.BatchArgumentError, match="single"):
        action_log_job._validate_args(args)


def test_rerank_api_source_rejects_recommendations_table():
    with pytest.raises(action_log_job.BatchArgumentError, match="recommendations-table"):
        _parse_args_for_mode(
            "single", "--exposure-source", "rerank-api",
            "--rerank-url", "http://s:8000", "--recommendations-table", "t",
        )


def test_run_passes_factory_in_rerank_api_mode(monkeypatch):
    monkeypatch.setattr(action_log_job, "GcsFileSystem", lambda: object())
    captured: dict = {}

    def _fake_daily(**kwargs):
        captured.update(kwargs)
        return {"status": "succeeded"}

    monkeypatch.setattr(action_log_job, "run_daily_action_log", _fake_daily)
    action_log_job._run(
        _parse_valid_single_args(
            "--exposure-source", "rerank-api", "--rerank-url", "http://s:8000"
        )
    )
    assert captured["candidate_provider_factory"] is not None
