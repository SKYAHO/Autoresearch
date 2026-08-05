# Training Dataset (model Input)

## Overview

> [!NOTE]
> `training_dataset`은 `training_entity`에 Feature Store에서 조회한 user/video feature를 붙이고, **interaction feature를 계산**한 최종 모델 학습 데이터셋이다.
>
> CTR 모델은 이 데이터셋을 입력으로 사용해 특정 `user_id`가 특정 `video_id`를 노출받았을 때 클릭할 확률, 즉 `clicked` label을 예측한다.

### 학습 구간 인자 계약 (`events_start_date` / `events_end_date`)

`build_training_dataset.main(events_source="bigquery", ...)`의 두 날짜 인자는
단일 의미를 갖는다 (#286): **"이벤트 발생 KST 캘린더 날짜의 폐구간
[start, end]"**. 구현은 이 의미를 두 단계로 나눠 수행한다.

- **dt 파티션 프루닝** (`padded_dt_range`): `[start − 7일(룩백), end + 세션
  padding(일 단위 올림)]` 범위의 당일 슬라이스 파티션(#295 계약)을 읽는다.
  오른쪽 padding은 KST 자정 직전 impression의 click→view→like 세션이
  dt=end+1 파티션에 실리는 경우의 attribution을 위해 필요하다.
- **최종 트림** (`events_kst_window`): 출력 행을 UTC 저장 timestamp 기준
  `[start 00:00 KST, end+1 00:00 KST)`로 자른다. 과거 구현은 UTC 자정
  경계(end 미포함)를 사용해 KST 가장자리 9시간이 어긋났다 — 회귀 테스트가
  현 경계를 고정한다.

### Canonical model input contract

모델 입력의 SSOT는 [`src/features/model_contract.py`](../../src/features/model_contract.py) 하나다. 아래 목록은 그 계약의 문서상 mirror이며, serving, training, simulation/daily scoring이 각자 feature 목록을 정의하거나 보정하지 않는다.

canonical model input은 다음 정확히 21개이며, 나열된 순서도 계약이다.

```text
age_group
occupation
watch_time_band
recent_click_count_7d
recent_view_count_7d
recent_watch_time_7d
recent_like_count_7d
historical_category_affinity
total_event_count_7d
category_id
duration_sec
view_count
like_ratio
comment_ratio
days_since_upload
channel_subscriber_count
channel_view_count
channel_video_count
topic_similarity
preferred_category_match
historical_category_match
```

이 중 categorical feature는 정확히 5개인 `age_group`, `occupation`, `watch_time_band`, `historical_category_affinity`, `category_id`다. `clicked`는 model feature가 아니라 label이며, `build_training_dataset.py`가 생성하는 `training_dataset.csv`의 22번째 물리 컬럼이다. 따라서 물리 학습 산출물은 21개 input feature + `clicked` label = 총 22컬럼이다.

### 필요한 입력 테이블 / 산출물

| 구분 | 이름 | 역할 |
| --- | --- | --- |
| Entity dataframe | `training_entity` | 학습 row 기준. `user_id`, `video_id`, `event_timestamp`, `clicked` 포함 |
| Feature View | `UserStaticView` | 유저 정적 피처 조회 |
| Feature View | `UserDynamicView` | 유저 행동 기반 동적 피처 조회 |
| Feature View | `VideoFeatureView` | 영상 피처 조회 |
| Feature View | `UserCategorySimilarityView` | 유저-카테고리별 `topic_similarity` 조회 |
| Artifact table | `user_topic_embedding` | 사용자 관심 키워드별 embedding 저장 |
| Reference table | `category_embedding` | 카테고리 설명문과 카테고리 embedding 저장 |
| Output table | `training_dataset` | 최종 모델 학습 데이터셋 |

### 데이터셋 생성 흐름

1. `training_entity`를 생성한다.
    - 기준 이벤트: `event_type = 'impression'`
    - positive 이벤트: `event_type = 'click'`
    - label: impression 이후 30분 이내 click 발생 여부
2. Feast `get_historical_features()`로 아래 Feature View를 point-in-time join한다.
    - `UserStaticView`
    - `UserDynamicView`
    - `VideoFeatureView`
3. `VideoFeatureView`에서 얻은 `category_id`를 사용해 `user_id` + `category_id` 기준으로 `UserCategorySimilarityView`를 조회하고, `topic_similarity`를 붙인다.
4. Join된 user/video/similarity feature를 기반으로 interaction feature를 계산한다.
    - `preferred_category_match`
    - `historical_category_match`
5. 최종 `training_dataset`을 parquet 또는 BigQuery table로 저장한다.
6. 모델 학습 후 아래 정보를 MLflow 또는 실험 metadata에 기록한다.
    - `dataset_id`
    - feature set
    - embedding model
    - user topic embedding version
    - category embedding version
    - similarity method
    - metric
    - model artifact path
    - training config

---

## 💠 Training/Input/Target 컬럼


### 📊 Training Dataset Columns

아래 표는 point-in-time feature assembly에 사용되는 논리 컬럼과 보조 컬럼을 함께 설명한다. 최종 모델 입력은 위 canonical contract에서 명시적으로 선택하며, `training_dataset.csv`의 physical output contract는 21개 input과 마지막 `clicked` label의 22컬럼이다.

| 컬럼 | 타입 | 출처 | 설명 |
| --- | --- | --- | --- |
| `dataset_id` | STRING | `training_entity` | 데이터셋/실험 버전 |
| `user_id` | STRING | `training_entity` | 유저 키 |
| `video_id` | STRING | `training_entity` | 영상 키 |
| `event_timestamp` | TIMESTAMP | `training_entity` | 학습 기준 시점 |
| `age_group` | STRING | `UserStaticView` | 연령대 |
| `occupation` | STRING | `UserStaticView` | 직업 |
| `preferred_category` | ARRAY<STRING> | `UserStaticView` | persona 기반 선호 카테고리. `preferred_category_match` 계산용 |
| `preferred_topics` | ARRAY<STRING> | `UserStaticView` | 사용자 관심 키워드. embedding artifact 생성과 디버깅에 사용 |
| `watch_time_band` | STRING | `UserStaticView` | 시청 시간대 성향. `morning`, `evening`, `night`, `unknown` |
| `recent_click_count_7d` | INT64 | `UserDynamicView` | 최근 7일 클릭 수 |
| `recent_view_count_7d` | INT64 | `UserDynamicView` | 최근 7일 view 수 |
| `recent_watch_time_7d` | INT64 | `UserDynamicView` | 최근 7일 총 시청 시간 |
| `recent_like_count_7d` | INT64 | `UserDynamicView` | 최근 7일 좋아요 수 |
| `historical_category_affinity` | STRING | `UserDynamicView` | 과거 행동 기반 선호 카테고리 |
| `total_event_count_7d` | INT64 | `UserDynamicView` | 최근 7일 전체 이벤트 수 |
| `category_id` | STRING | `VideoFeatureView` | 영상 카테고리 |
| `duration_sec` | INT64 | `VideoFeatureView` | 영상 길이 |
| `view_count` | INT64 | `VideoFeatureView` | 영상 조회수 |
| `like_ratio` | FLOAT64 | `VideoFeatureView` | 좋아요 비율 |
| `comment_ratio` | FLOAT64 | `VideoFeatureView` | 댓글 비율 |
| `days_since_upload` | INT64 | `VideoFeatureView` | 업로드 후 경과일. 수집 시점 기준 |
| `channel_subscriber_count` | INT64 | `VideoFeatureView` | 채널 구독자 수 |
| `channel_view_count` | INT64 | `VideoFeatureView` | 채널 누적 조회수 |
| `channel_video_count` | INT64 | `VideoFeatureView` | 채널 영상 수 |
| `topic_similarity` | FLOAT64 | `UserCategorySimilarityView` | 유저 topic embedding과 category embedding 기반 max cosine similarity |
| `topic_similarity_top_topic` | STRING | `UserCategorySimilarityView` | `topic_similarity` 계산 시 가장 높은 유사도를 낸 사용자 topic. 디버깅/분석용 |
| `preferred_category_match` | INT64 | Derived | `category_id`가 `preferred_category`에 포함되면 1, 아니면 0 |
| `historical_category_match` | INT64 | Derived | `historical_category_affinity = category_id`이면 1. `unknown`이면 0 |
| `clicked` | INT64 | `training_entity` | impression 이후 30분 이내 click 발생 여부 |

#### Category Key 정합성 기준

`category_id`는 모든 테이블에서 동일한 canonical category key를 사용한다.

현재 구현에서는 `youtube_trending_kr.video_category` 값을 표준 category key로 사용한다.

따라서 아래 컬럼들은 모두 같은 값 체계를 따라야 한다.

- `video_feature.category_id`
- `user_static_feature.preferred_category`
- `user_dynamic_feature.historical_category_affinity`
- `category_embedding.category_id`
- `user_category_similarity.category_id`

이 값 체계가 어긋나면 `preferred_category_match`, `historical_category_match`, `topic_similarity`가 잘못 계산될 수 있다.


### 📊 Model Input Columns

아래 컬럼만 모델 학습 입력으로 사용한다. 이 표는 `src/features/model_contract.py`의 canonical 21개 순서를 그대로 반영하며, 별도의 feature list를 정의하지 않는다.

| 컬럼 | 타입 |
| --- | --- |
| `age_group` | categorical |
| `occupation` | categorical |
| `watch_time_band` | categorical |
| `recent_click_count_7d` | numeric |
| `recent_view_count_7d` | numeric |
| `recent_watch_time_7d` | numeric |
| `recent_like_count_7d` | numeric |
| `historical_category_affinity` | categorical |
| `total_event_count_7d` | numeric |
| `category_id` | categorical |
| `duration_sec` | numeric |
| `view_count` | numeric |
| `like_ratio` | numeric |
| `comment_ratio` | numeric |
| `days_since_upload` | numeric |
| `channel_subscriber_count` | numeric |
| `channel_view_count` | numeric |
| `channel_video_count` | numeric |
| `topic_similarity` | numeric |
| `preferred_category_match` | binary |
| `historical_category_match` | binary |

#### 🔸 Training Dataset에는 있지만 Model Input에서는 제외하는 Columns

아래 컬럼은 `training_dataset`에는 보관하지만 모델 입력에서는 제외한다.

| 컬럼 | 제외 이유 |
| --- | --- |
| `dataset_id` | 데이터셋/실험 버전 추적용 |
| `user_id` | Entity key. 모델이 특정 유저 ID를 외우는 방향으로 학습할 수 있으므로 제외 |
| `video_id` | Entity key. raw ID를 모델 입력으로 사용하지 않음 |
| `event_timestamp` | point-in-time join 기준 시각. 모델 입력 피처가 아님 |
| `preferred_category` | array feature. 직접 입력하지 않고 `preferred_category_match` 계산에 사용 |
| `preferred_topics` | array feature. 직접 입력하지 않고 embedding artifact 생성과 similarity 분석에 사용 |
| `topic_similarity_top_topic` | similarity 계산 디버깅/분석용 |
| `source_event_id` | 원본 impression event 추적용. `training_entity`에는 존재하지만 모델 입력에서는 제외 |
| `user_topic_embedding.topic_embedding` | vector artifact. 모델 직접 입력이 아니라 `topic_similarity` 계산용 |
| `category_embedding.category_embedding` | vector artifact. 모델 직접 입력이 아니라 `topic_similarity` 계산용 |

<a id="target-column"></a>

### 📊 Target Column

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `clicked` | binary | impression 이후 30분 이내 click 발생 여부 |

---


## 💠 Interaction Feature 계산 규칙

### preferred_category_match

| 항목 | 내용 |
| --- | --- |
| 입력 | `preferred_category`, `category_id` |
| 계산 규칙 | `category_id IN UNNEST(preferred_category)`이면 1 |
| default | `preferred_category`가 null 또는 empty이면 0 |
| 타입 | INT64 |
| 모델 입력 여부 | 사용 |

```sql
IF(
  category_id IN UNNEST(COALESCE(preferred_category, ARRAY<STRING>[])),
  1,
  0
)
```

사용자의 persona 기반 선호 카테고리와 영상 카테고리가 일치하는지 여부

### historical_category_match

| 항목 | 내용 |
| --- | --- |
| 입력 | `historical_category_affinity`, `category_id` |
| 계산 규칙 | `historical_category_affinity = category_id`이면 1 |
| default | `historical_category_affinity = 'unknown'`이면 0 |
| 타입 | INT64 |
| 모델 입력 여부 | 사용 |

```sql
IF(
  COALESCE(historical_category_affinity, 'unknown') != 'unknown'
  AND historical_category_affinity = category_id,
  1,
  0
)
```

사용자의 과거 행동 기반 선호 카테고리와 영상 카테고리가 일치하는지 여부

### topic_similarity

`topic_similarity`는 `training_dataset` 생성 시점에 직접 vector cosine을 계산하지 않고, offline/batch 단계에서 미리 계산된 `user_category_similarity`에서 조회한다.

| 항목 | 내용 |
| --- | --- |
| 입력 | `user_id`, `category_id`, `event_timestamp` |
| Source | `UserCategorySimilarityView` 또는 `user_category_similarity` |
| 계산 방식 | user topic embedding과 category embedding 간 cosine similarity |
| pooling 방식 | max-pool |
| default | matching row가 없으면 0.0 |
| 타입 | FLOAT64 |
| 모델 입력 여부 | 사용 |

> [!WARNING]
> **주의**
>
> - `user_category_similarity`는 `user_topic_embedding`과 `category_embedding`을 기반으로 offline/batch에서 생성된다.
> - 단, 사용자 topic embedding이 없거나 해당 `user_id` + `category_id` 조합의 similarity row가 없는 경우가 있을 수 있다.
> - 이 경우 `training_dataset` 생성 시 `topic_similarity = 0.0`, `topic_similarity_top_topic = 'unknown'`으로 default 처리한

사용자 관심 키워드와 영상 카테고리의 의미적 유사도

---

## 💠 MVP용 BigQuery Join Fallback

Feast historical retrieval이 아직 완성되지 않았을 때, MVP에서는 BigQuery에서 직접 as-of join에 가까운 방식으로 `training_dataset`을 만들 수 있다.

최종 구조에서는 Feast `get_historical_features()`를 사용해 동일한 point-in-time join을 수행하는 것이 목표다.

> **구현 현황(#214)**: 아래 `user_category_similarity_joined` CTE와 동일한 as-of 패턴이 `build_training_dataset.py`에 `topic_similarity_source="bigquery"`(CLI `--topic-similarity-source bigquery`)로 구현되어 있다 — `topic_similarity` 컬럼만 이 경로를 탄다. 나머지 `user_static_feature`/`user_dynamic_feature`/`video_feature` 조인은 여전히 in-process DuckDB 계산(`src/features/assembly.py`)이며, `get_historical_features()` 자체는 아직 경유하지 않는다.

```sql
CREATE OR REPLACE TABLE `{project}.{dataset}.training_dataset` AS
WITH entity AS (
  SELECT *
  FROM `{project}.{dataset}.training_entity`
),

user_static_joined AS (
  SELECT
    e.dataset_id,
    e.user_id,
    e.video_id,
    e.event_timestamp,
    s.age_group,
    s.occupation,
    s.preferred_category,
    s.preferred_topics,
    s.watch_time_band,
    ROW_NUMBER() OVER (
      PARTITION BY e.dataset_id, e.user_id, e.video_id, e.event_timestamp
      ORDER BY s.event_timestamp DESC
    ) AS rn
  FROM entity e
  LEFT JOIN `{project}.{dataset}.user_static_feature` s
    ON e.user_id = s.user_id
   AND s.event_timestamp <= e.event_timestamp
),

user_dynamic_joined AS (
  SELECT
    e.dataset_id,
    e.user_id,
    e.video_id,
    e.event_timestamp,
    d.recent_click_count_7d,
    d.recent_view_count_7d,
    d.recent_watch_time_7d,
    d.recent_like_count_7d,
    d.historical_category_affinity,
    d.total_event_count_7d,
    ROW_NUMBER() OVER (
      PARTITION BY e.dataset_id, e.user_id, e.video_id, e.event_timestamp
      ORDER BY d.event_timestamp DESC
    ) AS rn
  FROM entity e
  LEFT JOIN `{project}.{dataset}.user_dynamic_feature` d
    ON e.user_id = d.user_id
   AND d.event_timestamp <= e.event_timestamp
),

video_joined AS (
  SELECT
    e.dataset_id,
    e.user_id,
    e.video_id,
    e.event_timestamp,
    v.category_id,
    v.duration_sec,
    v.view_count,
    v.like_ratio,
    v.comment_ratio,
    v.days_since_upload,
    v.channel_subscriber_count,
    v.channel_view_count,
    v.channel_video_count,
    ROW_NUMBER() OVER (
      PARTITION BY e.dataset_id, e.user_id, e.video_id, e.event_timestamp
      ORDER BY v.event_timestamp DESC
    ) AS rn
  FROM entity e
  LEFT JOIN `{project}.{dataset}.video_feature` v
    ON e.video_id = v.video_id
   AND v.event_timestamp <= e.event_timestamp
),

user_category_similarity_joined AS (
  SELECT
    e.dataset_id,
    e.user_id,
    e.video_id,
    e.event_timestamp,
    ucs.topic_similarity,
    ucs.topic_similarity_top_topic,
    ROW_NUMBER() OVER (
      PARTITION BY e.dataset_id, e.user_id, e.video_id, e.event_timestamp
      ORDER BY ucs.event_timestamp DESC
    ) AS rn
  FROM entity e
  LEFT JOIN video_joined v
    ON e.dataset_id = v.dataset_id
   AND e.user_id = v.user_id
   AND e.video_id = v.video_id
   AND e.event_timestamp = v.event_timestamp
   AND v.rn = 1
  LEFT JOIN `{project}.{dataset}.user_category_similarity` ucs
    ON e.user_id = ucs.user_id
   AND v.category_id = ucs.category_id
   AND ucs.event_timestamp <= e.event_timestamp
),

user_static_best AS (
  SELECT *
  FROM user_static_joined
  WHERE rn = 1
),

user_dynamic_best AS (
  SELECT *
  FROM user_dynamic_joined
  WHERE rn = 1
),

video_best AS (
  SELECT *
  FROM video_joined
  WHERE rn = 1
),

user_category_similarity_best AS (
  SELECT *
  FROM user_category_similarity_joined
  WHERE rn = 1
)

SELECT
  e.dataset_id,
  e.user_id,
  e.video_id,
  e.event_timestamp,

  COALESCE(s.age_group, 'unknown') AS age_group,
  COALESCE(s.occupation, 'unknown') AS occupation,
  COALESCE(s.preferred_category, ARRAY<STRING>[]) AS preferred_category,
  COALESCE(s.preferred_topics, ARRAY<STRING>[]) AS preferred_topics,
  COALESCE(s.watch_time_band, 'unknown') AS watch_time_band,

  COALESCE(d.recent_click_count_7d, 0) AS recent_click_count_7d,
  COALESCE(d.recent_view_count_7d, 0) AS recent_view_count_7d,
  COALESCE(d.recent_watch_time_7d, 0) AS recent_watch_time_7d,
  COALESCE(d.recent_like_count_7d, 0) AS recent_like_count_7d,
  COALESCE(d.historical_category_affinity, 'unknown') AS historical_category_affinity,
  COALESCE(d.total_event_count_7d, 0) AS total_event_count_7d,

  COALESCE(v.category_id, 'unknown') AS category_id,
  COALESCE(v.duration_sec, 0) AS duration_sec,
  COALESCE(v.view_count, 0) AS view_count,
  COALESCE(v.like_ratio, 0.0) AS like_ratio,
  COALESCE(v.comment_ratio, 0.0) AS comment_ratio,
  COALESCE(v.days_since_upload, 0) AS days_since_upload,
  COALESCE(v.channel_subscriber_count, 0) AS channel_subscriber_count,
  COALESCE(v.channel_view_count, 0) AS channel_view_count,
  COALESCE(v.channel_video_count, 0) AS channel_video_count,

  COALESCE(ucs.topic_similarity, 0.0) AS topic_similarity,
  COALESCE(ucs.topic_similarity_top_topic, 'unknown') AS topic_similarity_top_topic,

  IF(
    COALESCE(v.category_id, 'unknown') IN UNNEST(COALESCE(s.preferred_category, ARRAY<STRING>[])),
    1,
    0
  ) AS preferred_category_match,

  IF(
    COALESCE(d.historical_category_affinity, 'unknown') != 'unknown'
    AND d.historical_category_affinity = v.category_id,
    1,
    0
  ) AS historical_category_match,

  e.clicked
FROM entity e
LEFT JOIN user_static_best s
  ON e.dataset_id = s.dataset_id
 AND e.user_id = s.user_id
 AND e.video_id = s.video_id
 AND e.event_timestamp = s.event_timestamp
LEFT JOIN user_dynamic_best d
  ON e.dataset_id = d.dataset_id
 AND e.user_id = d.user_id
 AND e.video_id = d.video_id
 AND e.event_timestamp = d.event_timestamp
LEFT JOIN video_best v
  ON e.dataset_id = v.dataset_id
 AND e.user_id = v.user_id
 AND e.video_id = v.video_id
 AND e.event_timestamp = v.event_timestamp
LEFT JOIN user_category_similarity_best ucs
  ON e.dataset_id = ucs.dataset_id
 AND e.user_id = ucs.user_id
 AND e.video_id = ucs.video_id
 AND e.event_timestamp = ucs.event_timestamp;
```

---

<a id="training-dataset-metadata"></a>

## 💠 training_dataset_metadata

`training_dataset_metadata`는 데이터셋 전체에 공통으로 적용되는 설정과 생성 정보를 저장한다.

row마다 반복되는 메타정보는 `training_entity`나 `training_dataset` 컬럼으로 넣지 않고, 별도 metadata table 또는 config 파일로 관리한다.

### Columns

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `dataset_id` | STRING | 데이터셋/실험 버전 ID |
| `created_at` | TIMESTAMP | 데이터셋 생성 시각 |
| `label_base_event` | STRING | label 기준 이벤트. `impression` |
| `positive_event` | STRING | positive label 이벤트. `click` |
| `label_window_sec` | INT64 | impression 이후 click 판단 window. 기본값 1800초 |
| `source_action_log` | STRING | action log source table |
| `source_user_table` | STRING | virtual user source table |
| `source_video_table` | STRING | video source table |
| `feature_tables` | ARRAY<STRING> | 사용한 feature source table 목록 |
| `feature_views` | ARRAY<STRING> | 사용한 Feature View 목록 |
| `artifact_tables` | ARRAY<STRING> | 사용한 artifact/reference table 목록 |
| `feature_set_version` | STRING | feature 정의 버전 |
| `join_strategy` | STRING | feature join 방식. 예: `feast_historical_retrieval`, `bigquery_asof_fallback` |
| `snapshot_granularity` | STRING | dynamic feature snapshot 주기. 예: `daily` |
| `timezone` | STRING | snapshot/date 기준 timezone. 예: `Asia/Seoul` |
| `embedding_model` | STRING | embedding 생성 모델 |
| `embedding_dim` | INT64 | embedding 차원 |
| `user_topic_embedding_version` | STRING | user topic embedding 버전 |
| `category_embedding_version` | STRING | category embedding 버전 |
| `similarity_method` | STRING | similarity 계산 방식. 예: `cosine` |
| `similarity_pooling` | STRING | pooling 방식. 예: `max` |
| `notes` | STRING | 데이터셋 설명 |

---

### SQL

```sql
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.training_dataset_metadata` (
  dataset_id STRING,
  created_at TIMESTAMP,
  label_base_event STRING,
  positive_event STRING,
  label_window_sec INT64,
  source_action_log STRING,
  source_user_table STRING,
  source_video_table STRING,
  feature_tables ARRAY<STRING>,
  feature_views ARRAY<STRING>,
  artifact_tables ARRAY<STRING>,
  feature_set_version STRING,
  join_strategy STRING,
  snapshot_granularity STRING,
  timezone STRING,
  embedding_model STRING,
  embedding_dim INT64,
  user_topic_embedding_version STRING,
  category_embedding_version STRING,
  similarity_method STRING,
  similarity_pooling STRING,
  notes STRING
);

INSERT INTO `{project}.{dataset}.training_dataset_metadata`
SELECT
  'ctr_train_full_v1' AS dataset_id,
  CURRENT_TIMESTAMP() AS created_at,
  'impression' AS label_base_event,
  'click' AS positive_event,
  1800 AS label_window_sec,
  'data_lake/action_log' AS source_action_log,
  'asset/virtual_user/vu_1000' AS source_user_table,
  'data_lake/youtube_trending_kr' AS source_video_table,
  ['user_static_feature', 'user_dynamic_feature', 'video_feature', 'user_category_similarity'] AS feature_tables,
  ['UserStaticView', 'UserDynamicView', 'VideoFeatureView', 'UserCategorySimilarityView'] AS feature_views,
  ['user_topic_embedding', 'category_embedding'] AS artifact_tables,
  'full_v1' AS feature_set_version,
  'bigquery_asof_fallback' AS join_strategy,
  'daily' AS snapshot_granularity,
  'Asia/Seoul' AS timezone,
  'text-multilingual-embedding-002' AS embedding_model,
  768 AS embedding_dim,
  'user_topic_embedding_v1' AS user_topic_embedding_version,
  'category_embedding_v1' AS category_embedding_version,
  'cosine' AS similarity_method,
  'max' AS similarity_pooling,
  'CTR full v1 dataset with category similarity precomputed in user_category_similarity.' AS notes;
```

---

<a id="validation-checklist"></a>

### 검증 항목

별도 검증 쿼리 페이지에서 아래 항목을 확인한다.

1. `training_entity` row 수와 `training_dataset` row 수가 같은지 확인한다.
2. `clicked` label 비율을 확인한다.
3. user feature join 후 null/default 비율을 확인한다.
4. video feature join 후 null/default 비율을 확인한다.
5. `preferred_category_match`, `historical_category_match`의 0/1 분포를 확인한다.
6. `topic_similarity`의 null 비율, 평균, 최소값, 최대값을 확인한다.
7. `topic_similarity_top_topic`이 정상적으로 생성되는지 확인한다.
8. `user_topic_embedding`과 `category_embedding`의 embedding dimension이 같은지 확인한다.
9. `user_category_similarity`의 `(user_id, category_id, event_timestamp)` 중복 여부를 확인한다.
10. `dataset_id` 기준으로 metadata가 정상 기록됐는지 확인한다.

---


### 구현 원칙

- 모델에는 raw list/vector를 직접 입력하지 않는다.
- `preferred_category`는 `preferred_category_match` 계산에 사용한다.
- `preferred_topics`는 `user_topic_embedding` 생성과 similarity 분석에 사용한다.
- `user_topic_embedding`과 `category_embedding`은 모델 입력이 아니라 `user_category_similarity`를 만들기 위한 artifact table이다.
- `user_category_similarity.topic_similarity`는 최종 모델 입력 feature로 사용한다.
- 최종 모델 입력에는 scalar/categorical/numeric/binary feature만 포함한다.
- online serving 시점에는 LLM 호출, embedding 생성, 긴 text parsing, vector cosine 계산을 수행하지 않는다.
- serving에서는 미리 계산된 `user_category_similarity`를 조회해 `topic_similarity`를 사용한다.

---

## 💠 스냅샷 게시와 재사용

> [!NOTE]
> #530 이전에는 `training_dataset.csv`가 실행이 끝난 로컬 디스크에만 남아,
> "학습 데이터셋이 지금 어디에 있는가"라는 질문에 코드를 읽지 않고 답할 방법이
> 없었다. 이 절이 그 답이다.

### 저장 위치

`build-features`(`build_training_dataset.main`)는 조립이 끝나면
`training_dataset.csv`와 그 sidecar `training_dataset.csv.snapshot.json`
(`TrainingSnapshotManifest` — `dataset_sha256`·`schema_sha256`·`row_count`·
`events_start_date`/`events_end_date`·`feature_service`·`registry_uri`/
`registry_generation`/`registry_sha256`·`spine_usable_days` 포함)을 로컬에
durable하게 쓴다. 이 시점부터 같은 데이터가 최대 세 곳에 존재할 수 있다.

1. **로컬 경로** — `--output-path`(기본 `data/processed/training_dataset.csv`)와
   그 옆의 `.snapshot.json` sidecar.
2. **MLflow artifact** — `train-model`/`run-pipeline`이 학습을 마치면, 검증에
   실제로 쓰인 학습 입력을 해당 run의 `reproducibility/snapshot/` 아래
   canonical 이름(`training_dataset.csv`, `snapshot_manifest.json`)으로
   그대로 기록한다(`src/pipeline/train.py::_log_reproducibility_artifacts`).
   run_id를 아는 경우 MLflow UI/API의 Artifacts에서 바로 내려받을 수 있다.
3. **게시된 GCS 스냅샷** — `--snapshot-root`(또는 환경변수
   `TRAINING_SNAPSHOT_ROOT`)를 주면 content-addressed 주소
   `gs://<root>/by-hash/<dataset_sha256>/training_dataset.csv`와 같은 prefix의
   `snapshot_manifest.json`으로 write-once 게시한다
   (`src/pipeline/training_snapshot_store.py::publish_snapshot`). 주소가 CSV
   내용의 SHA-256이므로 같은 내용을 다시 게시해도 새 객체가 생기지 않고
   (`if_generation_match=0`이 이미 존재함을 뜻하는 412를 no-op으로 흡수한다),
   한 번 게시된 객체는 이후 절대 변경되지 않는다.

`--snapshot-root`도 `TRAINING_SNAPSHOT_ROOT`도 주지 않으면(대부분의 실험·dev
실행) 게시 없이 로컬 경로만 남는다 — `build_training_dataset.main`은
`snapshot_root` 인자를 받지 않는 한 게시 경로에 들어서지 않는다.

> [!WARNING]
> `TRAINING_SNAPSHOT_ROOT`/`--snapshot-root`는 **prod 재학습 경로에만**
> 설정한다. 실험·dev 파이프라인이 이 값을 켜면 아래 by-date 포인터가 서로
> 경합해 prod 포인터를 오염시킬 수 있다(#530). 환경변수는 `src/cli.py`에서만
> 해석하고 `build_training_dataset.main` 내부에서는 읽지 않는다 —
> `src/pipeline/degradation_eval.py`의 평가일 순회가 `main()`을 평가일 수만큼
> 반복 호출하므로, `main()`이 환경변수를 직접 읽으면 그 반복이 by-date
> 포인터를 평가일마다 덮어쓰게 된다. 실제로 `degradation_eval.py`는
> `snapshot_root`를 넘기지 않는다.

### 날짜·FeatureService로 찾기 (by-date 포인터)

기본 조건(FeatureService `ctr_training_v1`, `--extra-features` 미지정)으로
게시하면 by-hash 객체와 별도로 아래 위치의 포인터도 함께 갱신된다.

```text
gs://<root>/by-date/dt=<events_end_date>/<feature_service>.json
```

- `dt`는 게시 시각이 아니라 그 스냅샷의 **`events_end_date`**(학습 구간
  종료일)다.
- 내용은 `TrainingSnapshotPointer` — 현재 `dataset_sha256`·`uri`·
  `events_start_date`/`events_end_date`·`feature_service`·
  `registry_generation`·`published_at`과, 최근 최대 `MAX_POINTER_HISTORY`
  (10)건까지의 이전 스냅샷 이력 `previous`를 담는다.
- 조회 예:

```bash
gsutil cat gs://<bucket>/training-datasets/by-date/dt=2026-08-01/ctr_training_v1.json
```

- 같은 좌표(`events_end_date`, `feature_service`)에 새 스냅샷이 게시되면
  포인터는 **최신으로 이동**한다. 과거 학습이 참조한 `uri`는 by-hash 불변
  주소이므로, 포인터가 이동해도 그 학습의 재현성은 깨지지 않는다 — 재현은
  항상 sha 주소로 하기 때문에 포인터를 움직여도 안전하다.
- **실험 조립**(`--feature-service`가 기본이 아니거나 `--extra-features`를
  지정한 조립)**은 by-hash에만 올라가고 이 포인터는 갱신하지 않는다** —
  실험이 prod 포인터를 오염시키지 못하게 하는 명시적 설계다
  (`training_snapshot_store.publish_snapshot`의 `record_pointer=False`,
  판정은 `build_training_dataset.py::is_experiment_assembly`).

### `--dataset-uri`로 재사용하기

포인터에서 얻은 `uri`(또는 직접 아는 `gs://<root>/by-hash/<sha>/`)를
`train-model`이나 `run-pipeline`에 넘기면 조립을 다시 돌리지 않고 그 스냅샷을
학습 입력으로 쓴다.

```bash
python -m src.cli train-model --dataset-uri gs://<bucket>/training-datasets/by-hash/<sha>/
```

- `train-model --dataset-uri`는 `--data-path`와 함께 줄 수 없다(스냅샷이 이미
  학습 입력을 확정했다).
- `run-pipeline --dataset-uri`는 `--dataset-path`·`--events-start-date`·
  `--events-end-date`와 함께 줄 수 없다(같은 이유로 build-features 단계
  자체를 생략한다).
- `--min-coverage-days`는 이 재사용 경로에만 의미가 있다 — `--dataset-uri`
  없이 `train-model`을 실행하면 값을 줘도 아무 영향이 없다.

재사용 실행은 학습을 시작하기 전에 다음을 재검증하며, 하나라도 어긋나면
학습을 시작하지 않는다.

1. **주소-내용 일치** — URI의 `by-hash/<sha>/` 경로가 내려받은 CSV 옆
   sidecar의 `dataset_sha256`과 같은지
   (`training_snapshot_store.download_snapshot`).
2. **byte/schema/row_count 일치** — 내려받은 CSV의 SHA-256·컬럼 schema
   hash·행 수가 sidecar manifest와 같은지(`load_training_snapshot_manifest`).
3. **커버리지 게이트** — `--min-coverage-days`(미지정 시 조립 경로와 같은
   기본값)를 만족하는 `spine_usable_days`가 manifest에 있는지
   (`require_snapshot_coverage`). 이 필드가 없는 옛 스냅샷은 게이트가 켜진
   채로는 재사용할 수 없다 — `--min-coverage-days 0`으로 명시적으로 우회해야
   한다.

다운로드한 CSV는 임시 디렉터리에 두며, 학습이 끝나든 예외로 중단되든 반드시
정리한다.

---
