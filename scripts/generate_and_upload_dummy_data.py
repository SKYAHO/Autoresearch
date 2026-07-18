"""
TEMP_FEAST_BOOTSTRAP:
실제 데이터 적재 파이프라인 완료 전 Feast 스키마/조회 검증용 임시 seed script.
실제 BigQuery 적재 파이프라인과 스키마가 확정되면 이 파일은 삭제한다.

더미 Feature 데이터 생성 및 BigQuery 직접 업로드.

업로드 대상 테이블 (feature_repo/feature_definitions.py와 정합):
  1. {project}.{dataset}.user_static_feature
  2. {project}.{dataset}.user_dynamic_feature
  3. {project}.{dataset}.video_feature
  4. {project}.{dataset}.user_category_similarity

사용법:
  uv run --no-dev --group feast python scripts/generate_and_upload_dummy_data.py
"""

import argparse
import os
import random
from datetime import UTC, datetime, timedelta

import pandas as pd
from dotenv import load_dotenv
from google.cloud import bigquery

CATEGORY_IDS = ["1", "10", "17", "20", "22", "23", "24", "25", "26", "28"]
AGE_GROUPS = ["10s", "20s", "30s", "40s", "50s+"]
OCCUPATIONS = ["student", "engineer", "designer", "manager", "etc"]
WATCH_TIME_BANDS = ["light", "medium", "heavy"]
TOPICS = ["music", "gaming", "sports", "news", "education", "entertainment"]


def _event_timestamp() -> datetime:
    return datetime.now(UTC) - timedelta(hours=1)


def generate_user_static(users: int) -> pd.DataFrame:
    ts = _event_timestamp()
    return pd.DataFrame(
        {
            "user_id": [f"user_{i:04d}" for i in range(users)],
            "event_timestamp": [ts] * users,
            "age_group": [random.choice(AGE_GROUPS) for _ in range(users)],
            "occupation": [random.choice(OCCUPATIONS) for _ in range(users)],
            "preferred_category": [
                random.sample(CATEGORY_IDS, k=3) for _ in range(users)
            ],
            "preferred_topics": [
                random.sample(TOPICS, k=2) for _ in range(users)
            ],
            "watch_time_band": [
                random.choice(WATCH_TIME_BANDS) for _ in range(users)
            ],
        }
    )


def generate_user_dynamic(users: int) -> pd.DataFrame:
    ts = _event_timestamp()
    clicks = [random.randint(0, 200) for _ in range(users)]
    views = [random.randint(0, 500) for _ in range(users)]
    likes = [random.randint(0, 50) for _ in range(users)]
    return pd.DataFrame(
        {
            "user_id": [f"user_{i:04d}" for i in range(users)],
            "event_timestamp": [ts] * users,
            "recent_click_count_7d": clicks,
            "recent_view_count_7d": views,
            "recent_watch_time_7d": [
                random.randint(0, 100_000) for _ in range(users)
            ],
            "recent_like_count_7d": likes,
            "historical_category_affinity": [
                random.choice(CATEGORY_IDS) for _ in range(users)
            ],
            "total_event_count_7d": [
                c + v + l for c, v, l in zip(clicks, views, likes)
            ],
        }
    )


def generate_video(videos: int) -> pd.DataFrame:
    ts = _event_timestamp()
    return pd.DataFrame(
        {
            "video_id": [f"video_{i:05d}" for i in range(videos)],
            "event_timestamp": [ts] * videos,
            "category_id": [
                random.choice(CATEGORY_IDS) for _ in range(videos)
            ],
            "duration_sec": [random.randint(30, 3600) for _ in range(videos)],
            "view_count": [
                random.randint(100, 10_000_000) for _ in range(videos)
            ],
            "like_ratio": [round(random.random(), 4) for _ in range(videos)],
            "comment_ratio": [
                round(random.random() / 10, 4) for _ in range(videos)
            ],
            "days_since_upload": [
                random.randint(0, 3650) for _ in range(videos)
            ],
            "channel_subscriber_count": [
                random.randint(0, 5_000_000) for _ in range(videos)
            ],
            "channel_view_count": [
                random.randint(0, 1_000_000_000) for _ in range(videos)
            ],
            "channel_video_count": [
                random.randint(1, 5000) for _ in range(videos)
            ],
        }
    )


def generate_user_category_similarity(users: int) -> pd.DataFrame:
    ts = _event_timestamp()
    rows = []
    for i in range(users):
        for category_id in random.sample(CATEGORY_IDS, k=3):
            rows.append(
                {
                    "user_id": f"user_{i:04d}",
                    "category_id": category_id,
                    "event_timestamp": ts,
                    "topic_similarity": round(random.random(), 4),
                    "topic_similarity_top_topic": random.choice(TOPICS),
                }
            )
    return pd.DataFrame(rows)


def upload(client: bigquery.Client, dataset: str, table: str, df: pd.DataFrame):
    table_id = f"{client.project}.{dataset}.{table}"
    job_config = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE")
    client.load_table_from_dataframe(df, table_id, job_config=job_config).result()
    print(f"[OK] {table_id}: {len(df)} rows")


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--videos", type=int, default=200)
    parser.add_argument(
        "--project", default=os.environ.get("GCP_PROJECT_ID")
    )
    parser.add_argument(
        "--dataset", default=os.environ.get("BQ_DATASET", "feast_offline_store")
    )
    args = parser.parse_args()
    if not args.project:
        raise SystemExit("GCP_PROJECT_ID (또는 --project)가 필요합니다")

    client = bigquery.Client(project=args.project)
    upload(client, args.dataset, "user_static_feature", generate_user_static(args.users))
    upload(client, args.dataset, "user_dynamic_feature", generate_user_dynamic(args.users))
    upload(client, args.dataset, "video_feature", generate_video(args.videos))
    upload(
        client,
        args.dataset,
        "user_category_similarity",
        generate_user_category_similarity(args.users),
    )


if __name__ == "__main__":
    main()
