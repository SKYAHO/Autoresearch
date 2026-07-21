"""학습·시뮬레이션 공용 피처 조립 함수.

build_training_dataset.main()의 인라인 DuckDB SQL과 interaction 계산을 추출한
것이다. 학습 데이터셋 생성과 정책 시뮬레이션 라운드(simulate_policy_round)가
같은 코드로 피처를 계산해 학습-서빙 스큐를 방지한다.
"""

__arch__ = {
    "stage": "training",
    "role": "학습과 정책 시뮬레이션에서 공유하는 사용자·영상 피처를 조립합니다.",
    "owns": [
        "영상·사용자·상호작용 피처 계산",
        "point-in-time 사용자 피처 조회",
        "학습·시뮬레이션 공용 피처 컬럼 계약",
    ],
    "not_owns": [
        "정책별 노출 선택",
        "모델 학습과 추론 서비스",
    ],
}

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
    """TEMPORARY MOCK: 키워드 리스트에서 선호 카테고리 파생.

    실제 User Feature Specification 구현 전까지의 임시 mock 로직.
    LLM이 persona를 기반으로 YouTube 카테고리 1~3개를 직접 선택하는 방식으로
    향후 교체 필요 (docs/guides/ctr-model-specification.md, User Feature Specification 참고).

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
    """영상 원본 컬럼(categoryId/duration/viewCount/...)에서 모델 영상 피처를 계산한다."""
    datetime.strptime(snapshot_date, "%Y-%m-%d")  # SQL 보간 전 형식 검증
    con = duckdb.connect()
    con.register("videos_raw", videos_raw)
    return con.execute(
        f"""
        SELECT
            video_id,
            CAST(categoryId AS VARCHAR) AS category_id,
            COALESCE(CAST(duration AS INTEGER), 300) AS duration_sec,
            CAST(viewCount AS BIGINT) AS view_count,
            ROUND(CAST(likeCount AS FLOAT) / NULLIF(CAST(viewCount AS FLOAT), 0), 4) AS like_ratio,
            ROUND(CAST(commentCount AS FLOAT) / NULLIF(CAST(viewCount AS FLOAT), 0), 4) AS comment_ratio,
            DATE_DIFF('day', CAST(publishedAt AS DATE), DATE '{snapshot_date}') AS days_since_upload
        FROM videos_raw
        """
    ).df()


def compute_user_offline_features(personas_raw: pd.DataFrame) -> pd.DataFrame:
    """persona 원본(uuid/age/occupation)에서 오프라인 유저 피처를 계산한다."""
    con = duckdb.connect()
    con.register("personas_raw", personas_raw)
    return con.execute(
        """
        SELECT
            uuid AS user_id,
            CASE
                WHEN age < 20 THEN '10s'
                WHEN age < 30 THEN '20s'
                WHEN age < 40 THEN '30s'
                WHEN age < 50 THEN '40s'
                ELSE '50s+'
            END AS age_group,
            occupation
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
            ) AS recent_like_count_7d
        FROM query_points q
        """
    ).df()


def compute_interaction_columns(joined: pd.DataFrame) -> pd.DataFrame:
    """preferred/topic/match 상호작용 피처를 계산해 컬럼으로 추가한다.

    입력 필수 컬럼: hobbies_and_interests_list, historical_category_affinity,
    category_id. (build_training_dataset.py Step 2의 계산을 그대로 이동한 것.)
    """
    out = joined.copy()
    out["preferred_topics"] = out["hobbies_and_interests_list"].apply(extract_keywords_safe)
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
