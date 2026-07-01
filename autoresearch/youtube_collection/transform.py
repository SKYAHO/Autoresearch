import logging
import math
import re
from datetime import date, datetime, time
from typing import get_args, get_origin

from autoresearch.youtube_collection.schema import TARGET_COUNTRY, TrendingVideo


logger = logging.getLogger(__name__)

COUNTRY_ALIASES = _COUNTRY_ALIASES = {"KR": "KR", "South Korea": "KR"}

# Map normalized schema field -> raw Kaggle parquet column name.
_RAW_KEY_MAP = {
    "video_category": "video_category_id",  # parquet col mislabelled; holds a name
    "video_trending_date": "video_trending__date",  # parquet col has a typo
}
_DOT_DATE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})")


def normalize_kaggle_row(row: dict, collected_at: datetime) -> TrendingVideo:
    normalized: dict = {}
    for name, field in TrendingVideo.model_fields.items():
        if name == "collected_at":
            normalized[name] = collected_at
            continue
        raw_key = _RAW_KEY_MAP.get(name, name)
        normalized[name] = _coerce(row.get(raw_key), field.annotation)
    normalized["video_trending_country"] = _normalize_country(
        normalized["video_trending_country"]
    )
    logger.debug(
        "Normalized Kaggle row for video_id=%s",
        normalized.get("video_id"),
    )
    return TrendingVideo(**normalized)


def normalize_api_item(
    video_item: dict,
    channel_item: dict | None,
    category_map: dict[str, str],
    *,
    collected_at: datetime,
    region_code: str = TARGET_COUNTRY,
) -> TrendingVideo:
    snippet = video_item.get("snippet", {}) or {}
    content = video_item.get("contentDetails", {}) or {}
    stats = video_item.get("statistics", {}) or {}
    chan = channel_item or {}
    c_snippet = chan.get("snippet", {}) or {}
    c_stats = chan.get("statistics", {}) or {}
    thumbnails = snippet.get("thumbnails", {}) or {}
    default_thumb = thumbnails.get("default", {}) or {}
    localized = c_snippet.get("localized", {}) or {}
    category_id = snippet.get("categoryId")

    raw: dict = {
        "video_id": video_item.get("id"),
        "video_published_at": snippet.get("publishedAt"),
        "video_trending_date": collected_at,
        "video_trending_country": region_code,
        "video_title": snippet.get("title"),
        "video_description": snippet.get("description"),
        "video_default_thumbnail": default_thumb.get("url"),
        "video_category": category_map.get(category_id, "") if category_id else "",
        "video_tags": snippet.get("tags"),
        "video_duration": content.get("duration"),
        "video_dimension": content.get("dimension"),
        "video_definition": content.get("definition"),
        "video_licensed_content": content.get("licensedContent"),
        "video_view_count": stats.get("viewCount"),
        "video_like_count": stats.get("likeCount"),
        "video_comment_count": stats.get("commentCount"),
        "channel_id": snippet.get("channelId"),
        "channel_title": c_snippet.get("title"),
        "channel_description": c_snippet.get("description"),
        "channel_custom_url": c_snippet.get("customUrl"),
        "channel_published_at": c_snippet.get("publishedAt"),
        "channel_country": c_snippet.get("country"),
        "channel_view_count": c_stats.get("viewCount"),
        "channel_subscriber_count": c_stats.get("subscriberCount"),
        "channel_have_hidden_subscribers": c_stats.get("hiddenSubscriberCount"),
        "channel_video_count": c_stats.get("videoCount"),
        "channel_localized_title": localized.get("title"),
        "channel_localized_description": localized.get("description"),
    }
    normalized = {
        name: _coerce(raw.get(name), field.annotation)
        for name, field in TrendingVideo.model_fields.items()
        if name != "collected_at"
    }
    normalized["collected_at"] = collected_at
    logger.debug("Normalized API item for video_id=%s", normalized.get("video_id"))
    return TrendingVideo(**normalized)


def _is_missing(value: object) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _coerce(value: object, annotation: object) -> object:
    if type(None) in get_args(annotation):  # type: ignore[arg-type]
        if _is_missing(value) or value == "":
            return None
        inner = next(a for a in get_args(annotation) if a is not type(None))
        return _coerce(value, inner)
    if annotation is str:
        return "" if _is_missing(value) else str(value)
    if annotation is bool:
        return _to_bool(value)
    if annotation is int:
        return _to_int(value)
    if annotation is datetime:
        return _to_datetime(value)
    if get_origin(annotation) is list:
        return _to_tags(value)
    return value


def _to_int(value: object) -> int:
    if _is_missing(value) or value == "":
        return 0
    return int(float(value))


def _to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if _is_missing(value):
        return False
    return str(value).strip().lower() in ("true", "1", "yes")


def _to_tags(value: object) -> list[str]:
    if _is_missing(value) or value == "":
        return []
    if isinstance(value, list):
        return [str(t) for t in value]
    return [t.strip() for t in str(value).split(",") if t.strip()]


def _to_datetime(value: object) -> datetime | None:
    if _is_missing(value) or (
        isinstance(value, str) and value.strip() in ("", "None", "NaN", "nan", "null")
    ):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time())
    text = _DOT_DATE.sub(r"\1-\2-\3", str(value))
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _normalize_country(raw: object) -> str:
    if _is_missing(raw):
        raise ValueError("video_trending_country is missing")
    code = _COUNTRY_ALIASES.get(raw)  # type: ignore[arg-type]
    if code is None:
        raise ValueError(
            f"Unsupported country for {TARGET_COUNTRY}-only scope: {raw!r}"
        )
    return code
