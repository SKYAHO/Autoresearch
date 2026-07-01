"""
Feast Feature 조회 검증 스크립트

Online / Historical Feature 조회가 정상 동작하는지 확인합니다.

사전 조건:
  1. feast apply 실행 완료
  2. feast materialize 실행 완료 (BigQuery -> Redis 동기화)

사용법:
  python feature_repo/test_feature_retrieval.py
"""

from datetime import datetime, timedelta, timezone

import pandas as pd
from feast import FeatureStore


def test_online_features():
    """get_online_features: 단일 Entity Feature 조회"""
    print("=" * 60)
    print("1. Online Feature 조회 (get_online_features)")
    print("=" * 60)

    store = FeatureStore(repo_path="feature_repo")

    online_features = store.get_online_features(
        features=[
            "user_features:total_watch_count",
            "user_features:avg_watch_duration_sec",
            "video_features:view_count",
            "video_features:category",
        ],
        entity_rows=[
            {"user_id": "user_0001", "video_id": "video_00001"},
            {"user_id": "user_0002", "video_id": "video_00002"},
        ],
    ).to_dict()

    df = pd.DataFrame(online_features)
    print(df.to_string(index=False))
    print(f"\n[OK] Online Feature 조회 성공 ({len(df)} rows)\n")


def test_historical_features():
    """get_historical_features: Entity DataFrame 기반 Feature 조회"""
    print("=" * 60)
    print("2. Historical Feature 조회 (get_historical_features)")
    print("=" * 60)

    store = FeatureStore(repo_path="feature_repo")

    entity_df = pd.DataFrame(
        {
            "user_id": ["user_0001", "user_0002", "user_0003"],
            "video_id": ["video_00001", "video_00002", "video_00003"],
            "event_timestamp": [
                datetime.now(timezone.utc) - timedelta(hours=12),
                datetime.now(timezone.utc) - timedelta(hours=6),
                datetime.now(timezone.utc) - timedelta(hours=1),
            ],
        }
    )

    historical_features = store.get_historical_features(
        entity_df=entity_df,
        features=[
            "user_features:total_watch_count",
            "user_features:avg_watch_duration_sec",
            "video_features:view_count",
            "video_features:like_count",
            "user_video_interaction:watch_time_sec",
            "user_video_interaction:like_ratio",
        ],
    ).to_df()

    print(historical_features.to_string(index=False))
    print(f"\n[OK] Historical Feature 조회 성공 ({len(historical_features)} rows)\n")


if __name__ == "__main__":
    try:
        test_online_features()
    except Exception as e:
        print(f"[FAIL] Online Feature 조회 실패: {e}\n")

    try:
        test_historical_features()
    except Exception as e:
        print(f"[FAIL] Historical Feature 조회 실패: {e}\n")
