import json
from types import SimpleNamespace

import pytest

import autoresearch.jobs.youtube_backfill as backfill_job


_ARGS = [
    "--source-path",
    "gs://test-bucket/import/youtube.parquet",
    "--youtube-base-path",
    "gs://test-bucket/data_lake/youtube_trending_kr",
    "--overwrite=true",
]


def _json_lines(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines() if line]


@pytest.mark.parametrize("overwrite", [[], ["--overwrite=false"]])
def test_main_requires_explicit_overwrite(overwrite, monkeypatch, capsys):
    monkeypatch.setattr(
        backfill_job,
        "_run",
        lambda args: pytest.fail("backfill must not run"),
    )
    args = _ARGS[:-1] + overwrite

    assert backfill_job.main(args) == 2
    assert _json_lines(capsys.readouterr().out)[-1]["error_type"] == (
        "invalid_arguments"
    )


@pytest.mark.parametrize(
    "invalid_path",
    [
        "bucket/import/youtube.parquet",
        "gs://test-bucket/import//youtube.parquet",
        "gs://test-bucket/import/../youtube.parquet",
    ],
)
def test_main_rejects_noncanonical_gcs_paths(
    invalid_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        backfill_job,
        "_run",
        lambda args: pytest.fail("backfill must not run"),
    )

    assert backfill_job.main([invalid_path if arg == _ARGS[1] else arg for arg in _ARGS]) == 2
    assert _json_lines(capsys.readouterr().out)[-1]["error_type"] == (
        "invalid_arguments"
    )


def test_run_delegates_to_existing_backfill_logic(monkeypatch):
    filesystem = SimpleNamespace()
    captured = {}

    def fake_backfill(source_path, base_path, *, filesystem):
        captured["call"] = (source_path, base_path, filesystem)
        return 123

    monkeypatch.setattr(backfill_job, "GcsFileSystem", lambda: filesystem)
    monkeypatch.setattr(backfill_job, "backfill_from_parquet", fake_backfill)
    args = backfill_job._build_parser().parse_args(_ARGS)
    backfill_job._validate_args(args)

    result = backfill_job._run(args)

    assert captured["call"] == (
        "gs://test-bucket/import/youtube.parquet",
        "test-bucket/data_lake/youtube_trending_kr",
        filesystem,
    )
    assert result == {
        "status": "succeeded",
        "rows": 123,
        "output_base_path": "gs://test-bucket/data_lake/youtube_trending_kr",
        "overwrite": True,
    }


def test_main_emits_success_summary(monkeypatch, capsys):
    monkeypatch.setattr(
        backfill_job,
        "_run",
        lambda args: {"status": "succeeded", "rows": 42},
    )

    assert backfill_job.main(_ARGS) == 0

    summary = _json_lines(capsys.readouterr().out)[-1]
    assert summary == {
        "contract_version": "batch-contract-v1",
        "event": "job_summary",
        "job": "youtube_backfill",
        "rows": 42,
        "status": "succeeded",
    }


def test_main_hides_runtime_failure_detail(monkeypatch, capsys, caplog):
    def fail(args):
        raise RuntimeError("token=secret source=private")

    monkeypatch.setattr(backfill_job, "_run", fail)

    assert backfill_job.main(_ARGS) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err + caplog.text
    assert "secret" not in combined
    assert "private" not in combined
    assert _json_lines(captured.out)[-1]["error_type"] == "runtime_failure"


def test_version_reports_revision_and_contract(monkeypatch, capsys):
    monkeypatch.setattr(backfill_job, "_REVISION", "abc123")

    with pytest.raises(SystemExit) as exc_info:
        backfill_job._build_parser().parse_args(["--version"])

    assert exc_info.value.code == 0
    assert json.loads(capsys.readouterr().out) == {
        "application_revision": "abc123",
        "contract_version": "batch-contract-v1",
    }
