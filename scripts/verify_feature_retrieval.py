"""
TEMP_FEAST_BOOTSTRAP:
현재 조회 검증은 임시 더미 데이터 기준이다.
실제 BigQuery 적재 파이프라인과 스키마가 확정되면 실제 데이터 기준으로 교체한다.

Feast Feature 조회 검증 스크립트.

사전 조건:
  1. feast apply 실행 완료
  2. python -m autoresearch.jobs.feast_materialize 실행 완료

사용법:
  uv run --no-dev --group feast python scripts/verify_feature_retrieval.py
"""

import pandas as pd
from dotenv import load_dotenv
from feast import FeatureStore


def verify_online_features(store: FeatureStore) -> None:
    print("=" * 60)
    print("1. Online Feature 조회 (get_online_features)")
    print("=" * 60)

    online_features = store.get_online_features(
        features=[
            "UserStaticView:age_group",
            "UserStaticView:watch_time_band",
            "UserDynamicView:recent_view_count_7d",
            "VideoFeatureView:view_count",
            "VideoFeatureView:like_ratio",
        ],
        entity_rows=[
            {"user_id": "user_0001", "video_id": "video_00001"},
            {"user_id": "user_0002", "video_id": "video_00002"},
        ],
    ).to_dict()

    df = pd.DataFrame(online_features)
    print(df.to_string(index=False))
    if df["age_group"].isna().all():
        raise SystemExit("[FAIL] online feature 값이 비어 있습니다")
    print(f"\n[OK] Online Feature 조회 성공 ({len(df)} rows)\n")


def verify_similarity_view(store: FeatureStore) -> None:
    print("=" * 60)
    print("2. 복합 Entity 조회 (UserCategorySimilarityView)")
    print("=" * 60)

    online_features = store.get_online_features(
        features=[
            "UserCategorySimilarityView:topic_similarity",
            "UserCategorySimilarityView:topic_similarity_top_topic",
        ],
        entity_rows=[{"user_id": "user_0001", "category_id": "10"}],
    ).to_dict()

    df = pd.DataFrame(online_features)
    print(df.to_string(index=False))
    print(f"\n[OK] 복합 Entity 조회 성공 ({len(df)} rows)\n")


def main() -> None:
    load_dotenv()
    store = FeatureStore(repo_path="feature_repo")
    verify_online_features(store)
    verify_similarity_view(store)


if __name__ == "__main__":
    main()
