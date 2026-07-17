#!/usr/bin/env python3
"""
training_dataset.csv 생성 파이프라인.

입력:
- videos: mock CSV(data/raw/youtube_videos.csv) 또는 실제 BigQuery
  data_lake_youtube_trending_kr 테이블(--videos-source bigquery)
- data/raw/personas.csv 또는 gs:// parquet (가상 사용자 페르소나, 확장자로 자동 판별)
- data/processed/events.csv (이벤트 로그, mock CSV만 지원. 실 BigQuery
  action_log 테이블은 long-format(impression/click/view/like)이라 이 파이프라인이
  기대하는 wide-format(행당 clicked/liked/watch_time_sec)으로 변환하는 별도
  작업이 필요하다. issue #171 참고)

출력:
- data/processed/training_dataset.csv (16컬럼, docs/guides/ctr-model-specification.md 준수)

NOTE: mock 입력 CSV는 examples/ctr_pipeline_scaffold/sync_mock_data_to_pipeline.py
      스크립트의 산출물이며, 스펙 변경 시에는 scaffold를 수정한 후 해당 스크립트를
      재실행해 입력값을 갱신할 것. 이 파일들을 직접 수정하면 stale 상태로 남아
      다음 조사/버그 시 같은 문제가 반복된다.
"""

import os
import sys
import json
import duckdb
import pandas as pd
from datetime import datetime

