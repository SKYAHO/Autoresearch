# Feature Store

## Table of Contents

- [Overview](#overview)
- [Feature View Source Table Columns](#feature-view-source-table-columns)
- [Feast Feature Columns](#feast-feature-columns)
- [Entity / Timestamp / Feature Column 구분](#entity-timestamp-feature-column)
- [Feature Store에 등록하지 않는 대상](#feature-store-exclusions)
- [Derived / Interaction Feature 처리 기준](#derived-interaction-feature)
- [정리](#summary)

<a id="overview"></a>

## Overview

> [!NOTE]
> #### **BigQuery Source Table 이름** → **Feast Feature View 이름 (`Entity`)**
>
> - **user_static_feature → UserStaticView (`user_id`)**
> - **user_dynamic_feature → UserDynamicView (`user_id`)**
> - **video_feature → VideoFeatureView (`video_id`)**
> - **user_category_similarity → `UserCategorySimilarityView` (`user_id`, `category_id`)**

---

<a id="feature-view-source-table-columns"></a>

## 🔸 Feature View Source Table (**BigQuery)** 컬럼

| Source table | Entity columns | Timestamp column | Feature View columns |
| --- | --- | --- | --- |
| **user_static_feature** | `user_id` | `event_timestamp` | `age_group`, `occupation`, `preferred_category`, `preferred_topics`, `watch_time_band` |
| **user_dynamic_feature** | `user_id` | `event_timestamp` | `recent_click_count_7d`, `recent_view_count_7d`, `recent_watch_time_7d`, `recent_like_count_7d`, `historical_category_affinity`, `total_event_count_7d` |
| **video_feature** | `video_id` | `event_timestamp` | `category_id`, `duration_sec`, `view_count`, `like_ratio`, `comment_ratio`, `days_since_upload`, `channel_subscriber_count`, `channel_view_count`, `channel_video_count` |
| **user_category_similarity** | `user_id`, `category_id` | `event_timestamp` | `topic_similarity`, `topic_similarity_top_topic` |

---

<a id="feast-feature-columns"></a>

## 🔸 Feast Feature Columns

- 아래 컬럼만 Feast feature로 노출한다.
- Entity column과 timestamp column은 Feature View 정의에 필요하지만 feature column으로는 등록하지 않는다.

| user_static_feature | user_dynamic_feature | video_feature | user_category_similarity |
| --- | --- | --- | --- |
| age_group | recent_click_count_7d | category_id | topic_similarity |
| occupation | recent_view_count_7d | duration_sec | topic_similarity_top_topic |
| preferred_category | recent_watch_time_7d | view_count |  |
| preferred_topics | recent_like_count_7d | like_ratio |  |
| watch_time_band | historical_category_affinity | comment_ratio |  |
|  | total_event_count_7d | days_since_upload |  |
|  |  | channel_subscriber_count |  |
|  |  | channel_view_count |  |
|  |  | channel_video_count |  |

- `category_id`는 `VideoFeatureView`에서는 영상 카테고리를 나타내는 feature column으로 사용하고, `UserCategorySimilarityView`에서는 `topic_similarity` 조회를 위한 entity key로 사용한다
- `topic_similarity_top_topic`은 분석/디버깅 목적의 feature이며, 모델 입력에는 사용하지 않고 similarity 계산 결과를 해석하거나 디버깅하기 위한 분석용 feature로 등록한다.
- online serving 최적화 단계에서는 Redis/Valkey materialization 대상에서 제외할 수 있다.

---

<a id="entity-timestamp-feature-column"></a>

## 🔸 Entity / Timestamp / Feature Column 구분

Feature View source table에는 entity column과 timestamp column이 반드시 포함되어야 한다.

다만 이 컬럼들은 Feast feature column이나 model input feature로 사용하지 않는다.

| 구분 | 역할 | 예시 |
| --- | --- | --- |
| Entity column | feature lookup key | `user_id`, `video_id`, `category_id` |
| Timestamp column | point-in-time join 기준 시각 | `event_timestamp` |
| Feature column | 모델이 사용할 feature value | `age_group`, `view_count`, `topic_similarity` |

---

<a id="feature-store-exclusions"></a>

## 🔸 Feature Store에 등록하지 않는 대상

| 대상 | 이유 |
| --- | --- |
| raw `action_log` | label 생성과 dynamic feature 생성의 원본. 최종 모델 입력 피처 테이블이 아님 |
| raw `youtube_trending_kr` 전체 | 모델용으로 가공되지 않은 raw metadata가 많음 |
| raw `virtual_user` 전체 | lineage, 생성 메타데이터, raw JSON, 비정형 텍스트가 섞여 있음 |
| `training_entity` | feature lookup 기준 dataframe이지 online serving 피처가 아님 |
| `training_dataset` | 모델 학습용 결과물이며 Feature Store source가 아님 |
| `preferred_category_match`, `historical_category_match` | user/video feature join 후 계산하는 lightweight derived interaction feature |
| `schema_version`, `prompt_version`, `llm_model`, `generated_at` | 생성 메타데이터. 품질 추적용이지 모델 입력이 아님 |
| `created_at`, `feature_created_at`, `label_window_sec` | 데이터셋/파이프라인 metadata로 관리 |

---

<a id="derived-interaction-feature"></a>

## 🔸 Derived / Interaction Feature 처리 기준

| Feature | 저장 위치 | Feature Store 등록 여부 | 모델 입력 여부 | 설명 |
| --- | --- | --- | --- | --- |
| `preferred_category_match` | `training_dataset` 생성 시 계산 | 등록하지 않음 | 사용 | `preferred_category`와 `category_id`를 비교해 계산 |
| `historical_category_match` | `training_dataset` 생성 시 계산 | 등록하지 않음 | 사용 | `historical_category_affinity`와 `category_id`를 비교해 계산 |
| `topic_similarity` | `user_category_similarity` | `UserCategorySimilarityView`로 등록 | 사용 | embedding 기반 cosine similarity를 offline/batch에서 미리 계산한 scalar feature |
| `topic_similarity_top_topic` | `user_category_similarity` | `UserCategorySimilarityView`로 등록 | 사용하지 않음 | similarity 계산 결과 해석/디버깅용 |

<a id="summary"></a>

### 🔸 정리

`preferred_category_match`, `historical_category_match`는 user/video feature를 join한 뒤 계산하는 lightweight derived feature이므로 Feature Store에 저장하지 않는다.

반면 `topic_similarity`는 embedding vector 계산을 포함하는 고비용 feature이므로 online serving 시점에 계산하지 않는다.

따라서 offline/batch 단계에서 `user_topic_embedding`과 `category_embedding`을 이용해 `user_category_similarity`를 미리 만들고, `topic_similarity`를 `UserCategorySimilarityView`로 Feature Store에 등록한다.

- 주의
    - `UserCategorySimilarityView`는 entity가 (`user_id`, `category_id`)라서, serving이나 training dataset 생성 시에 **video feature에서 먼저 `category_id`를 알아야 조회 가능함**
        
        ```sql
        video_id → VideoFeatureView에서 category_id 조회
        user_id + category_id → UserCategorySimilarityView에서 topic_similarity 조회
        ```
        
    - Feast historical retrieval을 한 번에 처리하기 어렵다면, MVP에서는 BigQuery fallback처럼 `video_feature`를 먼저 붙인 뒤 `user_category_similarity`를 join하는 방식
