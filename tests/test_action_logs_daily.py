import inspect
import json
import shutil
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pyarrow.fs import FileInfo, FileType

import autoresearch.action_logs.daily as daily_module
from autoresearch.action_logs.daily import (
    merge_daily_action_log_shards,
    run_daily_action_log,
    run_daily_action_log_shard,
)
from autoresearch.action_logs.llm_generator import (
    DEFAULT_OPENROUTER_MODEL,
    OpenRouterActionLogGenerator,
    RuleBasedActionLogGenerator,
)
from autoresearch.action_logs.pipeline import ActionLogGenerationError


class _SelectorFilesystem:
    """FileSelector 호출만 검증하는 GCS filesystem 대역."""

    def __init__(self, infos=None, error: OSError | None = None):
        self.infos = list(infos or [])
        self.error = error
        self.selector = None

    def get_file_info(self, selector):
        self.selector = selector
        if self.error is not None:
            raise self.error
        return self.infos


def test_list_files_treats_missing_gcs_checkpoint_parts_prefix_as_empty():
    filesystem = _SelectorFilesystem()
    parts_path = "bucket/action_log_checkpoints/dt=2026-07-10/shard=000/parts"

    assert daily_module._list_files(parts_path, filesystem=filesystem) == []
    assert filesystem.selector.base_dir == parts_path
    assert filesystem.selector.recursive is False
    assert filesystem.selector.allow_not_found is True


def test_checkpoint_load_completed_restores_existing_parquet_parts(monkeypatch):
    part_path = "bucket/checkpoints/parts/part-work-a.parquet"
    filesystem = _SelectorFilesystem(
        [
            FileInfo(part_path, type=FileType.File),
            FileInfo("bucket/checkpoints/parts/.keep", type=FileType.File),
        ]
    )
    expected_drafts = [SimpleNamespace(event_id="draft-a")]
    monkeypatch.setattr(
        daily_module,
        "read_action_log_checkpoint_part",
        lambda path, filesystem: SimpleNamespace(
            work_id="work-a",
            drafts=expected_drafts,
        ),
    )
    store = daily_module._ActionLogCheckpointStore(
        partition_date=date(2026, 7, 10),
        shard_index=0,
        shard_count=1,
        checkpoint_base_path="bucket/checkpoints",
        config_fingerprint="fingerprint",
        config={},
        filesystem=filesystem,
    )

    assert store.load_completed() == {"work-a": expected_drafts}
    assert filesystem.selector.allow_not_found is True


def test_list_files_propagates_non_missing_filesystem_errors():
    filesystem = _SelectorFilesystem(error=PermissionError("permission denied"))

    with pytest.raises(PermissionError, match="permission denied"):
        daily_module._list_files("bucket/checkpoints/parts", filesystem=filesystem)


def _write_virtual_users(path, count: int = 3):
    users = []
    for i in range(count):
        users.append(
            {
                "user_id": f"vu_{i:04d}",
                "age": 20 + i,
                "sex": "female" if i % 2 == 0 else "male",
                "persona_summary": "테스트 유저",
                "primary_categories": ["Gaming", "Music"],
                "interest_keywords": ["게임", "음악"],
                "hobby_keywords": [],
                "lifestyle_keywords": [],
                "watch_time_band": "night",
            }
        )
    pq.write_table(pa.Table.from_pylist(users), path)


def _write_youtube_partition(base, partition_date: date, count: int = 12):
    partition_dir = base / f"dt={partition_date:%Y-%m-%d}"
    partition_dir.mkdir(parents=True)
    rows = []
    for i in range(count):
        rows.append(
            {
                "video_id": f"yt_{i:04d}",
                "video_title": f"게임 음악 영상 {i}",
                "video_description": "테스트 설명",
                "video_tags": ["게임", "음악"],
                "video_view_count": 10_000 - i,
                "video_like_count": 100 + i,
                "video_comment_count": 10 + i,
                "channel_title": f"채널 {i}",
                "video_published_at": datetime(2026, 7, 1, tzinfo=UTC),
            }
        )
    pq.write_table(pa.Table.from_pylist(rows), partition_dir / "part-0.parquet")


def test_daily_runners_expose_provider_and_expected_count_contract():
    for runner in (run_daily_action_log, run_daily_action_log_shard):
        parameters = inspect.signature(runner).parameters
        assert parameters["provider_routing_mode"].annotation in {str, "str"}
        assert parameters["provider_routing_mode"].default == "default"
        assert parameters["provider_slug"].default is None
        assert parameters["expected_user_count"].default is None


