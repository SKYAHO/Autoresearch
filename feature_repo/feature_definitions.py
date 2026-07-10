"""
Feast Entity & FeatureView 정의.

Entity (이름은 개념, join_keys는 컬럼 — 공식 quickstart 컨벤션):
  - user (join_key: user_id)
  - video (join_key: video_id)
  - category (join_key: category_id)

FeatureView (BigQuery source table → FeatureView):
  - user_static_feature      → UserStaticView (user)
  - user_dynamic_feature     → UserDynamicView (user)
  - video_feature            → VideoFeatureView (video)
  - user_category_similarity → UserCategorySimilarityView (user, category)
"""

import os

from feast import Entity, FeatureView, Field
from feast.infra.offline_stores.bigquery import BigQuerySource
from feast.types import Array, Float64, Int64, String
from feast.value_type import ValueType

GCP_PROJECT = os.environ["GCP_PROJECT_ID"]
BQ_DATASET = os.environ["BQ_DATASET"]


# ============================================================================
# Entity 정의
# ============================================================================

user_entity = Entity(
    name="user",
    join_keys=["user_id"],
    value_type=ValueType.STRING,
    description="사용자",
    tags={"domain": "user"},
)

video_entity = Entity(
    name="video",
    join_keys=["video_id"],
    value_type=ValueType.STRING,
    description="비디오",
    tags={"domain": "video"},
)

category_entity = Entity(
    name="category",
    join_keys=["category_id"],
    value_type=ValueType.STRING,
    description="비디오 카테고리",
    tags={"domain": "category"},
)


# ============================================================================
# Data Source 정의 (BigQuery 테이블 기반)
# 테이블: {GCP_PROJECT}.{BQ_DATASET}.{table_name}
# ============================================================================

user_static_source = BigQuerySource(
    name="user_static_feature_source",
    table=f"{GCP_PROJECT}.{BQ_DATASET}.user_static_feature",
    timestamp_field="event_timestamp",
    description="사용자 정적 Feature",
)

user_dynamic_source = BigQuerySource(
    name="user_dynamic_feature_source",
    table=f"{GCP_PROJECT}.{BQ_DATASET}.user_dynamic_feature",
    timestamp_field="event_timestamp",
    description="사용자 동적 Feature (최근 7일 집계)",
)

video_source = BigQuerySource(
    name="video_feature_source",
    table=f"{GCP_PROJECT}.{BQ_DATASET}.video_feature",
    timestamp_field="event_timestamp",
    description="비디오 Feature",
)

user_category_similarity_source = BigQuerySource(
    name="user_category_similarity_source",
    table=f"{GCP_PROJECT}.{BQ_DATASET}.user_category_similarity",
    timestamp_field="event_timestamp",
    description="사용자-카테고리 topic similarity",
)


# ============================================================================
# FeatureView 정의
# ============================================================================

user_static_view = FeatureView(
    name="UserStaticView",
    entities=[user_entity],
    schema=[
        Field(name="age_group", dtype=String),
        Field(name="occupation", dtype=String),
        Field(name="preferred_category", dtype=Array(String)),
        Field(name="preferred_topics", dtype=Array(String)),
        Field(name="watch_time_band", dtype=String),
    ],
    source=user_static_source,
    online=True,
    tags={"team": "feature-store"},
    description="사용자 정적 Feature",
)

user_dynamic_view = FeatureView(
    name="UserDynamicView",
    entities=[user_entity],
    schema=[
        Field(name="recent_click_count_7d", dtype=Int64),
        Field(name="recent_view_count_7d", dtype=Int64),
        Field(name="recent_watch_time_7d", dtype=Int64),
        Field(name="recent_like_count_7d", dtype=Int64),
        Field(name="historical_category_affinity", dtype=String),
        Field(name="total_event_count_7d", dtype=Int64),
    ],
    source=user_dynamic_source,
    online=True,
    tags={"team": "feature-store"},
    description="사용자 동적 Feature (최근 7일 집계)",
)

video_feature_view = FeatureView(
    name="VideoFeatureView",
    entities=[video_entity],
    schema=[
        Field(name="category_id", dtype=String),
        Field(name="duration_sec", dtype=Int64),
        Field(name="view_count", dtype=Int64),
        Field(name="like_ratio", dtype=Float64),
        Field(name="comment_ratio", dtype=Float64),
        Field(name="days_since_upload", dtype=Int64),
        Field(name="channel_subscriber_count", dtype=Int64),
        Field(name="channel_view_count", dtype=Int64),
        Field(name="channel_video_count", dtype=Int64),
    ],
    source=video_source,
    online=True,
    tags={"team": "feature-store"},
    description="비디오 Feature",
)

user_category_similarity_view = FeatureView(
    name="UserCategorySimilarityView",
    entities=[user_entity, category_entity],
    schema=[
        Field(name="topic_similarity", dtype=Float64),
        Field(name="topic_similarity_top_topic", dtype=String),
    ],
    source=user_category_similarity_source,
    online=True,
    tags={"team": "feature-store"},
    description="사용자-카테고리 topic similarity",
)
