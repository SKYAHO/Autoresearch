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
from datetime import timedelta

import pandas as pd
from feast import Entity, FeatureService, FeatureView, Field
from feast.infra.offline_stores.bigquery import BigQuerySource
from feast.on_demand_feature_view import on_demand_feature_view
from feast.types import Array, Float64, Int64, String
from feast.value_type import ValueType

# 파생 2종 ODFV 본체 재사용(#357 (A)). 순수 문자열 비교 함수라 embeddings의 지연
# import(vertexai)는 이 경로에서 로드되지 않는다.
from src.features.feature_builder import (
    compute_historical_category_match,
    compute_preferred_category_match,
)

# FeatureView ttl 정책 (#357 spec (C) 확정, 2026-07-27):
# - 일 스냅샷 뷰(UserDynamic)만 60h — 당일(≤24h)+1일 결손(≤48h)까지 stale 허용, 2일+는 null.
#   #365 결손이 잦아 1일마다 null 내면 학습이 과도하게 비므로 stale 상한을 60h로 묶는다.
# - 정적/준정적(UserStatic/Similarity)은 갱신 주기가 불규칙이라 ttl=None(무제한). 60h면 정상인데도 매일 null.
# - Video는 일 스냅샷이나 ttl=None — 트렌딩 이탈은 "인기 식음" 신호라 마지막 스냅샷을 유지(모델링 결정).
_USER_DYNAMIC_TTL = timedelta(hours=60)

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
    # 조인 키는 category_key다. 물리 컬럼(category_id)은 그대로 두고 소스 field_mapping으로
    # 매핑한다. category_id를 조인 키로 쓰면 VideoFeatureView의 category_id **피처**와
    # 이름이 겹쳐 BigQuery offline SQL이 ambiguous로 죽는다(#358, Feast 규칙: 피처명 ≠
    # 조인키명). category_id는 모델 계약(MODEL_FEATURE_COLUMNS) 이름이라 그대로 두고,
    # 여기(조인 배관)만 category_key로 구분한다.
    join_keys=["category_key"],
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
    # 물리 컬럼 category_id를 Feast 조인 키 category_key로 매핑(테이블 스키마 불변).
    # category_entity.join_keys=["category_key"]와 짝이다(#358).
    field_mapping={"category_id": "category_key"},
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
    ttl=None,  # 정적: persona 변경 시만 갱신 → 배치 주기 ttl 성립 안 함(#357 (C))
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
    ttl=_USER_DYNAMIC_TTL,  # 일 스냅샷: 1일 결손까지 stale 허용, 2일+는 null(#357 (C))
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
    ttl=None,  # 트렌딩 이탈 = "인기 식음" 신호라 마지막 스냅샷 유지(모델링 결정, #357 (C))
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
    ttl=None,  # 준정적: 임베딩 재계산 시만 갱신 → ttl=None(#357 (C))
    tags={"team": "feature-store"},
    description="사용자-카테고리 topic similarity",
)


# ============================================================================
# On-Demand FeatureView — 파생 2종 (조회 후 계산, 학습·서빙 공통 변환)
# preferred_category_match / historical_category_match는 raw 피처가 아니라
# (user 피처 vs 영상 category) 비교로 나오는 파생값이다. ODFV로 정의해 학습·서빙이
# 같은 변환을 공유 → train/serve skew 원천 차단(#357 (A), #358). 입력은 세 소스 뷰
# (preferred_category=UserStatic, historical_category_affinity=UserDynamic,
# category_id=Video)를 조인한 컬럼이며 feature_builder 함수로 계산한다.
# ============================================================================


def compute_category_matches(inputs: pd.DataFrame) -> pd.DataFrame:
    """ODFV 변환 본체(스토어 없이 단위 테스트 가능하도록 분리).

    inputs는 세 소스 뷰를 조인한 컬럼(preferred_category, historical_category_affinity,
    category_id)을 가진다. 두 파생 매칭을 계산해 반환한다.
    """
    out = pd.DataFrame(index=inputs.index)
    out["preferred_category_match"] = [
        compute_preferred_category_match(pref, cat)
        for pref, cat in zip(inputs["preferred_category"], inputs["category_id"])
    ]
    out["historical_category_match"] = [
        compute_historical_category_match(hist, cat)
        for hist, cat in zip(inputs["historical_category_affinity"], inputs["category_id"])
    ]
    return out


@on_demand_feature_view(
    sources=[user_static_view, user_dynamic_view, video_feature_view],
    schema=[
        Field(name="preferred_category_match", dtype=Int64),
        Field(name="historical_category_match", dtype=Int64),
    ],
)
def category_match_view(inputs: pd.DataFrame) -> pd.DataFrame:
    return compute_category_matches(inputs)


# ============================================================================
# FeatureService — 학습 21피처 묶음 (#358)
# MODEL_FEATURE_COLUMNS(src/features/model_contract.py)와 이름·개수 1:1이어야 한다.
# UserStatic에서 3개(preferred_category/preferred_topics는 ODFV 입력이라 제외),
# UserDynamic 6 + Video 9 + Similarity 1(topic_similarity만) + ODFV 2 = 21.
# ============================================================================

ctr_training_service = FeatureService(
    name="ctr_training_v1",
    features=[
        user_static_view[["age_group", "occupation", "watch_time_band"]],
        user_dynamic_view,
        video_feature_view,
        user_category_similarity_view[["topic_similarity"]],
        category_match_view,
    ],
    tags={"team": "feature-store"},
    description="CTR 학습 데이터셋 21피처(모델 계약과 1:1)",
)