@pytest.mark.parametrize(
    ("expected_user_count", "message"),
    [
        (True, "must be an integer"),
        (0, "must be at least 1"),
        (-1, "must be at least 1"),
    ],
)
def test_expected_user_count_rejects_invalid_values(expected_user_count, message):
    with pytest.raises(ValueError, match=message):
        daily_module._validate_expected_user_count([], expected_user_count)


def test_run_daily_action_log_writes_dt_partition(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    output_base = tmp_path / "action_log"
    quarantine_base = tmp_path / "action_log_quarantine"

    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)

    summary = run_daily_action_log(
        partition_date=partition_date,
        youtube_base_path=str(youtube_base),
        virtual_users_path=str(virtual_users_path),
        output_base_path=str(output_base),
        quarantine_base_path=str(quarantine_base),
        candidates_per_user=5,
        target_ctr=0.2,
        seed=123,
        generator_name="rule_based",
    )

    output_path = output_base / "dt=2026-07-01" / "part-0.parquet"
    quarantine_path = quarantine_base / "dt=2026-07-01" / "quarantine.jsonl"
    table = pq.read_table(output_path)

    assert summary["output_path"] == str(output_path)
    assert summary["impressions"] == 15
    assert summary["clicks"] == 3
    assert table.num_rows == summary["total_events"]
    assert quarantine_path.exists()


def test_run_daily_action_log_accepts_expected_user_count_100(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    output_base = tmp_path / "action_log"

    _write_virtual_users(virtual_users_path, count=100)
    _write_youtube_partition(youtube_base, partition_date, count=1)

    summary = run_daily_action_log(
        partition_date=partition_date,
        youtube_base_path=str(youtube_base),
        virtual_users_path=str(virtual_users_path),
        output_base_path=str(output_base),
        candidates_per_user=1,
        expected_user_count=100,
    )

    assert summary["users"] == 100
    assert summary["impressions"] == 100


def test_run_daily_action_log_rejects_expected_user_count_mismatch_before_generator(
    tmp_path,
    monkeypatch,
):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    output_base = tmp_path / "action_log"

    _write_virtual_users(virtual_users_path, count=99)
    _write_youtube_partition(youtube_base, partition_date, count=1)
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda *args, **kwargs: pytest.fail("generator must not be built"),
    )

    with pytest.raises(
        ValueError,
        match=r"expected_user_count=100, actual_user_count=99",
    ):
        run_daily_action_log(
            partition_date=partition_date,
            youtube_base_path=str(youtube_base),
            virtual_users_path=str(virtual_users_path),
            output_base_path=str(output_base),
            candidates_per_user=1,
            expected_user_count=100,
        )

    assert not output_base.exists()


def test_run_daily_action_log_shard_rejects_expected_count_before_split_and_generator(
    tmp_path,
    monkeypatch,
):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    output_base = tmp_path / "action_log_work"

    _write_virtual_users(virtual_users_path, count=99)
    _write_youtube_partition(youtube_base, partition_date, count=1)
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda *args, **kwargs: pytest.fail("generator must not be built"),
    )
    monkeypatch.setattr(
        daily_module,
        "_select_virtual_user_shard",
        lambda *args, **kwargs: pytest.fail("users must not be split"),
    )

    with pytest.raises(
        ValueError,
        match=r"expected_user_count=100, actual_user_count=99",
    ):
        run_daily_action_log_shard(
            partition_date=partition_date,
            shard_index=0,
            shard_count=2,
            youtube_base_path=str(youtube_base),
            virtual_users_path=str(virtual_users_path),
            output_base_path=str(output_base),
            progress_base_path=str(tmp_path / "progress"),
            checkpoint_base_path=str(tmp_path / "checkpoints"),
            candidates_per_user=1,
            expected_user_count=100,
        )

    assert not output_base.exists()


@pytest.mark.parametrize(
    ("provider_routing_mode", "provider_slug"),
    [("auto", None), ("fixed", "deepinfra")],
)
def test_rule_based_generator_rejects_non_default_provider_routing(
    provider_routing_mode,
    provider_slug,
):
    with pytest.raises(ValueError, match="only supported.*openrouter"):
        daily_module._build_generator(
            "rule_based",
            provider_routing_mode=provider_routing_mode,
            provider_slug=provider_slug,
        )


