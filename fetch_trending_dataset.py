"""YouTube Data API v3로 '인기 급상승' 데이터를 수집해
업로드된 youtube_trending_videos_global_daily.parquet 와 동일한 28컬럼 포맷으로 저장한다.

각 (영상 × 트렌딩 국가 × 수집일) 1행이며, 영상 상세 + 채널 통계가 한 테이블에 합쳐진다.

사용법:
  export YOUTUBE_API_KEY="발급받은_키"

  # 특정 국가들
  python fetch_trending_dataset.py --regions KR --max 200

  # 업로드 파일과 동일한 111개국 전체 (쿼터 약 1,000 units 소모)
  python fetch_trending_dataset.py --all --max 200

  # CSV도 함께 저장
  python fetch_trending_dataset.py --regions KR --csv

결과: data/youtube_trending_videos_global_daily_YYYYMMDD.parquet
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

API_BASE = "https://www.googleapis.com/youtube/v3"
OUTPUT_DIR = Path(os.getenv("YOUTUBE_OUTPUT_DIR", "data"))
HTTP = requests.Session()

# 업로드 parquet의 컬럼 순서(28개)와 정확히 일치
COLUMNS = [
    "video_id",
    "video_published_at",
    "video_trending__date",
    "video_trending_country",
    "channel_id",
    "video_title",
    "video_description",
    "video_default_thumbnail",
    "video_category_id",  # 카테고리 '이름' (예: Entertainment)
    "video_tags",
    "video_duration",
    "video_dimension",
    "video_definition",
    "video_licensed_content",
    "video_view_count",
    "video_like_count",
    "video_comment_count",
    "channel_title",
    "channel_description",
    "channel_custom_url",
    "channel_published_at",
    "channel_country",
    "channel_view_count",
    "channel_subscriber_count",
    "channel_have_hidden_subscribers",
    "channel_video_count",
    "channel_localized_title",
    "channel_localized_description",
]

# ISO 3166-1 alpha-2 코드 -> 데이터셋과 동일한 국가명
REGION_NAMES: dict[str, str] = {
    "DZ": "Algeria", "AR": "Argentina", "AM": "Armenia", "AU": "Australia",
    "AT": "Austria", "AZ": "Azerbaijan", "BH": "Bahrain", "BD": "Bangladesh",
    "BY": "Belarus", "BE": "Belgium", "BO": "Bolivia", "BA": "Bosnia and Herzegovina",
    "BR": "Brazil", "BG": "Bulgaria", "KH": "Cambodia", "CA": "Canada", "CL": "Chile",
    "CO": "Colombia", "CR": "Costa Rica", "HR": "Croatia", "CY": "Cyprus",
    "CZ": "Czechia", "DK": "Denmark", "DO": "Dominican Republic", "EC": "Ecuador",
    "EG": "Egypt", "SV": "El Salvador", "EE": "Estonia", "FI": "Finland",
    "FR": "France", "GE": "Georgia", "DE": "Germany", "GH": "Ghana", "GR": "Greece",
    "GT": "Guatemala", "HN": "Honduras", "HK": "Hong Kong", "HU": "Hungary",
    "IS": "Iceland", "IN": "India", "ID": "Indonesia", "IQ": "Iraq", "IE": "Ireland",
    "IL": "Israel", "IT": "Italy", "JM": "Jamaica", "JP": "Japan", "JO": "Jordan",
    "KZ": "Kazakhstan", "KE": "Kenya", "KW": "Kuwait", "LA": "Laos", "LV": "Latvia",
    "LB": "Lebanon", "LY": "Libya", "LI": "Liechtenstein", "LT": "Lithuania",
    "LU": "Luxembourg", "MY": "Malaysia", "MT": "Malta", "MX": "Mexico",
    "MD": "Moldova", "ME": "Montenegro", "MA": "Morocco", "NP": "Nepal",
    "NL": "Netherlands", "NZ": "New Zealand", "NI": "Nicaragua", "NG": "Nigeria",
    "MK": "North Macedonia", "NO": "Norway", "OM": "Oman", "PK": "Pakistan",
    "PA": "Panama", "PG": "Papua New Guinea", "PY": "Paraguay", "PE": "Peru",
    "PH": "Philippines", "PL": "Poland", "PT": "Portugal", "PR": "Puerto Rico",
    "QA": "Qatar", "RO": "Romania", "RU": "Russia", "SA": "Saudi Arabia",
    "SN": "Senegal", "RS": "Serbia", "SG": "Singapore", "SK": "Slovakia",
    "SI": "Slovenia", "ZA": "South Africa", "KR": "South Korea", "ES": "Spain",
    "LK": "Sri Lanka", "SE": "Sweden", "CH": "Switzerland", "TW": "Taiwan",
    "TZ": "Tanzania", "TH": "Thailand", "TN": "Tunisia", "TR": "Turkey",
    "UG": "Uganda", "UA": "Ukraine", "AE": "United Arab Emirates",
    "GB": "United Kingdom", "US": "United States", "UY": "Uruguay",
    "VE": "Venezuela", "VN": "Vietnam", "YE": "Yemen", "ZW": "Zimbabwe",
}


def normalize_region(region: str) -> str:
    """YouTube regionCode 로 쓸 국가 코드를 정규화."""
    return region.strip().upper()


def validate_max_results(max_results: int) -> int:
    if max_results < 1:
        raise SystemExit("--max 값은 1 이상이어야 합니다.")
    return max_results


def load_api_key() -> str:
    key = os.getenv("YOUTUBE_API_KEY")
    if not key:
        env_file = Path(".env")
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("YOUTUBE_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not key:
        sys.exit(
            "오류: YOUTUBE_API_KEY가 없습니다.\n"
            "  export YOUTUBE_API_KEY=\"발급받은_키\"  또는  .env 파일에 추가하세요."
        )
    return key


def api_get(key: str, endpoint: str, params: dict) -> dict:
    params = {k: v for k, v in {**params, "key": key}.items() if v is not None}
    try:
        resp = HTTP.get(f"{API_BASE}/{endpoint}", params=params, timeout=30)
    except requests.RequestException as exc:
        sys.exit(f"네트워크 오류 [{endpoint}]: {exc}")
    if resp.status_code != 200:
        try:
            msg = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            msg = resp.text
        sys.exit(f"API 오류 ({resp.status_code}) [{endpoint}]: {msg}")
    return resp.json()


def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def s(value) -> str | None:
    """값을 문자열로(없으면 None=<NA>)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def category_map(key: str, region: str) -> dict[str, str]:
    """카테고리 id -> 이름."""
    data = api_get(key, "videoCategories", {"part": "snippet", "regionCode": region})
    return {it["id"]: it["snippet"]["title"] for it in data.get("items", [])}


