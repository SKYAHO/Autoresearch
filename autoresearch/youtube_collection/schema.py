from datetime import datetime
import logging

from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)

SCHEMA_VERSION = "youtube_trending_kr_v1"
TARGET_COUNTRY = "KR"


class TrendingVideo(BaseModel):
    video_id: str
    video_published_at: datetime
    video_trending_date: datetime
    video_trending_country: str
    video_title: str
    video_description: str
    video_default_thumbnail: str
    video_category: str
    video_tags: list[str]
    video_duration: str
    video_dimension: str
    video_definition: str
    video_licensed_content: bool
    video_view_count: int = Field(ge=0)
    video_like_count: int = Field(ge=0)
    video_comment_count: int = Field(ge=0)
    channel_id: str
    channel_title: str
    channel_description: str
    channel_custom_url: str
    channel_published_at: datetime | None = None
    channel_country: str
    channel_view_count: int = Field(ge=0)
    channel_subscriber_count: int | None = Field(default=None, ge=0)
    channel_have_hidden_subscribers: bool
    channel_video_count: int = Field(ge=0)
    channel_localized_title: str
    channel_localized_description: str
    collected_at: datetime
