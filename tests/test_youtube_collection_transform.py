import math
from datetime import UTC, datetime

import pytest

from autoresearch.youtube_collection.schema import TrendingVideo
from autoresearch.youtube_collection.transform import (
    normalize_api_item,
    normalize_kaggle_row,
)


def _kaggle_row(**overrides):
    """Return a complete Kaggle KR row with the raw (pre-normalization) shape."""
    row = {
        "video_id": "abc123",
        "video_published_at": "2026-06-20T10:00:00Z",
        "video_trending__date": "2026-06-25",
        "video_trending_country": "South Korea",
        "channel_id": "chan1",
        "video_title": "테스트 영상",
        "video_description": "설명",
        "video_default_thumbnail": "https://i.ytimg.com/thumb.jpg",
        "video_category_id": 24,
        "video_tags": ["게임", "플레이"],
        "video_duration": "PT10M15S",
        "video_dimension": "2d",
        "video_definition": "hd",
        "video_licensed_content": False,
        "video_view_count": 42_000_000,
        "video_like_count": 1_500_000,
        "video_comment_count": 23_000,
        "channel_title": "테스트 채널",
        "channel_description": "채널 설명",
        "channel_custom_url": "@test",
        "channel_published_at": "2020-01-01T00:00:00Z",
        "channel_country": "KR",
        "channel_view_count": 1_000_000,
        "channel_subscriber_count": 500_000,
        "channel_have_hidden_subscribers": False,
        "channel_video_count": 300,
        "channel_localized_title": "테스트 채널",
        "channel_localized_description": "채널 설명",
    }
    row.update(overrides)
    return row


def test_normalize_kaggle_row_fixes_typo_key_and_country_name():
    collected_at = datetime(2026, 6, 26, 0, 30, tzinfo=UTC)

    video = normalize_kaggle_row(_kaggle_row(), collected_at=collected_at)

    assert isinstance(video, TrendingVideo)
    assert video.video_trending_date == datetime(2026, 6, 25, 0, 0)
    assert video.video_trending_country == "KR"
    assert video.collected_at == collected_at


def test_normalize_kaggle_row_coerces_nan_missing_string_fields():
    row = _kaggle_row(
        channel_country=math.nan,
        channel_custom_url=math.nan,
        channel_localized_title=math.nan,
        channel_localized_description=math.nan,
        channel_description=None,
    )
    collected_at = datetime(2026, 6, 26, 0, 30, tzinfo=UTC)

    video = normalize_kaggle_row(row, collected_at=collected_at)

    assert video.channel_country == ""
    assert video.channel_custom_url == ""
    assert video.channel_localized_title == ""
    assert video.channel_localized_description == ""
    assert video.channel_description == ""


def test_normalize_kaggle_row_nulls_subscriber_count_when_hidden():
    row = _kaggle_row(
        channel_have_hidden_subscribers=True,
        channel_subscriber_count=math.nan,
    )
    collected_at = datetime(2026, 6, 26, 0, 30, tzinfo=UTC)

    video = normalize_kaggle_row(row, collected_at=collected_at)

    assert video.channel_have_hidden_subscribers is True
    assert video.channel_subscriber_count is None


def test_normalize_kaggle_row_rejects_non_kr_country():
    collected_at = datetime(2026, 6, 26, 0, 30, tzinfo=UTC)
    with pytest.raises(ValueError):
        normalize_kaggle_row(
            _kaggle_row(video_trending_country="United States"),
            collected_at=collected_at,
        )


def test_normalize_kaggle_row_handles_real_all_string_parquet_shapes():
    """Real Kaggle parquet stores every column as string with quirky formats."""
    row = _kaggle_row(
        video_category_id="Sports",  # mislabelled col holds category NAME
        video_trending__date="2024.10.12",  # dot separator
        video_tags="JENNIE,Mantra,제니",  # comma-joined string
        video_view_count="42000000",
        video_like_count="1500000",
        video_comment_count="23000",
        video_licensed_content="False",  # bool-as-string
        channel_view_count="1000000.0",  # float-as-string
        channel_subscriber_count="482000.0",
        channel_have_hidden_subscribers="False",
        channel_video_count="300",
    )
    collected_at = datetime(2026, 6, 26, 0, 30, tzinfo=UTC)

    video = normalize_kaggle_row(row, collected_at=collected_at)

    assert video.video_category == "Sports"  # raw key video_category_id -> video_category
    assert video.video_trending_date == datetime(2024, 10, 12, 0, 0)
    assert video.video_tags == ["JENNIE", "Mantra", "제니"]
    assert video.video_view_count == 42_000_000
    assert video.video_like_count == 1_500_000
    assert video.video_comment_count == 23_000
    assert video.video_licensed_content is False
    assert video.channel_view_count == 1_000_000
    assert video.channel_subscriber_count == 482_000
    assert video.channel_have_hidden_subscribers is False
    assert video.channel_video_count == 300


