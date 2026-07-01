"""
Feast Entity & FeatureView 정의 (더미 스키마)

Entity:
  - user_id: 사용자 식별자
  - video_id: 비디오 식별자

FeatureView:
  - user_features: 사용자 단위 Feature (시청 수, 평균 시청 시간 등)
  - video_features: 비디오 단위 Feature (조회수, 좋아요 수, 카테고리 등)
  - user_video_interaction: 사용자-비디오 상호작용 Feature (시청 시간 등)

추후 SQL 팀에서 정의한 실제 스키마로 교체 예정.
"""

from datetime import timedelta

from feast import Entity, FeatureView, Field, FileSource
from feast.types import Int64, Float32, String


# ============================================================================
# Entity 정의
# ============================================================================

user_entity = Entity(
    name="user_id",
    join_keys=["user_id"],
    description="사용자 식별자",
    tags={"domain": "user"},
)

video_entity = Entity(
    name="video_id",
    join_keys=["video_id"],
    description="비디오 식별자",
    tags={"domain": "video"},
)


# ============================================================================
# Data Source 정의 (더미 parquet 파일 기반)
# 실제 운영 시 BigQuerySource 로 교체
# ============================================================================

user_source = FileSource(
    name="user_features_source",
    path="data/user_features.parquet",
    timestamp_field="event_timestamp",
    description="사용자 단위 Feature 데이터 (더미)",
)

video_source = FileSource(
    name="video_features_source",
    path="data/video_features.parquet",
    timestamp_field="event_timestamp",
    description="비디오 단위 Feature 데이터 (더미)",
)

interaction_source = FileSource(
    name="user_video_interaction_source",
    path="data/user_video_interaction.parquet",
    timestamp_field="event_timestamp",
    description="사용자-비디오 상호작용 Feature 데이터 (더미)",
)


# ============================================================================
# FeatureView 정의
# ============================================================================

user_features_fv = FeatureView(
    name="user_features",
    entities=[user_entity],
    ttl=timedelta(days=30),
    schema=[
        Field(name="total_watch_count", dtype=Int64),
        Field(name="avg_watch_duration_sec", dtype=Float32),
        Field(name="liked_video_count", dtype=Int64),
    ],
    source=user_source,
    online=True,
    tags={"team": "feature-store", "status": "dummy"},
    description="사용자 단위 더미 Feature (추후 실제 스키마로 교체)",
)

video_features_fv = FeatureView(
    name="video_features",
    entities=[video_entity],
    ttl=timedelta(days=30),
    schema=[
        Field(name="view_count", dtype=Int64),
        Field(name="like_count", dtype=Int64),
        Field(name="dislike_count", dtype=Int64),
        Field(name="duration_sec", dtype=Float32),
        Field(name="category", dtype=String),
    ],
    source=video_source,
    online=True,
    tags={"team": "feature-store", "status": "dummy"},
    description="비디오 단위 더미 Feature (추후 실제 스키마로 교체)",
)

user_video_interaction_fv = FeatureView(
    name="user_video_interaction",
    entities=[user_entity, video_entity],
    ttl=timedelta(days=14),
    schema=[
        Field(name="watch_time_sec", dtype=Float32),
        Field(name="like_ratio", dtype=Float32),
    ],
    source=interaction_source,
    online=True,
    tags={"team": "feature-store", "status": "dummy"},
    description="사용자-비디오 상호작용 더미 Feature (추후 실제 스키마로 교체)",
)
