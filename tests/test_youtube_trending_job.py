import json
from types import SimpleNamespace

import pytest

import autoresearch.jobs.youtube_trending as youtube_job


_ARGS = [
    "--partition-date",
    "2026-07-13",
    "--youtube-base-path",
    "gs://test-bucket/data_lake/youtube_trending_kr",
]


def _json_lines(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines() if line]


@pytest.mark.parametrize(
    ("option", "expected"),
    [
        ([], False),
        (["--overwrite"], True),
        (["--overwrite=true"], True),
        (["--overwrite=TRUE"], True),
        (["--overwrite=false"], False),
    ],
)
def test_parser_accepts_overwrite_forms(option, expected):
    args = youtube_job._build_parser().parse_args([*_ARGS, *option])

    assert args.overwrite is expected


def test_run_skips_existing_partition_before_loading_credentials(monkeypatch):
    filesystem = SimpleNamespace(
        get_file_info=lambda path: SimpleNamespace(type=SimpleNamespace(name="File"))
    )
    monkeypatch.setattr(youtube_job, "GcsFileSystem", lambda: filesystem)
    monkeypatch.setattr(
        youtube_job,
        "_load_api_keys",
        lambda: pytest.fail("credentials must not be loaded"),
    )
    args = youtube_job._build_parser().parse_args(_ARGS)
    youtube_job._validate_args(args)

    result = youtube_job._run(args)

    assert result == {
        "status": "skipped",
        "output_path": (
            "gs://test-bucket/data_lake/youtube_trending_kr/"
            "dt=2026-07-13/part-0.parquet"
        ),
        "videos": 0,
    }


def test_run_reuses_collection_client_and_load_layers(monkeypatch):
    filesystem = SimpleNamespace(
        get_file_info=lambda path: SimpleNamespace(
            type=SimpleNamespace(name="NotFound")
        )
    )
    captured = {}
    callables = SimpleNamespace(
        list_videos=object(),
        list_channels=object(),
        list_categories=object(),
    )

    class FakeClient:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def make_callables(self):
            return callables

    def fake_collect(*args, **kwargs):
        captured["collect_args"] = args
        captured["collect_kwargs"] = kwargs
        return [object(), object()]

    def fake_write(videos, base_path, partition_date, *, filesystem):
        captured["write"] = (videos, base_path, partition_date, filesystem)

    monkeypatch.setattr(youtube_job, "GcsFileSystem", lambda: filesystem)
    monkeypatch.setattr(youtube_job, "_load_api_keys", lambda: ["key-1", "key-2"])
    monkeypatch.setattr(youtube_job, "ResilientYouTubeClient", FakeClient)
    monkeypatch.setattr(youtube_job, "collect_trending", fake_collect)
    monkeypatch.setattr(youtube_job, "write_partition", fake_write)
    args = youtube_job._build_parser().parse_args(
        [*_ARGS, "--region-code", "us", "--max-results", "10", "--overwrite"]
    )
    youtube_job._validate_args(args)

    result = youtube_job._run(args)

    assert result["status"] == "succeeded"
    assert result["videos"] == 2
    assert captured["client"] == {"keys": ["key-1", "key-2"], "proxy_url": None}
    assert captured["collect_args"] == (
        callables.list_videos,
        callables.list_channels,
        callables.list_categories,
    )
    assert captured["collect_kwargs"]["region_code"] == "US"
    assert captured["collect_kwargs"]["max_results"] == 10
    assert captured["write"][1] == (
        "test-bucket/data_lake/youtube_trending_kr"
    )
    assert captured["write"][3] is filesystem


def test_load_api_keys_prefers_list_and_falls_back_to_single():
    assert youtube_job._load_api_keys(
        {"YOUTUBE_API_KEYS": " key-1, ,key-2 ", "YOUTUBE_API_KEY": "fallback"}
    ) == ["key-1", "key-2"]
    assert youtube_job._load_api_keys({"YOUTUBE_API_KEY": " single "}) == [
        "single"
    ]


def test_main_emits_skipped_summary(monkeypatch, capsys):
    monkeypatch.setattr(
        youtube_job,
        "_run",
        lambda args: {"status": "skipped", "output_path": "gs://bucket/path"},
    )

    assert youtube_job.main(_ARGS) == 0

    summary = _json_lines(capsys.readouterr().out)[-1]
    assert summary["status"] == "skipped"
    assert summary["job"] == "youtube_trending"
    assert summary["partition_date"] == "2026-07-13"


@pytest.mark.parametrize(
    "invalid_args",
    [
        ["--overwrite=yes"],
        ["--max-results", "0"],
        ["--region-code", "KOR"],
    ],
)
def test_main_returns_exit_2_for_invalid_arguments(invalid_args, monkeypatch, capsys):
    monkeypatch.setattr(youtube_job, "_run", lambda args: pytest.fail("must not run"))

    assert youtube_job.main([*_ARGS, *invalid_args]) == 2
    assert _json_lines(capsys.readouterr().out)[-1]["error_type"] == (
        "invalid_arguments"
    )


def test_main_hides_runtime_failure_detail(monkeypatch, capsys, caplog):
    def fail(args):
        raise RuntimeError("YOUTUBE_API_KEY=secret user_id=vu-1")

    monkeypatch.setattr(youtube_job, "_run", fail)

    assert youtube_job.main(_ARGS) == 1

    captured = capsys.readouterr()
    combined = captured.out + captured.err + caplog.text
    assert "secret" not in combined
    assert "vu-1" not in combined
    assert _json_lines(captured.out)[-1]["error_type"] == "runtime_failure"


def test_version_reports_revision_and_contract(monkeypatch, capsys):
    monkeypatch.setattr(youtube_job, "_REVISION", "abc123")

    with pytest.raises(SystemExit) as exc_info:
        youtube_job._build_parser().parse_args(["--version"])

    assert exc_info.value.code == 0
    assert json.loads(capsys.readouterr().out) == {
        "application_revision": "abc123",
        "contract_version": "batch-contract-v1",
    }