def fetch_trending(key: str, region: str, max_results: int) -> list[dict]:
    videos: list[dict] = []
    page_token = None
    while len(videos) < max_results:
        want = min(50, max_results - len(videos))
        data = api_get(
            key,
            "videos",
            {
                "part": "snippet,statistics,contentDetails",
                "chart": "mostPopular",
                "regionCode": region,
                "maxResults": want,
                "pageToken": page_token,
            },
        )
        videos.extend(data.get("items", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return videos[:max_results]


def fetch_channels(key: str, channel_ids: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    unique = list(dict.fromkeys(c for c in channel_ids if c))
    for batch in chunked(unique, 50):
        data = api_get(
            key, "channels", {"part": "snippet,statistics", "id": ",".join(batch), "maxResults": 50}
        )
        for it in data.get("items", []):
            out[it["id"]] = it
    return out


def build_rows(key: str, region: str, trending_date: str, max_results: int) -> list[dict]:
    region = normalize_region(region)
    max_results = validate_max_results(max_results)
    country = REGION_NAMES.get(region, region)
    print(f"  [{region}] {country} 트렌딩 수집...", end="", flush=True)
    videos = fetch_trending(key, region, max_results)
    if not videos:
        print(" 영상 0 · 채널 0")
        return []
    cats = category_map(key, region)
    channels = fetch_channels(key, [v["snippet"]["channelId"] for v in videos])
    print(f" 영상 {len(videos)} · 채널 {len(channels)}")

    rows: list[dict] = []
    for v in videos:
        sn = v.get("snippet", {})
        st = v.get("statistics", {})
        cd = v.get("contentDetails", {})
        ch = channels.get(sn.get("channelId"), {})
        chs, cht = ch.get("snippet", {}), ch.get("statistics", {})
        loc = chs.get("localized", {})
        tags = sn.get("tags") or []
        rows.append(
            {
                "video_id": s(v.get("id")),
                "video_published_at": s(sn.get("publishedAt")),
                "video_trending__date": trending_date,
                "video_trending_country": country,
                "channel_id": s(sn.get("channelId")),
                "video_title": s(sn.get("title")),
                "video_description": s(sn.get("description")),
                "video_default_thumbnail": s(sn.get("thumbnails", {}).get("default", {}).get("url")),
                "video_category_id": s(cats.get(sn.get("categoryId"))),
                "video_tags": ",".join(tags),
                "video_duration": s(cd.get("duration")),
                "video_dimension": s(cd.get("dimension")),
                "video_definition": s(cd.get("definition")),
                "video_licensed_content": s(cd.get("licensedContent")),
                "video_view_count": s(st.get("viewCount")),
                "video_like_count": s(st.get("likeCount")),
                "video_comment_count": s(st.get("commentCount")),
                "channel_title": s(chs.get("title")),
                "channel_description": s(chs.get("description")),
                "channel_custom_url": s(chs.get("customUrl")),
                "channel_published_at": s(chs.get("publishedAt")),
                "channel_country": s(chs.get("country")),
                "channel_view_count": s(cht.get("viewCount")),
                "channel_subscriber_count": s(cht.get("subscriberCount")),
                "channel_have_hidden_subscribers": s(cht.get("hiddenSubscriberCount")),
                "channel_video_count": s(cht.get("videoCount")),
                "channel_localized_title": s(loc.get("title")),
                "channel_localized_description": s(loc.get("description")),
            }
        )
    return rows


# 컬럼별 목표 타입
INT_COLS = [
    "video_view_count", "video_like_count", "video_comment_count",
    "channel_view_count", "channel_subscriber_count", "channel_video_count",
]
DATETIME_COLS = ["video_published_at", "channel_published_at"]   # 시각(UTC)
DATE_COLS = ["video_trending__date"]                              # 날짜
BOOL_COLS = ["video_licensed_content", "channel_have_hidden_subscribers"]


def coerce_types(df: pd.DataFrame) -> pd.DataFrame:
    """숫자→정수(Int64, 결측 허용), 날짜/시각→datetime, 불리언→boolean, 나머지→string."""
    df = df.copy()
    for c in INT_COLS:
        if c in df.columns:
            # "2598634.0" / "2598634" / <NA> 모두 처리
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
    for c in DATETIME_COLS:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce", utc=True)
    for c in DATE_COLS:
        if c in df.columns:
            norm = df[c].astype("string").str.replace(".", "-", regex=False)
            df[c] = pd.to_datetime(norm, errors="coerce")  # tz 없는 날짜
    for c in BOOL_COLS:
        if c in df.columns:
            df[c] = (
                df[c].astype("string").str.strip().str.lower()
                .map({"true": True, "false": False}).astype("boolean")
            )
    typed = set(INT_COLS + DATETIME_COLS + DATE_COLS + BOOL_COLS)
    for c in df.columns:
        if c not in typed:
            df[c] = df[c].astype("string")
    return df


def to_dataframe(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=COLUMNS)
    return coerce_types(df)


def main() -> None:
    parser = argparse.ArgumentParser(description="YouTube 트렌딩 → 동일 포맷 parquet")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--regions", help="콤마로 구분한 ISO 국가코드 (예: KR,US,JP)")
    g.add_argument("--all", action="store_true", help="지원 111개국 전체")
    parser.add_argument("--max", type=int, default=200, help="국가당 최대 영상 수 (기본 200)")
    parser.add_argument("--csv", action="store_true", help="parquet과 함께 CSV도 저장")
    args = parser.parse_args()

    max_results = validate_max_results(args.max)
    regions = (
        list(REGION_NAMES)
        if args.all
        else list(dict.fromkeys(normalize_region(r) for r in args.regions.split(",") if r.strip()))
    )
    key = load_api_key()

    trending_date = datetime.now(timezone.utc).strftime("%Y.%m.%d")
    print(f"수집일 {trending_date} · 국가 {len(regions)}개 · 국가당 최대 {max_results}")

    all_rows: list[dict] = []
    for region in regions:
        all_rows.extend(build_rows(key, region, trending_date, max_results))

    df = to_dataframe(all_rows)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    pq_path = OUTPUT_DIR / f"youtube_trending_videos_global_daily_{stamp}.parquet"
    df.to_parquet(pq_path, index=False)
    print(f"\n저장: {pq_path}  ({len(df)} rows × {len(df.columns)} cols)")
    if args.csv:
        csv_path = pq_path.with_suffix(".csv")
        df.to_csv(csv_path, index=False, encoding="utf-8-sig")
        print(f"저장: {csv_path}")


if __name__ == "__main__":
    main()
