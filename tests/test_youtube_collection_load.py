from datetime import date, datetime

import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.youtube_collection.load import write_partition
from autoresearch.youtube_collection.schema import TrendingVideo


def _video(video_id: str = "v1", **overrides) -> TrendingVideo:
    fields = {
        "video_id": video_id,
        "video_published_at": "2026-06-20T10:00:00Z",
        "video_trending_date": "2026-06-25T00:00:00Z",
        "video_trending_country": "KR",
        "video_title": f"영상 {video_id}",
        "video_description": "",
        "video_default_thumbnail": "",
        "video_category": "Sports",
        "video_tags": [],
        "video_duration": "PT1M",
        "video_dimension": "2d",
        "video_definition": "hd",
        "video_licensed_content": False,
        "video_view_count": 1000,
        "video_like_count": 50,
        "video_comment_count": 5,
        "channel_id": "chan1",
        "channel_title": "채널",
        "channel_description": "",
        "channel_custom_url": "",
        "channel_published_at": "2020-01-01T00:00:00Z",
        "channel_country": "KR",
        "channel_view_count": 100_000,
        "channel_subscriber_count": 10_000,
        "channel_have_hidden_subscribers": False,
        "channel_video_count": 300,
        "channel_localized_title": "",
        "channel_localized_description": "",
        "collected_at": "2026-06-26T00:30:00Z",
    }
    fields.update(overrides)
    return TrendingVideo(**fields)


def test_write_partition_creates_dt_hive_path(tmp_path):
    base = tmp_path / "lake" / "youtube_trending_kr"

    path = write_partition([_video("v1"), _video("v2")], str(base), date(2026, 6, 25))

    assert path.endswith("dt=2026-06-25/part-0.parquet")
    table = pq.read_table(path)
    assert table.num_rows == 2
    assert "video_id" in table.column_names
    raw_columns = pq.ParquetFile(path).schema_arrow.names
    assert "dt" not in raw_columns  # dt lives in the hive path, not stored as data
    assert "dt" in table.column_names  # hive-aware read materializes it for free


def test_write_partition_is_idempotent(tmp_path):
    base = tmp_path / "lake" / "youtube_trending_kr"

    write_partition([_video("v1"), _video("v2")], str(base), date(2026, 6, 25))
    write_partition([_video("v9")], str(base), date(2026, 6, 25))

    table = pq.read_table(str(base / "dt=2026-06-25" / "part-0.parquet"))
    assert table.num_rows == 1


def test_write_partition_preserves_list_and_null_fields(tmp_path):
    base = tmp_path / "lake"
    video = _video(
        "v1",
        video_tags=["게임", "플레이"],
        channel_have_hidden_subscribers=True,
        channel_subscriber_count=None,
    )

    write_partition([video], str(base), date(2026, 6, 25))

    table = pq.read_table(str(base / "dt=2026-06-25" / "part-0.parquet"))
    row = table.to_pylist()[0]
    assert row["video_tags"] == ["게임", "플레이"]
    assert row["channel_subscriber_count"] is None
    assert isinstance(row["collected_at"], datetime)


def test_to_table_keeps_int64_type_when_all_subscribers_none():
    # 한 파티션의 channel_subscriber_count 가 전부 None 이어도 int64 로 고정.
    # (from_pylist 자동 추론이면 null 타입이 돼 인접 파티션과 스키마 충돌.)
    from autoresearch.youtube_collection.load import _to_table

    video = _video("v1", channel_subscriber_count=None)
    table = _to_table([video])
    assert table.schema.field("channel_subscriber_count").type == pa.int64()
