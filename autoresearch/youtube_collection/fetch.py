import logging

from autoresearch.youtube_collection.schema import TARGET_COUNTRY, TrendingVideo
from autoresearch.youtube_collection.transform import normalize_api_item


logger = logging.getLogger(__name__)

_VIDEO_PART = "snippet,statistics,contentDetails"
_CHANNEL_PART = "snippet,statistics"
_CATEGORY_PART = "snippet"
_CHANNEL_BATCH = 50
_PAGE_SIZE = 50


def fetch_trending_video_items(
    list_videos,
    *,
    region_code: str = TARGET_COUNTRY,
    max_results: int = 200,
    page_size: int = _PAGE_SIZE,
) -> list[dict]:
    items: list[dict] = []
    page_token = None
    while len(items) < max_results:
        params = {
            "part": _VIDEO_PART,
            "chart": "mostPopular",
            "regionCode": region_code,
            "maxResults": min(page_size, max_results - len(items)),
        }
        if page_token:
            params["pageToken"] = page_token
        response = list_videos(**params)
        items.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    logger.info(
        "Fetched %d trending video items for region=%s", len(items), region_code
    )
    return items[:max_results]


def fetch_channel_map(list_channels, channel_ids: list[str]) -> dict[str, dict]:
    mapping: dict[str, dict] = {}
    for start in range(0, len(channel_ids), _CHANNEL_BATCH):
        batch = channel_ids[start : start + _CHANNEL_BATCH]
        response = list_channels(part=_CHANNEL_PART, id=",".join(batch))
        for item in response.get("items", []):
            mapping[item["id"]] = item
    logger.info("Fetched metadata for %d channels", len(mapping))
    return mapping


def fetch_category_map(
    list_categories,
    *,
    region_code: str = TARGET_COUNTRY,
) -> dict[str, str]:
    response = list_categories(part=_CATEGORY_PART, regionCode=region_code)
    mapping = {
        item["id"]: item["snippet"]["title"]
        for item in response.get("items", [])
    }
    logger.info("Fetched %d video categories for region=%s", len(mapping), region_code)
    return mapping


def collect_trending(
    list_videos,
    list_channels,
    list_categories,
    *,
    collected_at,
    region_code: str = TARGET_COUNTRY,
    max_results: int = 200,
) -> list[TrendingVideo]:
    video_items = fetch_trending_video_items(
        list_videos, region_code=region_code, max_results=max_results
    )
    category_map = fetch_category_map(list_categories, region_code=region_code)

    channel_ids = list(
        dict.fromkeys(
            item.get("snippet", {}).get("channelId")
            for item in video_items
            if item.get("snippet", {}).get("channelId")
        )
    )
    channel_map = fetch_channel_map(list_channels, channel_ids)

    videos = [
        normalize_api_item(
            item,
            channel_map.get(item.get("snippet", {}).get("channelId")),
            category_map,
            collected_at=collected_at,
            region_code=region_code,
        )
        for item in video_items
    ]
    logger.info(
        "Collected %d normalized trending videos for region=%s",
        len(videos),
        region_code,
    )
    return videos
