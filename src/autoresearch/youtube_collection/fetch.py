"""YouTube Data API v3 수집 모듈(daily DAG 의 핵심).

한국(KR) 인기 급상승(chart=mostPopular) 영상을 가져와 채널 메타를 붙이고,
카테고리 id 를 이름으로 바꿔 정규 TrendingVideo 리스트로 조립한다.

설계 포인트 — 'callable 주입' 패턴:
    이 모듈은 google-api-python-client 를 **직접 import 하지 않는다.** 대신
    ``list_videos(**kw) -> dict`` 형태의 평범한 callable 을 인자로 받는다.
    DAG 래퍼가 실제 API 객체(service.videos().list(...).execute())를 callable 로
    adapt 해서 넘겨준다.

    이유: (1) googleapiclient Mock 없이 순수 Python dict 로 단위테스트 가능,
    (2) API 클라이언트 의존성이 핵심 로직과 분리됨. 결과적으로 tests/ 에서
    가짜 응답 dict 만 넘기면 페이지네이션/배치 로직을 검증할 수 있다.

쿼터(하루 예산 10,000 units):
    videos.list 1 unit × 4페이지(~200개) + channels.list 1 unit × 3배치(~150채널)
    = 약 7 units/일(예산의 0.07%). API 키 1개로 충분.
"""
import logging

from autoresearch.youtube_collection.schema import TARGET_COUNTRY, TrendingVideo
from autoresearch.youtube_collection.transform import normalize_api_item


logger = logging.getLogger(__name__)

# API part 파라미터. 필요한 필드만 요청해 응답을 가볍게 유지.
_VIDEO_PART = "snippet,statistics,contentDetails"
_CHANNEL_PART = "snippet,statistics"
_CATEGORY_PART = "snippet"
_CHANNEL_BATCH = 50  # channels.list 한 번에 최대 50개 id.
_PAGE_SIZE = 50  # videos.list 한 페이지당 최대 50개.


def fetch_trending_video_items(
    list_videos,
    *,
    region_code: str = TARGET_COUNTRY,
    max_results: int = 200,
    page_size: int = _PAGE_SIZE,
) -> list[dict]:
    """chart=mostPopular 로 트렌딩 영상을 페이지네이션하며 max_results 개까지 수집.

    Args:
        list_videos: ``service.videos().list(**kw).execute()`` 를 감싼 callable.
        region_code: 국가 코드(기본 "KR").
        max_results: 최대 영상 수(기본 200 = KR 하루 트렌딩 규모).
        page_size: 페이지당 요청 수.

    nextPageToken 이 없으면(마지막 페이지) 수집 종료.
    """
    items: list[dict] = []
    page_token = None
    while len(items) < max_results:
        params = {
            "part": _VIDEO_PART,
            "chart": "mostPopular",
            "regionCode": region_code,
            # 남은 만큼만 요청(마지막 페이지에서 max_results 를 넘지 않게).
            "maxResults": min(page_size, max_results - len(items)),
        }
        if page_token:
            params["pageToken"] = page_token
        response = list_videos(**params)
        items.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break  # 더 이상 페이지 없음.
    logger.info(
        "Fetched %d trending video items for region=%s", len(items), region_code
    )
    return items[:max_results]


def fetch_channel_map(list_channels, channel_ids: list[str]) -> dict[str, dict]:
    """채널 id 리스트 → {channel_id: channel_item} 맵. 50개씩 배치 호출.

    같은 영상 여러 개가 같은 채널을 공유하므로, 영상 수보다 고유 채널 수가 적다.
    배치 호출로 API 횟수를 줄인다(120 채널 → 50/50/20 = 3호출).
    """
    mapping: dict[str, dict] = {}
    for start in range(0, len(channel_ids), _CHANNEL_BATCH):
        batch = channel_ids[start : start + _CHANNEL_BATCH]
        response = list_channels(part=_CHANNEL_PART, id=",".join(batch))
        for item in response.get("items", []):
            # id 가 없는 비정형 항목은 KeyError 대신 skip.
            cid = item.get("id")
            if cid:
                mapping[cid] = item
    logger.info("Fetched metadata for %d channels", len(mapping))
    return mapping


def fetch_category_map(
    list_categories,
    *,
    region_code: str = TARGET_COUNTRY,
) -> dict[str, str]:
    """videoCategories.list → {category_id: category_이름} 맵.

    API 가 주는 categoryId 는 숫자("24")라, 저장 시 이름("Music")으로 바꾸기 위해
    미리 id→이름 테이블을 만들어 둔다(Kaggle 스키마와 일치시키기 위함).
    """
    response = list_categories(part=_CATEGORY_PART, regionCode=region_code)
    mapping: dict[str, str] = {}
    for item in response.get("items", []):
        # id 또는 snippet.title 이 없는 비정형 항목은 skip (KeyError 방지).
        cid = item.get("id")
        title = item.get("snippet", {}).get("title")
        if cid and title:
            mapping[cid] = title
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
    """수집 → 채널 메타 결합 → 카테고리 변환 → 정규 TrendingVideo 리스트 조립.

    DAG 가 호출하는 진입점. 내부 흐름:
      1. 트렌딩 영상 items 수집(fetch_trending_video_items)
      2. 카테고리 맵 생성(fetch_category_map)
      3. 영상들의 고유 channelId 들을 순서 보존하며 dedup(dict.fromkeys)
      4. 채널 메타 일괄 조회(fetch_channel_map)
      5. 각 영상을 normalize_api_item 으로 정규화(채널/카테고리 결합)
    """
    video_items = fetch_trending_video_items(
        list_videos, region_code=region_code, max_results=max_results
    )
    category_map = fetch_category_map(list_categories, region_code=region_code)

    # 고유 channelId 만 추출(순서 유지 — 결정성/재현성). None 은 제외.
    channel_ids = list(
        dict.fromkeys(
            item.get("snippet", {}).get("channelId")
            for item in video_items
            if item.get("snippet", {}).get("channelId")
        )
    )
    channel_map = fetch_channel_map(list_channels, channel_ids)

    # 각 영상 정규화. 채널이 누락된(조회 실패) 경우 None 을 넘겨 빈값 폴백.
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
