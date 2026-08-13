"""원본 데이터 → 정규 스키마(TrendingVideo) 변환 모듈.

두 종류 원본을 같은 스키마로 맞춘다:
  1. Kaggle 과거 parquet  → ``normalize_kaggle_row``
  2. YouTube Data API v3 응답 → ``normalize_api_item``

두 원본 모두 결국 같은 ``_coerce`` 강제 변환 로직을 거쳐 타입이 보장된 dict 가 되고,
``TrendingVideo`` 모델로 검증된다. 이 "어떤 원본이든 같은 coerce 통로"가
스키마 일관성의 핵심.

KR-only 강제:
  ``TARGET_COUNTRY`` 가 "KR" 이므로, 국가가 KR 계열이 아니면 ValueError 로
  거부한다. 잘못된 국가 데이터가 레이크에 섞이는 것을 원천 차단.

타입 강제(coercion)가 필요한 이유:
  Kaggle 원본은 **모든 컬럼이 string** 이다(카운트 "482000.0", bool "False",
  날짜 "2024.10.12"). 반면 API 응답은 네이티브 타입(int/list)이지만 일부 필드는
  올 수도 안 올 수도 있다. 두 경우를 모두 커버하기 위해 어노테이션(타입 힌트)을
  기준으로 값을 변환한다.
"""
import logging
import math
import re
from datetime import UTC, date, datetime, time
from typing import get_args, get_origin

from autoresearch.youtube_collection.schema import TARGET_COUNTRY, TrendingVideo


logger = logging.getLogger(__name__)

# 국가 표현 통일. 원본에서 "KR" 또는 "South Korea" 로 들어온 값은 모두 "KR" 로.
# 이 외 값은 KR-only scope 위반이므로 _normalize_country 가 ValueError 발생.
COUNTRY_ALIASES = _COUNTRY_ALIASES = {"KR": "KR", "South Korea": "KR"}

# 정규 스키마 필드명 → Kaggle parquet 원본 컬럼명 매핑.
# Kaggle 원본에 오타/오배명이 있어서 정규 이름으로 바꿔 읽는다.
_RAW_KEY_MAP = {
    # parquet 컬럼명은 video_category_id 이지만 실제 값은 카테고리 '이름'("Music").
    "video_category": "video_category_id",
    # parquet 컬럼명이 video_trending__date (밑줄 2개 오타).
    "video_trending_date": "video_trending__date",
}
# Kaggle 날짜가 "2024.10.12" 처럼 점(.) 구분이라 ISO("-")로 바꾸기 위한 정규식.
_DOT_DATE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})")