def test_normalize_kaggle_row_handles_null_tags_and_empty_strings():
    row = _kaggle_row(
        video_tags=None,
        video_description="",
    )
    collected_at = datetime(2026, 6, 26, 0, 30, tzinfo=UTC)

    video = normalize_kaggle_row(row, collected_at=collected_at)

    assert video.video_tags == []
    assert video.video_description == ""


def test_normalize_kaggle_row_handles_null_channel_published_at():
    row = _kaggle_row(channel_published_at=None)
    collected_at = datetime(2026, 6, 26, 0, 30, tzinfo=UTC)

    video = normalize_kaggle_row(row, collected_at=collected_at)

    assert video.channel_published_at is None


def _api_video_item(**overrides) -> dict:
    item = {
        "id": "vid1",
        "snippet": {
            "publishedAt": "2026-06-20T10:00:00Z",
            "channelId": "chan1",
            "title": "테스트",
            "description": "설명",
            "thumbnails": {"default": {"url": "https://i.ytimg.com/vi/vid1/default.jpg"}},
            "categoryId": "24",
            "tags": ["게임", "플레이"],
        },
        "contentDetails": {
            "duration": "PT10M15S",
            "dimension": "2d",
            "definition": "hd",
            "licensedContent": False,
        },
        "statistics": {
            "viewCount": "4820000",
            "likeCount": "150000",
            "commentCount": "2300",
        },
    }
    _deep_update(item, overrides)
    return item


def _api_channel_item(**overrides) -> dict:
    item = {
        "id": "chan1",
        "snippet": {
            "title": "테스트 채널",
            "description": "채널 설명",
            "customUrl": "@test",
            "publishedAt": "2020-01-01T00:00:00Z",
            "country": "KR",
            "localized": {"title": "테스트 채널", "description": "채널 설명"},
        },
        "statistics": {
            "viewCount": "1000000",
            "subscriberCount": "500000",
            "hiddenSubscriberCount": False,
            "videoCount": "300",
        },
    }
    _deep_update(item, overrides)
    return item


def _deep_update(target: dict, overrides: dict) -> None:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def test_normalize_api_item_maps_nested_response_to_schema():
    collected_at = datetime(2026, 6, 25, 15, 30, tzinfo=UTC)

    video = normalize_api_item(
        _api_video_item(),
        _api_channel_item(),
        {"24": "Entertainment"},
        collected_at=collected_at,
    )

    assert isinstance(video, TrendingVideo)
    assert video.video_id == "vid1"
    assert video.video_category == "Entertainment"  # categoryId 24 -> name
    assert video.video_view_count == 4_820_000  # string count -> int
    assert video.video_like_count == 150_000
    assert video.video_comment_count == 2300
    assert video.video_tags == ["게임", "플레이"]
    assert video.video_trending_country == "KR"
    assert video.video_trending_date == collected_at  # snapshot = collect time
    assert video.video_default_thumbnail == "https://i.ytimg.com/vi/vid1/default.jpg"
    assert video.channel_id == "chan1"
    assert video.channel_subscriber_count == 500_000
    assert video.channel_have_hidden_subscribers is False
    assert video.channel_video_count == 300


def test_normalize_api_item_nulls_subscriber_count_when_hidden():
    collected_at = datetime(2026, 6, 25, 15, 30, tzinfo=UTC)

    channel = _api_channel_item()
    channel["statistics"] = {"hiddenSubscriberCount": True, "viewCount": "1000"}

    video = normalize_api_item(
        _api_video_item(), channel, {"24": "Entertainment"}, collected_at=collected_at
    )

    assert video.channel_have_hidden_subscribers is True
    assert video.channel_subscriber_count is None
    assert video.channel_view_count == 1000


def test_normalize_api_item_defaults_missing_metadata_to_empty():
    collected_at = datetime(2026, 6, 25, 15, 30, tzinfo=UTC)

    video = _api_video_item()
    del video["snippet"]["tags"]  # missing tags
    del video["statistics"]["commentCount"]  # comments disabled

    result = normalize_api_item(
        video,
        {"id": "chan1", "snippet": {}, "statistics": {}},
        {"24": "Entertainment"},
        collected_at=collected_at,
    )

    assert result.video_tags == []
    assert result.video_comment_count == 0
    assert result.channel_custom_url == ""
    assert result.channel_localized_title == ""