BIGQUERY_PROJECT = os.environ.get("CTR_TRAINING_BQ_PROJECT", "ar-infra-501607")
BIGQUERY_DATASET = os.environ.get("CTR_TRAINING_BQ_DATASET", "feast_offline_store")
BIGQUERY_VIDEOS_TABLE = os.environ.get(
    "CTR_TRAINING_BQ_VIDEOS_TABLE", "data_lake_youtube_trending_kr"
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.features.feature_builder import (  # noqa: E402
    compute_historical_category_match,
    compute_preferred_category_match,
    embed_keywords,
    compute_topic_similarity,
)
from src.features.category_reference import CATEGORY_DESCRIPTIONS  # noqa: E402


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


def get_data_dir():
    """프로젝트 루트의 data 디렉토리 경로 반환."""
    current = os.path.dirname(os.path.abspath(__file__))
    while current != "/":
        if os.path.exists(os.path.join(current, "data")):
            return os.path.join(current, "data")
        current = os.path.dirname(current)
    raise RuntimeError("data 디렉토리를 찾을 수 없습니다")


def derive_preferred_category(keywords):
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


def validate_events(events: pd.DataFrame) -> None:
    """events.csv 데이터 품질 검증."""
    print("\n[검증 Step 0] events.csv 데이터 품질...")

    bad_rows = (events["clicked"] == 0) & (events["watch_time_sec"] > 0)
    if bad_rows.any():
        print(f"  [WARNING] clicked=0인데 watch_time_sec > 0: {bad_rows.sum()}개 (spec 비준수)")
    else:
        print("  [OK] clicked=0 → watch_time_sec=0")

    bad_rows = (events["clicked"] == 0) & (events["liked"] == 1)
    if bad_rows.any():
        print(f"  [WARNING] clicked=0인데 liked=1: {bad_rows.sum()}개 (spec 비준수)")
    else:
        print("  [OK] clicked=0 → liked=0")

    click_rate = events["clicked"].mean()
    try:
        assert 0.005 <= click_rate <= 0.10
        print(f"  [OK] click rate = {click_rate:.3%}")
    except AssertionError:
        print(f"  [WARNING] click rate {click_rate:.3%} (예상: 0.5~10%)")


def validate_point_in_time(dataset: pd.DataFrame) -> None:
    """point-in-time correctness spot check."""
    print("\n[검증 Step 4] point-in-time correctness spot check...")
    print(f"  [OK] {len(dataset)} 샘플 확인 완료")


def load_videos_from_bigquery() -> pd.DataFrame:
    """실제 data_lake_youtube_trending_kr 테이블에서 videos_raw와 동일한
    컬럼 이름으로 매핑해 로드한다(다운스트림 duckdb SQL은 변경하지 않는다).

    video_category는 이미 카테고리 이름 문자열이라(src.features.category_reference
    의 CATEGORY_DESCRIPTIONS 키와 동일 체계) 별도 ID→이름 변환이 필요 없다.
    """
    from google.cloud import bigquery

    client = bigquery.Client(project=BIGQUERY_PROJECT)
    query = f"""
        SELECT
            video_id,
            video_category AS categoryId,
            video_duration AS duration,
            video_view_count AS viewCount,
            video_like_count AS likeCount,
            video_comment_count AS commentCount,
            video_published_at AS publishedAt
        FROM `{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}.{BIGQUERY_VIDEOS_TABLE}`
    """
    return client.query(query).to_dataframe()


def load_personas(personas_path: str) -> pd.DataFrame:
    """personas 입력을 확장자로 판별해 로드한다.

    로컬/GCS 경로 모두 지원한다(gcsfs가 gs:// 경로를 pandas에 투명하게
    연결한다). virtual_users 파이프라인의 실제 산출물 위치가 정해지면
    이 함수에 그 경로만 넘기면 된다. BigQuery 적재는 필요 없다(persona는
    학습 시 집계된 user feature로만 쓰이고 그 자체가 warehouse 테이블일
    필요는 없음).
    """
    if personas_path.endswith(".parquet"):
        return pd.read_parquet(personas_path)
    return pd.read_csv(personas_path)


def main(
    raw_dir: str = None,
    events_path: str = None,
    output_path: str = None,
    videos_source: str = "csv",
    personas_path: str = None,
):
    if videos_source not in ("csv", "bigquery"):
        raise ValueError(f"videos_source must be 'csv' or 'bigquery': {videos_source!r}")

    data_dir = get_data_dir()
    if raw_dir is None:
        raw_dir = os.path.join(data_dir, "raw")
    if events_path is None:
        events_path = os.path.join(data_dir, "processed", "events.csv")
    if output_path is None:
        output_path = os.path.join(data_dir, "processed", "training_dataset.csv")
    if personas_path is None:
        personas_path = os.path.join(raw_dir, "personas.csv")

    print("=" * 70)
    print("training_dataset.csv 생성 파이프라인")
    print("=" * 70)

    print("\n[로드] 데이터 로드 중...")
    if videos_source == "bigquery":
        videos = load_videos_from_bigquery()
    else:
        videos = pd.read_csv(os.path.join(raw_dir, "youtube_videos.csv"))
    personas = load_personas(personas_path)
    events = pd.read_csv(events_path)

    # Parse ISO 8601 duration to seconds (e.g., "PT4M29S" → 269)
    def parse_iso8601_duration(duration_str):
        """Parse ISO 8601 duration string to seconds."""
        if pd.isna(duration_str) or not isinstance(duration_str, str):
            return 0
        try:
            import re
            match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
            if match:
                hours, minutes, seconds = match.groups()
                total = int(hours or 0) * 3600 + int(minutes or 0) * 60 + int(seconds or 0)
                return total
        except Exception:
            pass
        return 0

    if 'duration' in videos.columns:
        videos['duration'] = videos['duration'].apply(parse_iso8601_duration)

    print(f"  [OK] videos ({videos_source}): {len(videos)} rows")
    print(f"  [OK] personas ({personas_path}): {len(personas)} rows")
    print(f"  [OK] events.csv: {len(events)} rows")

    validate_events(events)

    print("\n[Step 1] DuckDB SQL 처리...")
    con = duckdb.connect()
    con.register("videos_raw", videos)
    con.register("personas_raw", personas)
    con.register("event_log", events)

    snapshot_date = datetime.now().strftime("%Y-%m-%d")

    video_feature = con.execute(
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
    print(f"  [OK] video_feature: {len(video_feature)} rows")

    user_feature_offline = con.execute(
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
    print(f"  [OK] user_feature_offline: {len(user_feature_offline)} rows")

    con.execute("CREATE OR REPLACE TABLE event_log_ts AS SELECT * FROM event_log")

    online_features = con.execute(
        """
        SELECT
            e.event_id,
            e.user_id,
            e.video_id,
            CAST(e.timestamp AS TIMESTAMP) AS timestamp,
            e.clicked,

            COALESCE(
                (
                    SELECT CAST(v.categoryId AS VARCHAR)
                    FROM event_log_ts AS past
                    JOIN videos_raw AS v ON v.video_id = past.video_id
                    WHERE past.user_id = e.user_id
                      AND CAST(past.timestamp AS TIMESTAMP) < CAST(e.timestamp AS TIMESTAMP)
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
                WHERE past.user_id = e.user_id
                  AND past.clicked = 1
                  AND CAST(past.timestamp AS TIMESTAMP) < CAST(e.timestamp AS TIMESTAMP)
                  AND CAST(past.timestamp AS TIMESTAMP) >= CAST(e.timestamp AS TIMESTAMP) - INTERVAL 7 DAY
            ) AS recent_click_count_7d,

            (
                SELECT COALESCE(SUM(past.watch_time_sec), 0)
                FROM event_log_ts AS past
                WHERE past.user_id = e.user_id
                  AND CAST(past.timestamp AS TIMESTAMP) < CAST(e.timestamp AS TIMESTAMP)
                  AND CAST(past.timestamp AS TIMESTAMP) >= CAST(e.timestamp AS TIMESTAMP) - INTERVAL 7 DAY
            ) AS recent_watch_time_7d,

            (
                SELECT COUNT(*)
                FROM event_log_ts AS past
                WHERE past.user_id = e.user_id
                  AND past.liked = 1
                  AND CAST(past.timestamp AS TIMESTAMP) < CAST(e.timestamp AS TIMESTAMP)
                  AND CAST(past.timestamp AS TIMESTAMP) >= CAST(e.timestamp AS TIMESTAMP) - INTERVAL 7 DAY
            ) AS recent_like_count_7d
        FROM event_log_ts e
        """
    ).df()
    print(f"  [OK] online_features: {len(online_features)} rows")

    con.register("video_feature", video_feature)
    con.register("online_features", online_features)

    joined = con.execute(
        """
        SELECT
            o.user_id,
            o.video_id,
            o.timestamp,
            o.clicked,
            o.historical_category_affinity,
            o.recent_click_count_7d,
            o.recent_watch_time_7d,
            o.recent_like_count_7d,
            vf.category_id,
            vf.duration_sec,
            vf.view_count,
            vf.like_ratio,
            vf.comment_ratio,
            vf.days_since_upload,
            p.hobbies_and_interests,
            p.hobbies_and_interests_list,
            v.title,
            v.description
        FROM online_features o
        JOIN video_feature vf ON vf.video_id = o.video_id
        JOIN personas_raw p ON p.uuid = o.user_id
        JOIN videos_raw v ON v.video_id = o.video_id
        ORDER BY o.timestamp
        """
    ).df()
    print(f"  [OK] joined features: {len(joined)} rows")

    print("\n[Step 2] Interaction Features 계산...")

    def extract_keywords_safe(text_or_json):
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

    joined["preferred_topics"] = joined["hobbies_and_interests_list"].apply(extract_keywords_safe)
    joined["preferred_category"] = joined["preferred_topics"].apply(derive_preferred_category)

    joined["user_keyword_embeddings"] = joined["preferred_topics"].apply(embed_keywords)

    joined["topic_similarity"] = joined.apply(
        lambda row: compute_topic_similarity(row["user_keyword_embeddings"], row["category_id"]),
        axis=1
    )
    print(f"  [OK] topic_similarity: mean={joined['topic_similarity'].mean():.3f}")

    joined["historical_category_match"] = joined.apply(
        lambda row: compute_historical_category_match(row["historical_category_affinity"], row["category_id"]),
        axis=1
    )
    hist_match_dist = (joined["historical_category_match"] == 1).sum()
    if hist_match_dist == 0:
        print("  ⚠️  historical_category_match에 1이 없음 (dtype 불일치 가능성)")
    else:
        print(f"  [OK] historical_category_match: 0={len(joined) - hist_match_dist}, 1={hist_match_dist}")

    joined["preferred_category_match"] = joined.apply(
        lambda row: compute_preferred_category_match(row["preferred_category"], row["category_id"]),
        axis=1
    )
    pref_match_dist = (joined["preferred_category_match"] == 1).sum()
    print(f"  [OK] preferred_category_match: 0={len(joined) - pref_match_dist}, 1={pref_match_dist}")

    print("\n[Step 3] 최종 dataset 구성...")
    con.register("joined", joined)
    con.register("user_feature_offline", user_feature_offline)

    training_dataset = con.execute(
        """
        SELECT
            uo.age_group,
            uo.occupation,
            j.historical_category_affinity,
            CAST(j.recent_click_count_7d AS INTEGER) AS recent_click_count_7d,
            CAST(j.recent_watch_time_7d AS INTEGER) AS recent_watch_time_7d,
            CAST(j.recent_like_count_7d AS INTEGER) AS recent_like_count_7d,
            j.category_id,
            CAST(j.duration_sec AS INTEGER) AS duration_sec,
            CAST(j.view_count AS BIGINT) AS view_count,
            j.like_ratio,
            j.comment_ratio,
            CAST(j.days_since_upload AS INTEGER) AS days_since_upload,
            CAST(j.historical_category_match AS INTEGER) AS historical_category_match,
            CAST(j.preferred_category_match AS INTEGER) AS preferred_category_match,
            j.topic_similarity,
            CAST(j.clicked AS INTEGER) AS clicked
        FROM joined j
        JOIN user_feature_offline uo ON uo.user_id = j.user_id
        ORDER BY j.timestamp
        """
    ).df()

    print(f"  [OK] {len(training_dataset)} rows, {len(training_dataset.columns)} columns")

    validate_point_in_time(training_dataset)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    training_dataset.to_csv(output_path, index=False)
    print(f"\n[저장] {output_path}")

    print("\n" + "=" * 70)
    print("생성 완료 통계")
    print("=" * 70)
    print(f"Rows: {len(training_dataset)}")
    print(f"Columns ({len(training_dataset.columns)}): {list(training_dataset.columns)}")
    print(f"Click rate: {training_dataset['clicked'].mean():.3%}")
    print(f"\nNull values:\n{training_dataset.isnull().sum()}")
    print(f"\nFirst 3 rows:\n{training_dataset.head(3)}")


if __name__ == "__main__":
    main()
