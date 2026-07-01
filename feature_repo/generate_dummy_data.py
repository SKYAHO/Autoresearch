"""
더미 Feature 데이터 생성 스크립트

생성되는 데이터:
  1. data/user_features.parquet          - 사용자 단위 Feature
  2. data/video_features.parquet         - 비디오 단위 Feature
  3. data/user_video_interaction.parquet - 사용자-비디오 상호작용 Feature

사용법:
  python feature_repo/generate_dummy_data.py

옵션:
  --users N    생성할 사용자 수 (기본 100)
  --videos N   생성할 비디오 수 (기본 200)
  --interactions N  생성할 상호작용 수 (기본 1000)
"""

import argparse
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"

VIDEO_CATEGORIES = [
    "Music", "Gaming", "Sports", "News", "Entertainment",
    "Education", "Science & Technology", "Comedy", "Howto & Style", "Travel",
]


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


def main():
    parser = argparse.ArgumentParser(description="더미 Feature 데이터 생성")
    parser.add_argument("--users", type=int, default=100, help="사용자 수 (기본 100)")
    parser.add_argument("--videos", type=int, default=200, help="비디오 수 (기본 200)")
    parser.add_argument(
        "--interactions", type=int, default=1000,
        help="상호작용 수 (기본 1000)",
    )
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print(f"더미 데이터 생성 중... (users={args.users}, videos={args.videos}, interactions={args.interactions})")

    user_df = generate_user_features(args.users)
    video_df = generate_video_features(args.videos)
    interaction_df = generate_interaction_features(
        args.interactions, args.users, args.videos
    )

    user_path = DATA_DIR / "user_features.parquet"
    video_path = DATA_DIR / "video_features.parquet"
    interaction_path = DATA_DIR / "user_video_interaction.parquet"

    user_df.to_parquet(user_path, index=False)
    video_df.to_parquet(video_path, index=False)
    interaction_df.to_parquet(interaction_path, index=False)

    print(f"\n생성 완료:")
    print(f"  {user_path}        ({len(user_df)} rows)")
    print(f"  {video_path}       ({len(video_df)} rows)")
    print(f"  {interaction_path} ({len(interaction_df)} rows)")

    print(f"\n미리보기 (user_features):")
    print(user_df.head().to_string(index=False))


if __name__ == "__main__":
    main()