def test_build_generator_passes_normalized_fixed_provider_slug(monkeypatch):
    captured = {}
    sentinel = SimpleNamespace(model_name=DEFAULT_OPENROUTER_MODEL)

    def _openrouter_factory(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        daily_module,
        "OpenRouterActionLogGenerator",
        _openrouter_factory,
    )

    generator = daily_module._build_generator(
        "openrouter",
        model_name=DEFAULT_OPENROUTER_MODEL,
        provider_routing_mode="fixed",
        provider_slug=" DeepInfra ",
    )

    assert generator is sentinel
    assert captured == {
        "model_name": DEFAULT_OPENROUTER_MODEL,
        "provider_routing_mode": "fixed",
        "provider_slug": "deepinfra",
    }


def test_run_daily_action_log_keeps_event_timestamps_inside_partition_date(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    output_base = tmp_path / "action_log"

    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)

    run_daily_action_log(
        partition_date=partition_date,
        youtube_base_path=str(youtube_base),
        virtual_users_path=str(virtual_users_path),
        output_base_path=str(output_base),
        candidates_per_user=5,
        target_ctr=0.2,
        seed=123,
        generator_name="rule_based",
    )

    table = pq.read_table(output_base / "dt=2026-07-01" / "part-0.parquet")
    kst = ZoneInfo("Asia/Seoul")
    assert {
        row["event_timestamp"].astimezone(kst).date()
        for row in table.select(["event_timestamp"]).to_pylist()
    } == {partition_date}