def normalize_kaggle_row(row: dict, collected_at: datetime) -> TrendingVideo:
    """Kaggle parquet 1행(row)을 정규 TrendingVideo 로 변환.

    동작 흐름:
      1. TrendingVideo 의 각 필드를 순회.
      2. collected_at 은 인자로 받은 값을 그대로 주입(원본에 없는 필드).
      3. 그 외 필드는 _RAW_KEY_MAP 으로 원본 컬럼명을 찾아 값을 꺼낸 뒤,
         필드의 타입 어노테이션에 맞게 _coerce 로 강제 변환.
      4. 국가는 별도로 _normalize_country 로 KR 정규화 + 검증.
      5. TrendingVideo(**dict) 로 최종 타입 검증.
    """
    normalized: dict = {}
    for name, field in TrendingVideo.model_fields.items():
        if name == "collected_at":
            # 이 값은 원본에 없고 파이프라인이 주입한다.
            normalized[name] = collected_at
            continue
        # 정규 필드명에 해당하는 원본 컬럼명(오타/오배명 보정).
        raw_key = _RAW_KEY_MAP.get(name, name)
        # 어노테이션(int/str/datetime/list/Optional ...) 기반 강제 변환.
        normalized[name] = _coerce(row.get(raw_key), field.annotation)
    # 국가는 coerce 가 아니라 별도 검증 로직(KR-only 강제).
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
    """YouTube Data API v3 응답 1건을 정규 TrendingVideo 로 변환.

    API 응답은 중첩 구조(snippet/statistics/contentDetails)라 이를 flat dict 로
    펼친 뒤 Kaggle 과 동일한 _coerce 통로를 거친다. channel_item 이 없으면(드물지만
    채널 조회 실패) 빈 값으로 폴백한다.

    참고:
      * video_trending_date = collected_at: API 은 '트렌딩 여부'만 주고 날짜를 주지
        않으므로, 스냅샷 시각(=수집 시각)을 트렌딩 날짜로 쓴다.
      * video_category = category_map[categoryId]: API 가 주는 건 숫자 id 이므로,
        미리 만든 category_map(id→이름) 으로 이름("Music")으로 변환.
    """
    # --- 영상 쪽 중첩 객체 꺼내기(없으면 빈 dict 로 폴백) ---
    snippet = video_item.get("snippet", {}) or {}
    content = video_item.get("contentDetails", {}) or {}
    stats = video_item.get("statistics", {}) or {}
    # statistics 가 통째로 없으면 카운트가 0으로 강제된다(append-only 델타 왜곡).
    # 스키마를 바꾸진 않되, 추적 가능하도록 WARN 로그를 남긴다.
    if not stats:
        logger.warning(
            "video statistics missing for video_id=%s; counts default to 0",
            video_item.get("id"),
        )
    # --- 채널 쪽(채널 조회 실패 시 None → 빈 dict 폴백) ---
    chan = channel_item or {}
    c_snippet = chan.get("snippet", {}) or {}
    c_stats = chan.get("statistics", {}) or {}
    thumbnails = snippet.get("thumbnails", {}) or {}
    default_thumb = thumbnails.get("default", {}) or {}
    localized = c_snippet.get("localized", {}) or {}
    category_id = snippet.get("categoryId")

    # flat dict 구성: 정규 스키마 필드명 = API 필드에서 꺼낸 값.
    raw: dict = {
        "video_id": video_item.get("id"),
        "video_published_at": snippet.get("publishedAt"),
        "video_trending_date": collected_at,  # 스냅샷=수집시각
        "video_trending_country": region_code,  # "KR"
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
    # Kaggle 과 동일한 coerce 통로. collected_at 은 제외하고 변환 후 따로 주입.
    normalized = {
        name: _coerce(raw.get(name), field.annotation)
        for name, field in TrendingVideo.model_fields.items()
        if name != "collected_at"
    }
    normalized["collected_at"] = collected_at
    # KR-only 강제: Kaggle 경로와 대칭되게 API 경로에서도 _normalize_country 로 검증.
    # region_code 가 KR 계열이 아니면 ValueError (잘못된 국가 데이터 원천 차단).
    normalized["video_trending_country"] = _normalize_country(
        normalized["video_trending_country"]
    )
    logger.debug("Normalized API item for video_id=%s", normalized.get("video_id"))
    return TrendingVideo(**normalized)


def _is_missing(value: object) -> bool:
    """결측 판정: None 이거나 NaN(float)이면 결측."""
    return value is None or (isinstance(value, float) and math.isnan(value))


def _coerce(value: object, annotation: object) -> object:
    """타입 어노테이션에 맞춰 값을 강제 변환(coerce).

    핵심 아이디어: 필드의 '타입 힌트'를 보고 분기한다. 원본 출처(Kaggle/API)와
    무관하게 같은 로직이 동작한다.

    Optional 처리(int | None 같은): None 이거나 빈값이면 None 반환, 아니면 내부
    타입으로 재귀 호출.
    """
    if type(None) in get_args(annotation):  # type: ignore[arg-type]
        # Optional 내부 타입인데 결측이면 None.
        if _is_missing(value) or value == "":
            return None
        # Optional 이 아닌 진짜 타입(int/datetime/...)을 꺼내 재귀.
        inner = next(a for a in get_args(annotation) if a is not type(None))
        return _coerce(value, inner)
    if annotation is str:
        return "" if _is_missing(value) else str(value)
    if annotation is bool:
        return _to_bool(value)
    if annotation is int:
        return _to_int(value)
    if annotation is datetime:
        result = _to_datetime(value)
        # non-Optional datetime 인데 결측/파싱불가 → pydantic 의 모호한 에러 대신
        # 명확한 ValueError 로 조기 실패(Optional 분기는 위에서 이미 None 반환됨).
        if result is None:
            raise ValueError(
                f"required datetime is missing or unparseable: {value!r}"
            )
        return result
    if get_origin(annotation) is list:
        return _to_tags(value)
    # 스키마에 새 타입(float 등)이 추가되면 조용히 통과시키지 않도록 방어.
    raise TypeError(f"unsupported annotation: {annotation!r}")


def _to_int(value: object) -> int:
    """정수로 변환. Kaggle 은 "482000.0" 처럼 float 문자열로 오므로 float→int."""
    if _is_missing(value) or value == "":
        return 0
    return int(float(value))


def _to_bool(value: object) -> bool:
    """불로 변환. Kaggle 은 "False" 문자열, API 는 진짜 bool 둘 다 커버."""
    if isinstance(value, bool):
        return value
    if _is_missing(value):
        return False
    return str(value).strip().lower() in ("true", "1", "yes")


def _to_tags(value: object) -> list[str]:
    """태그 리스트로 변환.
    API 는 list 로 오고, Kaggle 은 "태그1,태그2" 콤마 조인 문자열로 온다.
    """
    if _is_missing(value) or value == "":
        return []
    if isinstance(value, list):
        return [str(t) for t in value]
    return [t.strip() for t in str(value).split(",") if t.strip()]


def _to_datetime(value: object) -> datetime | None:
    """datetime 으로 변환. 결측/센티넬 문자열은 None.
    Kaggle 날짜("2024.10.12")는 점을 대시로 바꾸고, ISO 'Z' 접미사는 '+00:00'로.
    """
    if _is_missing(value) or (
        isinstance(value, str) and value.strip() in ("", "None", "NaN", "nan", "null")
    ):
        return None
    if isinstance(value, datetime):
        return _ensure_utc(value)
    if isinstance(value, date):
        return _ensure_utc(datetime.combine(value, time()))
    # "2024.10.12" → "2024-10-12" 치환 후 ISO 파싱.
    text = _DOT_DATE.sub(r"\1-\2-\3", str(value))
    return _ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))


def _ensure_utc(dt: datetime) -> datetime:
    """naive datetime(백필의 "2024.10.12" 등)에 UTC 를 부착.

    백필은 naive, 일일은 UTC-aware 로 파싱돼 같은 컬럼에 tz 가 섞이는 것을 막는다.
    이미 tz 가 있으면 그대로(UTC 가정하에 변환하지 않고 보존).
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _normalize_country(raw: object) -> str:
    """국가를 KR 로 정규화. KR 계열이 아니면 ValueError(KR-only scope 강제)."""
    if _is_missing(raw):
        raise ValueError("video_trending_country is missing")
    code = _COUNTRY_ALIASES.get(raw)  # type: ignore[arg-type]
    if code is None:
        raise ValueError(
            f"Unsupported country for {TARGET_COUNTRY}-only scope: {raw!r}"
        )
    return code
