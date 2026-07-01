from datetime import UTC, date, datetime

import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.youtube_collection.backfill import backfill_from_parquet


def _raw_row(video_id: str, trending_date: date, country: str) -> dict:
    return {
        "video_id": video_id,
        "video_published_at": "2024-10-10T10:00:00Z",
        "video_trending__date": trending_date,
        "video_trending_country": country,
        "channel_id": "chan1",
        "video_title": f"영상 {video_id}",
        "video_description": "",
        "video_default_thumbnail": "",
        "video_category_id": "Sports",  # raw parquet col name (holds a category name)
        "video_tags": [],
        "video_duration": "PT1M",
        "video_dimension": "2d",
        "video_definition": "hd",
        "video_licensed_content": False,
        "video_view_count": 1000,
        "video_like_count": 50,
        "video_comment_count": 5,
        "channel_title": "채널",
        "channel_description": "",
        "channel_custom_url": "",
        "channel_published_at": "2020-01-01T00:00:00Z",
        "channel_country": "",
        "channel_view_count": 100_000,
        "channel_subscriber_count": 10_000,
        "channel_have_hidden_subscribers": False,
        "channel_video_count": 300,
        "channel_localized_title": "",
        "channel_localized_description": "",
    }


def test_backfill_filters_kr_and_partitions_by_trending_date(tmp_path):
    rows = [
        _raw_row("v1", date(2024, 10, 12), "South Korea"),
        _raw_row("v2", date(2024, 10, 12), "South Korea"),
        _raw_row("v3", date(2024, 10, 13), "South Korea"),
        _raw_row("v9", date(2024, 10, 12), "United States"),  # must be excluded
    ]
    source = tmp_path / "global.parquet"
    pq.write_table(pa.Table.from_pylist(rows), source)

    base = tmp_path / "lake"
    collected_at = datetime(2026, 6, 26, 0, 30, tzinfo=UTC)

    total = backfill_from_parquet(str(source), str(base), collected_at=collected_at)

    assert total == 3
    t_1012 = pq.read_table(str(base / "dt=2024-10-12" / "part-0.parquet"))
    t_1013 = pq.read_table(str(base / "dt=2024-10-13" / "part-0.parquet"))
    assert t_1012.num_rows == 2
    assert t_1013.num_rows == 1


def test_backfill_normalizes_written_rows(tmp_path):
    rows = [_raw_row("v1", date(2024, 10, 12), "South Korea")]
    source = tmp_path / "global.parquet"
    pq.write_table(pa.Table.from_pylist(rows), source)

    base = tmp_path / "lake"
    collected_at = datetime(2026, 6, 26, 0, 30, tzinfo=UTC)

    backfill_from_parquet(str(source), str(base), collected_at=collected_at)

    row = pq.read_table(str(base / "dt=2024-10-12" / "part-0.parquet")).to_pylist()[0]
    assert row["video_trending_country"] == "KR"  # full name -> code
    assert row["collected_at"] == collected_at


def test_backfill_skips_malformed_rows_instead_of_crashing(tmp_path):
    good = _raw_row("v1", date(2024, 10, 12), "South Korea")
    bad = _raw_row("v2", date(2024, 10, 12), "South Korea")
    bad["video_published_at"] = None  # non-Optional datetime 결측 -> 정규화 ValueError -> skip
    rows = [good, bad]
    source = tmp_path / "global.parquet"
    pq.write_table(pa.Table.from_pylist(rows), source)

    base = tmp_path / "lake"
    collected_at = datetime(2026, 6, 26, 0, 30, tzinfo=UTC)

    total = backfill_from_parquet(str(source), str(base), collected_at=collected_at)

    assert total == 1  # malformed 행은 건너뛰고 정상 1행만 적재