def test_run_daily_action_log_rejects_timestamp_outside_partition_date(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    output_base = tmp_path / "action_log"

    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)

    with pytest.raises(ValueError, match="outside partition_date"):
        run_daily_action_log(
            partition_date=partition_date,
            youtube_base_path=str(youtube_base),
            virtual_users_path=str(virtual_users_path),
            output_base_path=str(output_base),
            candidates_per_user=5,
            target_ctr=0.2,
            seed=123,
            generator_name="rule_based",
            history_end=datetime(2026, 7, 3, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        )


def test_sharded_daily_action_log_merges_global_partition(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "action_log_work"
    work_quarantine_base = tmp_path / "action_log_quarantine_work"
    output_base = tmp_path / "action_log"
    quarantine_base = tmp_path / "action_log_quarantine"

    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)

    for shard_index in range(2):
        run_daily_action_log_shard(
            partition_date=partition_date,
            shard_index=shard_index,
            shard_count=2,
            youtube_base_path=str(youtube_base),
            virtual_users_path=str(virtual_users_path),
            output_base_path=str(work_base),
            quarantine_base_path=str(work_quarantine_base),
            candidates_per_user=5,
            target_ctr=0.2,
            seed=123,
            generator_name="rule_based",
        )

    shard_path = work_base / "dt=2026-07-01" / "shard=000" / "part-0.parquet"
    shard_table = pq.read_table(shard_path)
    manifest_path = work_base / "dt=2026-07-01" / "shard=000" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "event_id" not in shard_table.column_names
    assert shard_table.num_rows == 5
    assert manifest == {
        "manifest_version": "action_log_shard_manifest_v1",
        "partition_date": "2026-07-01",
        "shard_index": 0,
        "shard_count": 2,
        "generator": "rule_based",
        "model_name": "fixture-rule-action-log",
        "generator_config": {},
        "candidates_per_user": 5,
        "target_ctr": 0.2,
        "personalized_ratio": 0.7,
        "popular_ratio": 0.2,
        "exploration_ratio": 0.1,
        "seed": 123,
        "chunk_size": 0,
        "max_quarantine_ratio": 0.5,
        "history_end": "2026-07-01T15:00:00Z",
        "total_work": 1,
        "completed_work": 1,
        "quarantine_count": 0,
        "schema_version": "action_log_schema_v1",
        "prompt_version": "action_log_ctr_v2",
        "input_fingerprint": manifest["input_fingerprint"],
        "config_fingerprint": manifest["config_fingerprint"],
    }
    assert len(manifest["input_fingerprint"]) == 64
    assert len(manifest["config_fingerprint"]) == 64

    summary = merge_daily_action_log_shards(
        partition_date=partition_date,
        shard_count=2,
        shard_output_base_path=str(work_base),
        output_base_path=str(output_base),
        shard_quarantine_base_path=str(work_quarantine_base),
        quarantine_base_path=str(quarantine_base),
    )

    output_path = output_base / "dt=2026-07-01" / "part-0.parquet"
    quarantine_path = quarantine_base / "dt=2026-07-01" / "quarantine.jsonl"
    table = pq.read_table(output_path)
    rows = table.to_pylist()
    event_ids = [row["event_id"] for row in rows]

    assert summary["impressions"] == 15
    assert summary["clicks"] == 3
    assert table.num_rows == summary["total_events"]
    assert event_ids == [f"evt_{index:08d}" for index in range(len(rows))]
    assert set(table["llm_model"].to_pylist()) == {"fixture-rule-action-log"}
    assert quarantine_path.exists()


def test_shard_merge_matches_single_run_event_contract(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    single_base = tmp_path / "single"
    work_base = tmp_path / "work"
    merged_base = tmp_path / "merged"

    _write_virtual_users(virtual_users_path, count=6)
    _write_youtube_partition(youtube_base, partition_date)
    common = {
        "partition_date": partition_date,
        "youtube_base_path": str(youtube_base),
        "virtual_users_path": str(virtual_users_path),
        "candidates_per_user": 5,
        "target_ctr": 0.2,
        "seed": 123,
        "generator_name": "rule_based",
    }
    single_summary = run_daily_action_log(
        **common,
        output_base_path=str(single_base),
    )
    for shard_index in range(3):
        run_daily_action_log_shard(
            **common,
            shard_index=shard_index,
            shard_count=3,
            output_base_path=str(work_base),
        )
    merged_summary = merge_daily_action_log_shards(
        partition_date=partition_date,
        shard_count=3,
        shard_output_base_path=str(work_base),
        output_base_path=str(merged_base),
    )

    single = pq.read_table(single_base / "dt=2026-07-01" / "part-0.parquet").to_pylist()
    merged = pq.read_table(merged_base / "dt=2026-07-01" / "part-0.parquet").to_pylist()
    single_clicked = {
        (row["user_id"], row["video_id"])
        for row in single
        if row["event_type"] == "click"
    }
    merged_clicked = {
        (row["user_id"], row["video_id"])
        for row in merged
        if row["event_type"] == "click"
    }

    assert [row["event_id"] for row in merged] == [row["event_id"] for row in single]
    assert merged_clicked == single_clicked
    assert merged_summary["ctr"] == single_summary["ctr"]
    assert {row["llm_model"] for row in merged} == {
        row["llm_model"] for row in single
    } == {"fixture-rule-action-log"}


def test_merge_rejects_missing_or_tampered_shard_manifest(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "work"

    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)
    for shard_index in range(2):
        run_daily_action_log_shard(
            partition_date=partition_date,
            shard_index=shard_index,
            shard_count=2,
            youtube_base_path=str(youtube_base),
            virtual_users_path=str(virtual_users_path),
            output_base_path=str(work_base),
            candidates_per_user=5,
            seed=123,
        )

    manifest_path = work_base / "dt=2026-07-01" / "shard=001" / "manifest.json"
    original = manifest_path.read_text(encoding="utf-8")
    manifest_path.unlink()
    with pytest.raises(ValueError, match="missing shard manifest"):
        merge_daily_action_log_shards(
            partition_date=partition_date,
            shard_count=2,
            shard_output_base_path=str(work_base),
            output_base_path=str(tmp_path / "merged"),
        )

    manifest_path.write_text(original, encoding="utf-8")
    payload = json.loads(original)
    payload["model_name"] = ""
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="model_name"):
        merge_daily_action_log_shards(
            partition_date=partition_date,
            shard_count=2,
            shard_output_base_path=str(work_base),
            output_base_path=str(tmp_path / "merged"),
        )


class _OneUserFailureGenerator(RuleBasedActionLogGenerator):
    def generate(self, virtual_user, videos):
        if virtual_user["user_id"] == "vu_0000":
            return "{not valid json"
        return super().generate(virtual_user, videos)


class _CountingGenerator(RuleBasedActionLogGenerator):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def generate(self, virtual_user, videos):
        self.calls += 1
        return super().generate(virtual_user, videos)


@pytest.mark.parametrize(
    ("max_quarantine_ratio", "should_raise"),
    [(0.4, False), (0.2, True)],
)
def test_quarantine_guard_is_global_at_merge(
    tmp_path,
    monkeypatch,
    max_quarantine_ratio,
    should_raise,
):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "work"
    work_quarantine_base = tmp_path / "work_quarantine"

    _write_virtual_users(virtual_users_path, count=4)
    _write_youtube_partition(youtube_base, partition_date)
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda generator_name, model_name=None, **kwargs: _OneUserFailureGenerator(),
    )

    summaries = []
    for shard_index in range(2):
        summaries.append(
            run_daily_action_log_shard(
                partition_date=partition_date,
                shard_index=shard_index,
                shard_count=2,
                youtube_base_path=str(youtube_base),
                virtual_users_path=str(virtual_users_path),
                output_base_path=str(work_base),
                quarantine_base_path=str(work_quarantine_base),
                candidates_per_user=5,
                target_ctr=0.2,
                max_quarantine_ratio=max_quarantine_ratio,
            )
        )

    assert summaries[0]["drafts"] == 5
    assert summaries[0]["quarantined_users"] == 1
    def merge():
        return merge_daily_action_log_shards(
            partition_date=partition_date,
            shard_count=2,
            shard_output_base_path=str(work_base),
            output_base_path=str(tmp_path / "merged"),
            shard_quarantine_base_path=str(work_quarantine_base),
        )
    if should_raise:
        with pytest.raises(ActionLogGenerationError, match="global quarantine ratio"):
            merge()
    else:
        summary = merge()
        assert summary["quarantine_count"] == 1
        assert summary["total_work"] == 4


