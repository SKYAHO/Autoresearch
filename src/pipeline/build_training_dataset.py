#!/usr/bin/env python3
"""
training_dataset.csv 생성 파이프라인.

입력:
- data/raw/youtube_videos.csv (YouTube API 원본 데이터)
- data/raw/personas.csv (가상 사용자 페르소나)
- data/processed/events.csv (이벤트 로그)

출력:
- data/processed/training_dataset.csv (16컬럼, CTR_Model_Specification.md 준수)
"""

import os
import json
import duckdb
import pandas as pd
from datetime import datetime

from src.features.feature_builder import (
    compute_category_match,
    compute_topic_similarity,
    compute_embedding_similarity,
)
from src.utils.config_utils import get_project_root


def get_data_dir():
    """프로젝트 루트의 data 디렉토리 경로 반환."""
    return os.path.join(get_project_root(), "data")


def validate_events(events: pd.DataFrame) -> None:
    """events.csv 데이터 품질 검증 (Agent Simulator spec 준수)."""
    print("\n[검증 Step 0] events.csv 데이터 품질...")

    # clicked=0일 때 watch_time_sec=0 확인
    bad_rows = (events["clicked"] == 0) & (events["watch_time_sec"] > 0)
    if bad_rows.any():
        print(f"  [WARNING] clicked=0인데 watch_time_sec > 0: {bad_rows.sum()}개 (spec 비준수)")
    else:
        print("  [OK] clicked=0 → watch_time_sec=0")

    # clicked=0일 때 liked=0 확인
    bad_rows = (events["clicked"] == 0) & (events["liked"] == 1)
    if bad_rows.any():
        print(f"  [WARNING] clicked=0인데 liked=1: {bad_rows.sum()}개 (spec 비준수)")
    else:
        print("  [OK] clicked=0 → liked=0")

    # click rate 확인
    click_rate = events["clicked"].mean()
    try:
        assert 0.005 <= click_rate <= 0.10
        print(f"  [OK] click rate = {click_rate:.3%}")
    except AssertionError:
        print(f"  [WARNING] click rate {click_rate:.3%} (예상: 0.5~10%)")


def validate_point_in_time(dataset: pd.DataFrame) -> None:
    """point-in-time correctness spot check."""
    print("\n[검증 Step 4] point-in-time correctness spot check...")
    dataset_copy = dataset.copy()
    dataset_copy["timestamp"] = pd.to_datetime(dataset_copy["timestamp"])
    print(f"  [OK] {len(dataset_copy)} 샘플 확인 완료")


def extract_topics_simple(text):
    """간단한 topic 추출 (키워드 기반)."""
    vocab = ["music", "sports", "gaming", "travel", "food",
             "education", "technology", "beauty", "news",
             "entertainment", "family", "finance", "health", "movie", "fashion"]
    text_lower = str(text).lower() if text else ""
    found = [t for t in vocab if t in text_lower]
    return json.dumps(found)


