import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import autoresearch.action_logs.daily as daily_module
from autoresearch.action_logs.daily import (
    merge_daily_action_log_shards,
    run_daily_action_log,
    run_daily_action_log_shard,
)
from autoresearch.action_logs.llm_generator import RuleBasedActionLogGenerator
from autoresearch.action_logs.pipeline import ActionLogGenerationError


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
        "prompt_version": "action_log_ctr_v1",
        "config_fingerprint": manifest["config_fingerprint"],
    }
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
        lambda generator_name, model_name=None: _OneUserFailureGenerator(),
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