def test_run_daily_action_log_shard_writes_progress_json_dt_shard_path(tmp_path):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "action_log_work"

    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)

    summary = run_daily_action_log_shard(
        partition_date=partition_date,
        shard_index=1,
        shard_count=2,
        youtube_base_path=str(youtube_base),
        virtual_users_path=str(virtual_users_path),
        output_base_path=str(work_base),
        candidates_per_user=5,
        target_ctr=0.2,
        seed=123,
        generator_name="rule_based",
        progress_flush_chunks=1,
    )

    progress_path = (
        tmp_path
        / "action_log_progress"
        / "dt=2026-07-01"
        / "shard=001"
        / "progress.json"
    )
    payload = json.loads(progress_path.read_text(encoding="utf-8"))

    assert summary["progress_path"] == str(progress_path)
    assert payload["partition_date"] == "2026-07-01"
    assert payload["shard_index"] == 1
    assert payload["shard_count"] == 2
    assert payload["status"] == "success"
    assert payload["completed_chunks"] == payload["total_chunks"] == 2
    assert payload["success_chunks"] == 2
    assert payload["failed_chunks"] == 0
    assert payload["quarantined_chunks"] == 0
    assert payload["updated_at"].endswith("Z")


def test_run_daily_action_log_shard_ignores_progress_writer_failure(
    tmp_path,
    monkeypatch,
):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "action_log_work"

    _write_virtual_users(virtual_users_path)
    _write_youtube_partition(youtube_base, partition_date)

    def _raise_progress_write(payload, path, *, filesystem=None):
        raise OSError("progress write failed")

    monkeypatch.setattr(
        daily_module,
        "_write_progress_json_file",
        _raise_progress_write,
    )

    summary = run_daily_action_log_shard(
        partition_date=partition_date,
        shard_index=0,
        shard_count=2,
        youtube_base_path=str(youtube_base),
        virtual_users_path=str(virtual_users_path),
        output_base_path=str(work_base),
        candidates_per_user=5,
        target_ctr=0.2,
        seed=123,
        generator_name="rule_based",
        progress_flush_chunks=1,
    )

    output_path = work_base / "dt=2026-07-01" / "shard=000" / "part-0.parquet"
    assert summary["output_path"] == str(output_path)
    assert output_path.exists()


