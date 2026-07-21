"""학습·시뮬레이션 공용 피처 조립 함수.

build_training_dataset.main()의 인라인 DuckDB SQL과 interaction 계산을 추출한
것이다. 학습 데이터셋 생성과 정책 시뮬레이션 라운드(simulate_policy_round)가
같은 코드로 피처를 계산해 학습-서빙 스큐를 방지한다.
"""

import json
from datetime import datetime

import duckdb
import pandas as pd

from src.features.category_reference import CATEGORY_DESCRIPTIONS
from src.features.feature_builder import (
    compute_historical_category_match,
    compute_preferred_category_match,
    compute_topic_similarity,
    embed_keywords,
)

KEYWORD_TO_CATEGORY = {
    "gaming": "Gaming",
    "game": "Gaming",
    "music": "Music",
    "sports": "Sports",
    "travel": "Travel & Events",
    "food": "Howto & Style",
    "beauty": "Howto & Style",
    "fashion": "Howto & Style",
    "education": "Education",
    "technology": "Science & Technology",
    "news": "News & Politics",
    "entertainment": "Entertainment",
    "comedy": "Comedy",
    "pet": "Pets & Animals",
    "animal": "Pets & Animals",
}

assert set(KEYWORD_TO_CATEGORY.values()) <= set(CATEGORY_DESCRIPTIONS), \
    f"KEYWORD_TO_CATEGORY has invalid categories: {set(KEYWORD_TO_CATEGORY.values()) - set(CATEGORY_DESCRIPTIONS)}"


def derive_preferred_category(keywords) -> list:
    """FALLBACK MOCK: 키워드 리스트에서 선호 카테고리 파생.

    personas 원본에 virtual_users 파이프라인이 LLM으로 직접 산출한
    primary_categories 컬럼이 없을 때만(구식 mock personas.csv 등) 쓰는
    fallback이다. primary_categories가 있으면 parse_primary_categories()가
    그 실제 값을 그대로 쓴다 (#205, autoresearch/virtual_users/schema.py의
    YouTubeProfile.primary_categories 참고).

    Args:
        keywords: preferred_topics의 키워드 리스트 (영어 또는 한글).

    Returns:
        매핑되는 category_id 리스트 (최대 3개, dedup, 순서 유지).
    """
    categories = []
    seen = set()
    for kw in keywords:
        kw_lower = str(kw).lower()
        if kw_lower in KEYWORD_TO_CATEGORY:
            cat_id = KEYWORD_TO_CATEGORY[kw_lower]
            if cat_id not in seen:
                categories.append(cat_id)
                seen.add(cat_id)
                if len(categories) >= 3:
                    break
    return categories if categories else ["People & Blogs"]


def parse_primary_categories(value) -> list:
    """virtual_users 파이프라인이 산출한 primary_categories 원본을 파싱한다.

    parquet에서 로드하면 파이썬 list로, mock CSV에서 로드하면 JSON 문자열로
    들어온다. docs/guides/data-warehouse.md의 user_static_feature 규칙과
    동일하게 null/빈 값은 빈 리스트로 처리하고, CATEGORY_DESCRIPTIONS
    vocabulary 밖의 값은 걸러낸다(LLM vocab drift 방어).
    """
    if isinstance(value, list):
        categories = value
    elif value is None or pd.isna(value):
        return []
    else:
        try:
            categories = json.loads(str(value))
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(categories, list):
        return []
    return [c for c in categories if c in CATEGORY_DESCRIPTIONS]


def extract_keywords_safe(text_or_json) -> list:
    """hobbies_and_interests_list (JSON 리스트) 또는 hobbies_and_interests (텍스트)에서 키워드 추출."""
    if pd.isna(text_or_json):
        return []
    try:
        keywords = json.loads(str(text_or_json))
        if isinstance(keywords, list):
            return [str(k).lower() for k in keywords if k]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def compute_video_features(videos_raw: pd.DataFrame, snapshot_date: str) -> pd.DataFrame:
    """영상 원본 컬럼(categoryId/duration/viewCount/...)에서 모델 영상 피처를 계산한다.

    channel_subscriber_count/channel_view_count/channel_video_count는
    videos_raw에 channelSubscriberCount/channelViewCount/channelVideoCount
    컬럼이 있을 때만 채우고, 없으면 0으로 default 처리한다
    (docs/guides/data-warehouse.md video_feature cold-start 규칙과 동일).
    """
    datetime.strptime(snapshot_date, "%Y-%m-%d")  # SQL 보간 전 형식 검증
    con = duckdb.connect()
    con.register("videos_raw", videos_raw)
    channel_subscriber_expr = (
        "CAST(channelSubscriberCount AS BIGINT)"
        if "channelSubscriberCount" in videos_raw.columns
        else "NULL"
    )
    channel_view_expr = (
        "CAST(channelViewCount AS BIGINT)"
        if "channelViewCount" in videos_raw.columns
        else "NULL"
    )
    channel_video_expr = (
        "CAST(channelVideoCount AS BIGINT)"
        if "channelVideoCount" in videos_raw.columns
        else "NULL"
    )
    return con.execute(
        f"""
        SELECT
            video_id,
            CAST(categoryId AS VARCHAR) AS category_id,
            COALESCE(CAST(duration AS INTEGER), 300) AS duration_sec,
            CAST(viewCount AS BIGINT) AS view_count,
            ROUND(CAST(likeCount AS FLOAT) / NULLIF(CAST(viewCount AS FLOAT), 0), 4) AS like_ratio,
            ROUND(CAST(commentCount AS FLOAT) / NULLIF(CAST(viewCount AS FLOAT), 0), 4) AS comment_ratio,
            DATE_DIFF('day', CAST(publishedAt AS DATE), DATE '{snapshot_date}') AS days_since_upload,
            COALESCE({channel_subscriber_expr}, 0) AS channel_subscriber_count,
            COALESCE({channel_view_expr}, 0) AS channel_view_count,
            COALESCE({channel_video_expr}, 0) AS channel_video_count
        FROM videos_raw
        """
    ).df()


