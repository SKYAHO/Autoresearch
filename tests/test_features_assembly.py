"""src/features/assembly.py 공용 피처 조립 함수 단위 테스트."""

import pandas as pd

from src.features.assembly import (
    compute_interaction_columns,
    compute_point_in_time_user_features,
    compute_user_offline_features,
    compute_video_features,
)


def _videos_raw() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "video_id": ["v1", "v2"],
            "categoryId": ["Gaming", "Music"],
            "duration": [120, None],
            "viewCount": [1000, 0],
            "likeCount": [100, 5],
            "commentCount": [10, 1],
            "publishedAt": ["2026-07-01", "2026-07-10"],
        }
    )


def test_compute_video_features_columns_and_values():
    out = compute_video_features(_videos_raw(), "2026-07-11")
    assert list(out.columns) == [
        "video_id", "category_id", "duration_sec", "view_count",
        "like_ratio", "comment_ratio", "days_since_upload",
    ]
    v1 = out[out["video_id"] == "v1"].iloc[0]
    assert v1["duration_sec"] == 120
    assert v1["like_ratio"] == 0.1
    assert v1["days_since_upload"] == 10
    v2 = out[out["video_id"] == "v2"].iloc[0]
    assert v2["duration_sec"] == 300  # COALESCE 기본값
    assert pd.isna(v2["like_ratio"])  # viewCount=0 → NULLIF → NULL


def test_compute_user_offline_features_age_group_buckets():
    personas = pd.DataFrame(
        {"uuid": ["u1", "u2", "u3"], "age": [19, 34, 60], "occupation": ["s", "o", "r"]}
    )
    out = compute_user_offline_features(personas)
    assert list(out.columns) == ["user_id", "age_group", "occupation"]
    assert out["age_group"].tolist() == ["10s", "30s", "50s+"]


def test_compute_point_in_time_user_features_respects_as_of():
    # u1: as_of 이전 클릭 1건(Gaming) → affinity=Gaming, count=1. as_of 이후 이벤트는 무시.
    events = pd.DataFrame(
        {
            "event_id": ["e1", "e2"],
            "user_id": ["u1", "u1"],
            "video_id": ["v1", "v2"],
            "timestamp": ["2026-07-10 10:00:00", "2026-07-12 10:00:00"],
            "clicked": [1, 1],
            "liked": [0, 1],
            "watch_time_sec": [60, 30],
        }
    )
    query_points = pd.DataFrame(
        {"user_id": ["u1"], "as_of": ["2026-07-11 00:00:00"], "tag": ["q1"]}
    )
    out = compute_point_in_time_user_features(events, _videos_raw(), query_points)
    row = out.iloc[0]
    assert row["tag"] == "q1"  # carry 컬럼 보존
    assert row["historical_category_affinity"] == "Gaming"
    assert row["recent_click_count_7d"] == 1
    assert row["recent_watch_time_7d"] == 60
    assert row["recent_like_count_7d"] == 0


def test_compute_interaction_columns_matches():
    joined = pd.DataFrame(
        {
            "hobbies_and_interests_list": ['["gaming"]'],
            "historical_category_affinity": ["Gaming"],
            "category_id": ["Gaming"],
        }
    )
    out = compute_interaction_columns(joined)
    assert out["historical_category_match"].iloc[0] == 1
    assert out["preferred_category_match"].iloc[0] in (0, 1)
    assert 0.0 <= abs(out["topic_similarity"].iloc[0]) <= 1.0