def test_shard_resume_calls_only_unfinished_work_after_interruption(
    tmp_path,
    monkeypatch,
):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "action_log_work"
    generator = _CountingGenerator()

    _write_virtual_users(virtual_users_path, count=3)
    _write_youtube_partition(youtube_base, partition_date)
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda generator_name, model_name=None, **kwargs: generator,
    )
    original_write_part = daily_module._ActionLogCheckpointStore.write_part
    interrupted = False

    def _write_then_interrupt(self, work_id, work_order, drafts):
        nonlocal interrupted
        original_write_part(self, work_id, work_order, drafts)
        if not interrupted:
            interrupted = True
            raise RuntimeError("simulated interruption")

    monkeypatch.setattr(
        daily_module._ActionLogCheckpointStore,
        "write_part",
        _write_then_interrupt,
    )
    kwargs = {
        "partition_date": partition_date,
        "shard_index": 0,
        "shard_count": 1,
        "youtube_base_path": str(youtube_base),
        "virtual_users_path": str(virtual_users_path),
        "output_base_path": str(work_base),
        "candidates_per_user": 4,
        "chunk_size": 2,
        "max_concurrency": 1,
    }

    with pytest.raises(RuntimeError, match="simulated interruption"):
        run_daily_action_log_shard(**kwargs)
    assert generator.calls == 1

    monkeypatch.setattr(
        daily_module._ActionLogCheckpointStore,
        "write_part",
        original_write_part,
    )
    calls_before_resume = generator.calls
    summary = run_daily_action_log_shard(**kwargs)

    assert summary["total_work"] == 6
    assert generator.calls - calls_before_resume == 5
    assert Path(summary["manifest_path"]).exists()
    assert Path(summary["output_path"]).exists()


def test_checkpoint_fingerprint_isolates_changed_config(tmp_path, monkeypatch):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "action_log_work"
    generator = _CountingGenerator()

    _write_virtual_users(virtual_users_path, count=2)
    _write_youtube_partition(youtube_base, partition_date)
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda generator_name, model_name=None, **kwargs: generator,
    )
    common = {
        "partition_date": partition_date,
        "shard_index": 0,
        "shard_count": 1,
        "youtube_base_path": str(youtube_base),
        "virtual_users_path": str(virtual_users_path),
        "output_base_path": str(work_base),
        "candidates_per_user": 4,
        "chunk_size": 2,
    }

    first = run_daily_action_log_shard(**common, seed=1)
    calls_after_first = generator.calls
    second = run_daily_action_log_shard(**common, seed=2)

    assert first["config_fingerprint"] != second["config_fingerprint"]
    assert first["checkpoint_path"] != second["checkpoint_path"]
    assert generator.calls - calls_after_first == second["total_work"]


def test_provider_routing_mode_isolates_shard_checkpoint_fingerprint(
    tmp_path,
):
    partition_date = date(2026, 7, 1)
    request = daily_module._build_request(
        partition_date=partition_date,
        tmp_dir=tmp_path,
        candidates_per_user=24,
        target_ctr=0.02,
        personalized_ratio=0.7,
        popular_ratio=0.2,
        exploration_ratio=0.1,
        seed=42,
        max_concurrency=1,
        chunk_size=0,
        max_quarantine_ratio=0.5,
        history_end=None,
    )
    auto_generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        provider_routing_mode="auto",
    )
    fixed_generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        provider_routing_mode="fixed",
        provider_slug="deepinfra",
    )
    auto_config = dict(auto_generator.fingerprint_config)
    fixed_config = dict(fixed_generator.fingerprint_config)
    common = {
        "generator_name": "openrouter",
        "model_name": DEFAULT_OPENROUTER_MODEL,
        "input_fingerprint": "a" * 64,
        "request": request,
    }

    auto_fingerprint = daily_module._config_fingerprint(
        **common,
        generator_config=auto_config,
    )
    fixed_fingerprint = daily_module._config_fingerprint(
        **common,
        generator_config=fixed_config,
    )
    auto_store = daily_module._ActionLogCheckpointStore(
        partition_date=partition_date,
        shard_index=0,
        shard_count=1,
        checkpoint_base_path=str(tmp_path / "checkpoints"),
        config_fingerprint=auto_fingerprint,
        config={},
    )
    fixed_store = daily_module._ActionLogCheckpointStore(
        partition_date=partition_date,
        shard_index=0,
        shard_count=1,
        checkpoint_base_path=str(tmp_path / "checkpoints"),
        config_fingerprint=fixed_fingerprint,
        config={},
    )

    assert auto_generator.model_name == fixed_generator.model_name
    assert auto_fingerprint != fixed_fingerprint
    assert auto_store.namespace_path != fixed_store.namespace_path
    assert auto_config["provider_routing_mode"] == "auto"
    assert auto_config["provider_preferences"] == {}
    assert fixed_config["provider_routing_mode"] == "fixed"
    assert fixed_config["provider_slug"] == "deepinfra"
    assert fixed_config["provider_preferences"] == {
        "only": ["deepinfra"],
        "allow_fallbacks": False,
    }


