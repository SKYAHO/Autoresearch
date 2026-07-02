"""
더미 Feature 데이터 생성 및 BigQuery 직접 업로드

parquet이나 GCS를 거치지 않고, DataFrame을 BigQuery 테이블에 직접 적재합니다.

업로드 대상 테이블:
  1. {project}.{dataset}.user_features
  2. {project}.{dataset}.video_features
  3. {project}.{dataset}.user_video_interaction

사전 조건:
  - BigQuery 데이터셋(feast_offline_store)이 생성되어 있어야 함
  - GOOGLE_APPLICATION_CREDENTIALS 환경 변수에 서비스 계정 키 경로 지정

사용법:
  GOOGLE_APPLICATION_CREDENTIALS=./keys/service-account.json \
  python scripts/generate_and_upload_dummy_data.py

옵션:
  --users N          생성할 사용자 수 (기본 100)
  --videos N         생성할 비디오 수 (기본 200)
  --interactions N   생성할 상호작용 수 (기본 1000)
  --project PROJECT  GCP 프로젝트 ID (기본: .env의 GCP_PROJECT_ID)
  --dataset DATASET  BigQuery 데이터셋 (기본: feast_offline_store)
"""

import argparse
import os
import random
import sys
from datetime import datetime, timedelta, timezone

import pandas as pd

try:
    from dotenv import load_dotenv
except ImportError:
    print("python-dotenv 가 필요합니다: pip install python-dotenv")
    sys.exit(1)

try:
    from google.cloud import bigquery
except ImportError:
    print("google-cloud-bigquery 가 필요합니다: pip install google-cloud-bigquery")
    sys.exit(1)

VIDEO_CATEGORIES = [
    "Music", "Gaming", "Sports", "News", "Entertainment",
    "Education", "Science & Technology", "Comedy", "Howto & Style", "Travel",
]


# ============================================================================
# 데이터 생성 함수
# ============================================================================

def generate_user_features(n_users: int = 100) -> pd.DataFrame:
    users = []
    base_time = datetime.now(timezone.utc) - timedelta(days=1)

    for i in range(1, n_users + 1):
        users.append({
            "user_id": f"user_{i:04d}",
            "total_watch_count": random.randint(5, 5000),
            "avg_watch_duration_sec": round(random.uniform(30.0, 1200.0), 2),
            "liked_video_count": random.randint(0, 800),
            "event_timestamp": base_time - timedelta(hours=random.randint(0, 23)),
        })

    return pd.DataFrame(users)


def generate_video_features(n_videos: int = 200) -> pd.DataFrame:
    videos = []
    base_time = datetime.now(timezone.utc) - timedelta(days=1)

    for i in range(1, n_videos + 1):
        view_count = random.randint(100, 10_000_000)
        like_count = int(view_count * random.uniform(0.01, 0.15))
        dislike_count = int(like_count * random.uniform(0.01, 0.3))

        videos.append({
            "video_id": f"video_{i:05d}",
            "view_count": view_count,
            "like_count": like_count,
            "dislike_count": dislike_count,
            "duration_sec": round(random.uniform(60.0, 3600.0), 2),
            "category": random.choice(VIDEO_CATEGORIES),
            "event_timestamp": base_time - timedelta(hours=random.randint(0, 23)),
        })

    return pd.DataFrame(videos)


def generate_interaction_features(
    n_interactions: int = 1000,
    n_users: int = 100,
    n_videos: int = 200,
) -> pd.DataFrame:
    interactions = []
    base_time = datetime.now(timezone.utc) - timedelta(days=1)

    for _ in range(n_interactions):
        interactions.append({
            "user_id": f"user_{random.randint(1, n_users):04d}",
            "video_id": f"video_{random.randint(1, n_videos):05d}",
            "watch_time_sec": round(random.uniform(10.0, 3600.0), 2),
            "like_ratio": round(random.uniform(0.0, 1.0), 4),
            "event_timestamp": base_time - timedelta(hours=random.randint(0, 47)),
        })

    return pd.DataFrame(interactions)


# ============================================================================
# BigQuery 업로드 함수
# ============================================================================

def upload_to_bigquery(
    df: pd.DataFrame,
    table_name: str,
    project: str,
    dataset: str,
    client: bigquery.Client,
):
    table_id = f"{project}.{dataset}.{table_name}"
    job_config = bigquery.LoadJobConfig(
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )

    print(f"  업로드 중: {table_name} -> {table_id}")
    job = client.load_table_from_dataframe(df, table_id, job_config=job_config)
    job.result()

    table = client.get_table(table_id)
    print(f"    [OK] {table.num_rows} rows 적재 완료")


# ============================================================================
# 메인
# ============================================================================

def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="더미 Feature 데이터 생성 및 BigQuery 업로드")
    parser.add_argument("--users", type=int, default=100, help="사용자 수 (기본 100)")
    parser.add_argument("--videos", type=int, default=200, help="비디오 수 (기본 200)")
    parser.add_argument("--interactions", type=int, default=1000, help="상호작용 수 (기본 1000)")
    parser.add_argument("--project", default=os.getenv("GCP_PROJECT_ID"))
    parser.add_argument("--dataset", default=os.getenv("BQ_DATASET", "feast_offline_store"))
    args = parser.parse_args()

    if not args.project:
        print("[ERROR] --project 또는 .env의 GCP_PROJECT_ID 필요")
        sys.exit(1)

    print(f"더미 데이터 생성 및 BigQuery 업로드")
    print(f"  Project:  {args.project}")
    print(f"  Dataset:  {args.dataset}")
    print(f"  Users:    {args.users}")
    print(f"  Videos:   {args.videos}")
    print(f"  Interactions: {args.interactions}")
    print()

    # 데이터 생성
    print("1. 더미 데이터 생성")
    user_df = generate_user_features(args.users)
    video_df = generate_video_features(args.videos)
    interaction_df = generate_interaction_features(args.interactions, args.users, args.videos)
    print(f"  user_features:          {len(user_df)} rows")
    print(f"  video_features:         {len(video_df)} rows")
    print(f"  user_video_interaction: {len(interaction_df)} rows")
    print()

    # BigQuery 업로드
    print("2. BigQuery 업로드")
    bq_client = bigquery.Client(project=args.project)

    upload_to_bigquery(user_df, "user_features", args.project, args.dataset, bq_client)
    upload_to_bigquery(video_df, "video_features", args.project, args.dataset, bq_client)
    upload_to_bigquery(interaction_df, "user_video_interaction", args.project, args.dataset, bq_client)

    print(f"\n[완료] BigQuery 업로드 성공")


if __name__ == "__main__":
    main()