def compute_user_offline_features(personas_raw: pd.DataFrame) -> pd.DataFrame:
    """persona 원본(uuid/age/occupation)에서 오프라인 유저 피처를 계산한다.

    watch_time_band는 personas_raw에 watch_time_band 컬럼이 있을 때만
    docs/guides/data-warehouse.md의 user_static_feature 정규화 규칙(오전/오후/
    저녁/밤 표기를 morning/evening/night로 통일, 그 외는 unknown)을 적용하고,
    컬럼이 없으면 "unknown"으로 default 처리한다.
    """
    con = duckdb.connect()
    con.register("personas_raw", personas_raw)
    watch_time_band_expr = (
        """
        CASE
            WHEN LOWER(TRIM(watch_time_band)) IN ('morning', 'am', '오전', '아침') THEN 'morning'
            WHEN LOWER(TRIM(watch_time_band)) IN ('evening', 'pm', '저녁', '오후') THEN 'evening'
            WHEN LOWER(TRIM(watch_time_band)) IN ('night', 'late_night', '밤', '심야') THEN 'night'
            ELSE 'unknown'
        END
        """
        if "watch_time_band" in personas_raw.columns
        else "'unknown'"
    )
    return con.execute(
        f"""
        SELECT
            uuid AS user_id,
            CASE
                WHEN age < 20 THEN '10s'
                WHEN age < 30 THEN '20s'
                WHEN age < 40 THEN '30s'
                WHEN age < 50 THEN '40s'
                ELSE '50s+'
            END AS age_group,
            occupation,
            {watch_time_band_expr} AS watch_time_band
        FROM personas_raw
        """
    ).df()