def test_prompt_version_change_isolates_checkpoint_and_supports_rollback(
    tmp_path,
    monkeypatch,
):
    """프롬프트 버전 rollout은 namespace를 분리하고 rollback을 보존한다."""
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "action_log_work"
    generator = _CountingGenerator()

    _write_virtual_users(virtual_users_path, count=2)
    _write_youtube_partition(youtube_base, partition_date)
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda generator_name, model_name=None, **kwargs: generator,
    )
    common = {
        "partition_date": partition_date,
        "shard_index": 0,
        "shard_count": 1,
        "youtube_base_path": str(youtube_base),
        "virtual_users_path": str(virtual_users_path),
        "output_base_path": str(work_base),
        "candidates_per_user": 4,
        "chunk_size": 2,
    }

    monkeypatch.setattr(daily_module, "PROMPT_VERSION", "action_log_ctr_v1")
    v1_first = run_daily_action_log_shard(**common)
    calls_after_v1 = generator.calls

    monkeypatch.setattr(daily_module, "PROMPT_VERSION", "action_log_ctr_v2")
    v2 = run_daily_action_log_shard(**common)
    calls_after_v2 = generator.calls

    assert v1_first["config_fingerprint"] != v2["config_fingerprint"]
    assert v1_first["checkpoint_path"] != v2["checkpoint_path"]
    assert calls_after_v2 - calls_after_v1 == v2["total_work"]

    monkeypatch.setattr(daily_module, "PROMPT_VERSION", "action_log_ctr_v1")
    v1_rollback = run_daily_action_log_shard(**common)

    assert v1_rollback["config_fingerprint"] == v1_first["config_fingerprint"]
    assert v1_rollback["checkpoint_path"] == v1_first["checkpoint_path"]
    assert generator.calls == calls_after_v2


def test_checkpoint_fingerprint_isolates_changed_input_content(tmp_path, monkeypatch):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "action_log_work"
    generator = _CountingGenerator()

    _write_virtual_users(virtual_users_path, count=2)
    _write_youtube_partition(youtube_base, partition_date)
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda generator_name, model_name=None, **kwargs: generator,
    )
    kwargs = {
        "partition_date": partition_date,
        "shard_index": 0,
        "shard_count": 1,
        "youtube_base_path": str(youtube_base),
        "virtual_users_path": str(virtual_users_path),
        "output_base_path": str(work_base),
        "candidates_per_user": 4,
        "chunk_size": 2,
    }

    first = run_daily_action_log_shard(**kwargs)
    users = pq.read_table(virtual_users_path).to_pylist()
    users[0]["persona_summary"] = "변경된 입력"
    pq.write_table(pa.Table.from_pylist(users), virtual_users_path)
    calls_after_first = generator.calls
    second = run_daily_action_log_shard(**kwargs)

    assert first["config_fingerprint"] != second["config_fingerprint"]
    assert first["checkpoint_path"] != second["checkpoint_path"]
    assert generator.calls - calls_after_first == second["total_work"]


def test_checkpoint_duplicate_parts_are_deduplicated_by_work_id(
    tmp_path,
    monkeypatch,
):
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube_trending_kr"
    work_base = tmp_path / "action_log_work"
    first_generator = _CountingGenerator()

    _write_virtual_users(virtual_users_path, count=2)
    _write_youtube_partition(youtube_base, partition_date)
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda generator_name, model_name=None, **kwargs: first_generator,
    )
    kwargs = {
        "partition_date": partition_date,
        "shard_index": 0,
        "shard_count": 1,
        "youtube_base_path": str(youtube_base),
        "virtual_users_path": str(virtual_users_path),
        "output_base_path": str(work_base),
        "candidates_per_user": 4,
        "chunk_size": 2,
    }
    first = run_daily_action_log_shard(**kwargs)
    parts_dir = Path(first["checkpoint_path"]) / "parts"
    source = sorted(parts_dir.glob("*.parquet"))[0]
    shutil.copyfile(source, parts_dir / "duplicate.parquet")

    resumed_generator = _CountingGenerator()
    monkeypatch.setattr(
        daily_module,
        "_build_generator",
        lambda generator_name, model_name=None, **kwargs: resumed_generator,
    )
    second = run_daily_action_log_shard(**kwargs)

    assert resumed_generator.calls == 0
    assert second["drafts"] == first["drafts"]