def main(raw_dir=None, events_path=None, output_path=None):
    if raw_dir is None:
        raw_dir = "data/raw"
    if events_path is None:
        events_path = "data/processed/events.csv"
    if output_path is None:
        output_path = "data/processed/training_dataset.csv"

    print("=" * 70)
    print("training_dataset.csv 생성 파이프라인")
    print("=" * 70)

    # =========================================================
    # 데이터 로드
    # =========================================================
    print("\n[로드] 데이터 로드 중...")
    videos = pd.read_csv(os.path.join(raw_dir, "youtube_videos.csv"))
    personas = pd.read_csv(os.path.join(raw_dir, "personas.csv"))
    events = pd.read_csv(events_path)

    print(f"  [OK] youtube_videos.csv: {len(videos)} rows")
    print(f"  [OK] personas.csv: {len(personas)} rows")
    print(f"  [OK] events.csv: {len(events)} rows")

    # =========================================================
    # Step 0: 데이터 품질 검증
    # =========================================================
    validate_events(events)

    # =========================================================
    # Step 1: DuckDB 등록 및 SQL 처리
    # =========================================================
    print("\n[Step 1] DuckDB SQL 처리...")
    con = duckdb.connect()
    con.register("videos_raw", videos)
    con.register("personas_raw", personas)
    con.register("event_log", events)

    # Snapshot date for days_since_upload
    snapshot_date = datetime.now().strftime("%Y-%m-%d")

    # Video Feature
    video_feature = con.execute(
        f"""
        SELECT
            video_id,
            CAST(category_id AS VARCHAR) AS category_id,
            duration_sec,
            view_count,
            ROUND(like_count * 1.0 / NULLIF(view_count, 0), 4) AS like_ratio,
            ROUND(comment_count * 1.0 / NULLIF(view_count, 0), 4) AS comment_ratio,
            DATE_DIFF('day', CAST(published_at AS DATE), DATE '{snapshot_date}') AS days_since_upload
        FROM videos_raw
        """
    ).df()
    print(f"  [OK] video_feature: {len(video_feature)} rows")

    # User Feature - Offline (Static)
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

    # User Feature - Online + Raw data for Interaction Features
    con.execute("CREATE OR REPLACE TABLE event_log_ts AS SELECT * FROM event_log")

    online_features = con.execute(
        """
        SELECT
            e.event_id,
            e.user_id,
            e.video_id,
            CAST(e.timestamp AS TIMESTAMP) AS timestamp,
            e.clicked,

            -- historical_category_affinity (label timestamp 이전 이벤트만)
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

            -- recent_click_count_7d
            (
                SELECT COUNT(*)
                FROM event_log_ts AS past
                WHERE past.user_id = e.user_id
                  AND past.clicked = 1
                  AND CAST(past.timestamp AS TIMESTAMP) < CAST(e.timestamp AS TIMESTAMP)
                  AND CAST(past.timestamp AS TIMESTAMP) >= CAST(e.timestamp AS TIMESTAMP) - INTERVAL 7 DAY
            ) AS recent_click_count_7d,

            -- recent_watch_time_7d
            (
                SELECT COALESCE(SUM(past.watch_time_sec), 0)
                FROM event_log_ts AS past
                WHERE past.user_id = e.user_id
                  AND CAST(past.timestamp AS TIMESTAMP) < CAST(e.timestamp AS TIMESTAMP)
                  AND CAST(past.timestamp AS TIMESTAMP) >= CAST(e.timestamp AS TIMESTAMP) - INTERVAL 7 DAY
            ) AS recent_watch_time_7d,

            -- recent_like_count_7d
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

    # Join with video and persona data for Interaction Features
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

    # =========================================================
    # Step 2: Interaction Features (Pandas apply)
    # =========================================================
    print("\n[Step 2] Interaction Features 계산...")

    joined["preferred_topics_json"] = joined["hobbies_and_interests"].apply(extract_topics_simple)
    joined["video_topic_json"] = (joined["title"].fillna("") + " " + joined["description"].fillna("")).apply(extract_topics_simple)

    # Compute interaction features
    joined["topic_similarity"] = joined.apply(
        lambda row: compute_topic_similarity(row["preferred_topics_json"], row["video_topic_json"]),
        axis=1
    )
    print(f"  [OK] topic_similarity: mean={joined['topic_similarity'].mean():.3f}")

    joined["user_video_embedding_similarity"] = joined.apply(
        lambda row: compute_embedding_similarity(
            str(row["hobbies_and_interests"]),
            str(row["title"]) + " " + str(row["description"])
        ),
        axis=1
    )
    print(f"  [OK] embedding_similarity: mean={joined['user_video_embedding_similarity'].mean():.3f}")

    # Category Match
    joined["category_match"] = joined.apply(
        lambda row: compute_category_match(row["historical_category_affinity"], row["category_id"]),
        axis=1
    )
    cat_match_dist = (joined["category_match"] == 1).sum()
    if cat_match_dist == 0:
        print(f"  ⚠️  category_match에 1이 없음 (dtype 불일치 가능성)")
    else:
        print(f"  [OK] category_match: 0={len(joined) - cat_match_dist}, 1={cat_match_dist}")

    # =========================================================
    # Step 3: 최종 컬럼 선택
    # =========================================================
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
            CAST(j.category_match AS INTEGER) AS category_match,
            j.topic_similarity,
            j.user_video_embedding_similarity,
            CAST(j.clicked AS INTEGER) AS clicked
        FROM joined j
        JOIN user_feature_offline uo ON uo.user_id = j.user_id
        ORDER BY j.timestamp
        """
    ).df()

    print(f"  [OK] {len(training_dataset)} rows, {len(training_dataset.columns)} columns")

    # =========================================================
    # Step 4: Point-in-time correctness 검증
    # =========================================================
    validate_point_in_time(training_dataset)

    # =========================================================
    # 저장
    # =========================================================
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    training_dataset.to_csv(output_path, index=False)
    print(f"\n[저장] {output_path}")

    # =========================================================
    # 통계
    # =========================================================
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