def compute_point_in_time_user_features(
    event_log: pd.DataFrame,
    videos_raw: pd.DataFrame,
    query_points: pd.DataFrame,
) -> pd.DataFrame:
    """query_points(user_id, as_of[, carry...])의 각 행에 대해 as_of 직전 기준
    historical_category_affinity와 recent 7일 집계를 계산한다.

    학습 경로는 query_points=노출 이벤트(as_of=impression 시각)로, 시뮬레이션
    경로는 query_points=유저×기준시각 1행으로 호출한다 — 같은 SQL이므로
    point-in-time 계산이 두 경로에서 항상 일치한다.

    recent_view_count_7d/total_event_count_7d는 근사값이다: event_log는
    long-format action_log가 아니라 impression 1행에 clicked/liked/
    watch_time_sec를 붙인 wide-format 어댑터(derive_wide_events() 참고)라
    view를 별도 행으로 셀 수 없다. watch_time_sec > 0을 view 발생으로,
    "impression 1 + clicked + view + liked" 합을 total_event_count_7d로
    근사한다. docs/guides/data-warehouse.md의 user_dynamic_feature(Feast
    경유 목표 설계)는 raw event_type을 직접 카운트하므로 이 근사와 다를 수
    있다 — Feast 전환(#207) 이후에는 이 근사가 필요 없어진다.
    """
    con = duckdb.connect()
    con.register("event_log_src", event_log)
    con.register("videos_raw", videos_raw)
    con.register("query_points_src", query_points)
    con.execute("CREATE OR REPLACE TABLE event_log_ts AS SELECT * FROM event_log_src")
    con.execute("CREATE OR REPLACE TABLE query_points AS SELECT * FROM query_points_src")
    carry = [c for c in query_points.columns if c not in ("user_id", "as_of")]
    carry_select = "".join(f'q."{name}",\n            ' for name in carry)
    return con.execute(
        f"""
        SELECT
            q.user_id,
            q.as_of,
            {carry_select}COALESCE(
                (
                    SELECT CAST(v.categoryId AS VARCHAR)
                    FROM event_log_ts AS past
                    JOIN videos_raw AS v ON v.video_id = past.video_id
                    WHERE past.user_id = q.user_id
                      AND CAST(past.timestamp AS TIMESTAMP) < CAST(q.as_of AS TIMESTAMP)
                      AND past.clicked = 1
                    GROUP BY v.categoryId
                    ORDER BY COUNT(*) DESC
                    LIMIT 1
                ),
                'unknown'
            ) AS historical_category_affinity,

            (
                SELECT COUNT(*)
                FROM event_log_ts AS past
                WHERE past.user_id = q.user_id
                  AND past.clicked = 1
                  AND CAST(past.timestamp AS TIMESTAMP) < CAST(q.as_of AS TIMESTAMP)
                  AND CAST(past.timestamp AS TIMESTAMP) >= CAST(q.as_of AS TIMESTAMP) - INTERVAL 7 DAY
            ) AS recent_click_count_7d,

            (
                SELECT COALESCE(SUM(past.watch_time_sec), 0)
                FROM event_log_ts AS past
                WHERE past.user_id = q.user_id
                  AND CAST(past.timestamp AS TIMESTAMP) < CAST(q.as_of AS TIMESTAMP)
                  AND CAST(past.timestamp AS TIMESTAMP) >= CAST(q.as_of AS TIMESTAMP) - INTERVAL 7 DAY
            ) AS recent_watch_time_7d,

            (
                SELECT COUNT(*)
                FROM event_log_ts AS past
                WHERE past.user_id = q.user_id
                  AND past.liked = 1
                  AND CAST(past.timestamp AS TIMESTAMP) < CAST(q.as_of AS TIMESTAMP)
                  AND CAST(past.timestamp AS TIMESTAMP) >= CAST(q.as_of AS TIMESTAMP) - INTERVAL 7 DAY
            ) AS recent_like_count_7d,

            (
                SELECT COUNT(*)
                FROM event_log_ts AS past
                WHERE past.user_id = q.user_id
                  AND CAST(past.watch_time_sec AS BIGINT) > 0
                  AND CAST(past.timestamp AS TIMESTAMP) < CAST(q.as_of AS TIMESTAMP)
                  AND CAST(past.timestamp AS TIMESTAMP) >= CAST(q.as_of AS TIMESTAMP) - INTERVAL 7 DAY
            ) AS recent_view_count_7d,

            (
                SELECT
                    COALESCE(COUNT(*), 0)
                    + COALESCE(SUM(CASE WHEN past.clicked = 1 THEN 1 ELSE 0 END), 0)
                    + COALESCE(SUM(CASE WHEN CAST(past.watch_time_sec AS BIGINT) > 0 THEN 1 ELSE 0 END), 0)
                    + COALESCE(SUM(CASE WHEN past.liked = 1 THEN 1 ELSE 0 END), 0)
                FROM event_log_ts AS past
                WHERE past.user_id = q.user_id
                  AND CAST(past.timestamp AS TIMESTAMP) < CAST(q.as_of AS TIMESTAMP)
                  AND CAST(past.timestamp AS TIMESTAMP) >= CAST(q.as_of AS TIMESTAMP) - INTERVAL 7 DAY
            ) AS total_event_count_7d
        FROM query_points q
        """
    ).df()


def compute_interaction_columns(joined: pd.DataFrame) -> pd.DataFrame:
    """preferred/topic/match 상호작용 피처를 계산해 컬럼으로 추가한다.

    입력 필수 컬럼: hobbies_and_interests_list, historical_category_affinity,
    category_id. (build_training_dataset.py Step 2의 계산을 그대로 이동한 것.)

    preferred_category는 joined에 primary_categories 컬럼이 있으면(virtual_users
    파이프라인의 실제 LLM 산출값, #205) 그 값을 그대로 쓰고, 없으면(구식 mock
    personas.csv 등) derive_preferred_category() 키워드 매핑 fallback을 쓴다.
    """
    out = joined.copy()
    out["preferred_topics"] = out["hobbies_and_interests_list"].apply(extract_keywords_safe)
    if "primary_categories" in out.columns:
        out["preferred_category"] = out["primary_categories"].apply(parse_primary_categories)
    else:
        out["preferred_category"] = out["preferred_topics"].apply(derive_preferred_category)
    out["user_keyword_embeddings"] = out["preferred_topics"].apply(embed_keywords)
    out["topic_similarity"] = out.apply(
        lambda row: compute_topic_similarity(row["user_keyword_embeddings"], row["category_id"]),
        axis=1,
    )
    out["historical_category_match"] = out.apply(
        lambda row: compute_historical_category_match(
            row["historical_category_affinity"], row["category_id"]
        ),
        axis=1,
    )
    out["preferred_category_match"] = out.apply(
        lambda row: compute_preferred_category_match(row["preferred_category"], row["category_id"]),
        axis=1,
    )
    return out
