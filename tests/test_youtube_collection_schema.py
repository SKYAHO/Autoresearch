import pytest
from pydantic import ValidationError

from autoresearch.youtube_collection.schema import (
    SCHEMA_VERSION,
    TARGET_COUNTRY,
    TrendingVideo,
)


def test_schema_constants_define_collection_contract():
    assert SCHEMA_VERSION
    assert TARGET_COUNTRY == "KR"


def test_trending_video_accepts_fully_normalized_kr_row():
    video = TrendingVideo(
        video_id="abc123",
        video_published_at="2026-06-20T10:00:00Z",
        video_trending_date="2026-06-25T00:00:00Z",
        video_trending_country="KR",
        video_title="테스트 영상",
        video_description="설명",
        video_default_thumbnail="https://i.ytimg.com/thumb.jpg",
        video_category="Sports",
        video_tags=["게임", "플레이"],
        video_duration="PT10M15S",
        video_dimension="2d",
        video_definition="hd",
        video_licensed_content=False,
        video_view_count=42_000_000,
        video_like_count=1_500_000,
        video_comment_count=23_000,
        channel_id="chan1",
        channel_title="테스트 채널",
        channel_description="채널 설명",
        channel_custom_url="@test",
        channel_published_at="2020-01-01T00:00:00Z",
        channel_country="KR",
        channel_view_count=1_000_000,
        channel_subscriber_count=500_000,
        channel_have_hidden_subscribers=False,
        channel_video_count=300,
        channel_localized_title="테스트 채널",
        channel_localized_description="채널 설명",
        collected_at="2026-06-25T15:30:00Z",
    )

    assert video.video_id == "abc123"
    assert video.video_trending_country == "KR"
    assert video.video_view_count == 42_000_000
    assert video.video_tags == ["게임", "플레이"]
    assert video.collected_at.year == 2026


def test_trending_video_rejects_negative_view_count():
    with pytest.raises(ValidationError):
        TrendingVideo(
            video_id="abc123",
            video_published_at="2026-06-20T10:00:00Z",
            video_trending_date="2026-06-25T00:00:00Z",
            video_trending_country="KR",
            video_title="테스트 영상",
            video_description="",
            video_default_thumbnail="",
            video_category="Sports",
            video_tags=[],
            video_duration="PT1M",
            video_dimension="2d",
            video_definition="hd",
            video_licensed_content=False,
            video_view_count=-1,
            video_like_count=0,
            video_comment_count=0,
            channel_id="chan1",
            channel_title="채널",
            channel_description="",
            channel_custom_url="",
            channel_published_at="2020-01-01T00:00:00Z",
            channel_country="KR",
            channel_view_count=0,
            channel_subscriber_count=0,
            channel_have_hidden_subscribers=False,
            channel_video_count=0,
            channel_localized_title="",
            channel_localized_description="",
            collected_at="2026-06-25T15:30:00Z",
        )


def test_trending_video_allows_missing_subscriber_count_when_hidden():
    video = TrendingVideo(
        video_id="abc123",
        video_published_at="2026-06-20T10:00:00Z",
        video_trending_date="2026-06-25T00:00:00Z",
        video_trending_country="KR",
        video_title="테스트 영상",
        video_description="",
        video_default_thumbnail="",
        video_category="Sports",
        video_tags=[],
        video_duration="PT1M",
        video_dimension="2d",
        video_definition="hd",
        video_licensed_content=False,
        video_view_count=0,
        video_like_count=0,
        video_comment_count=0,
        channel_id="chan1",
        channel_title="채널",
        channel_description="",
        channel_custom_url="",
        channel_published_at="2020-01-01T00:00:00Z",
        channel_country="KR",
        channel_view_count=0,
        channel_subscriber_count=None,
        channel_have_hidden_subscribers=True,
        channel_video_count=0,
        channel_localized_title="",
        channel_localized_description="",
        collected_at="2026-06-25T15:30:00Z",
    )

    assert video.channel_subscriber_count is None
    assert video.channel_have_hidden_subscribers is True
