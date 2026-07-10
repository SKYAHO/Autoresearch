# Data Warehouse (BigQuery)

> 기준일: 2026-07-10

## Table of Contents

- [현재 구현 상태](#implementation-status)
- [공통 데이터 계약](#common-contracts)
- [user_static_feature](#user_static_feature)
- [user_dynamic_feature](#user_dynamic_feature)
- [video_feature](#video_feature)
- [training_entity](#training_entity)
- [user_topic_embedding](#user_topic_embedding)
- [category_embedding](#category_embedding)
- [user_category_similarity](#user_category_similarity)
- [training_dataset 연계 계약](#training-dataset-contract)

<a id="implementation-status"></a>

## 현재 구현 상태

이 문서는 BigQuery 기반 Data Warehouse의 **목표 스키마와 materialization 설계**를
정의합니다. 문서의 SQL이 저장소에서 이미 실행되고 있다는 의미는 아닙니다.

| 영역 | 상태 | 저장소 기준 |
| --- | --- | --- |
| Feast Entity, BigQuerySource, 4개 Feature View 계약 | **구현됨** | `feature_repo/feature_definitions.py`에 `UserStaticView`, `UserDynamicView`, `VideoFeatureView`, `UserCategorySimilarityView`가 정의되어 있음 |
| `user_static_feature`, `user_dynamic_feature`, `video_feature`, `user_category_similarity` BigQuery materialization | **설계/계획** | 이 문서에 목표 SQL만 있으며 실행 SQL 모듈이나 Airflow DAG는 아직 없음 |
| `training_entity`, `user_topic_embedding`, `category_embedding` 생성 | **설계/계획** | 이 문서에 스키마·생성 규칙이 있으나 실행 파이프라인은 아직 없음 |
| Feast Historical Retrieval 및 BigQuery `training_dataset` 생성 | **설계/계획** | 상세 계약과 BigQuery fallback은 [`training-dataset.md`](./training-dataset.md)에 있으며 운영 DAG는 아직 없음 |

현재 4개 Feature View에는 `ttl`이 설정되어 있지 않습니다. 아래의 7일·30일은
BigQuery source table을 생성할 때 적용하는 집계 window이며 Feast TTL을 뜻하지
않습니다. 운영 전에는 Feature View별 유효 기간과 보존 정책을 별도로 결정해야
합니다.

<a id="common-contracts"></a>

## 공통 데이터 계약

### Category key

`category_id`라는 컬럼명을 사용하지만, 현재 저장소의 canonical category key는
YouTube 숫자 category ID가 아니라 `"Music"`, `"Gaming"` 같은 **카테고리 이름
STRING**입니다. 수집 단계에서 API의 숫자 ID를 이름으로 변환해
`youtube_trending_kr.video_category`에 저장합니다.

Raw `video_category`가 null 또는 빈 문자열이면 warehouse에서 `"unknown"`으로
정규화하며, 빈 문자열을 Feast entity/join key로 사용하지 않습니다.

따라서 다음 컬럼은 모두 동일한 카테고리 이름 값 체계를 사용해야 합니다.

- `user_static_feature.preferred_category`
- `user_dynamic_feature.historical_category_affinity`
- `video_feature.category_id`
- `category_embedding.category_id`
- `user_category_similarity.category_id`

숫자 ID와 이름을 혼용하면 Feast entity join과
`preferred_category_match`·`historical_category_match`·`topic_similarity`가 모두
잘못 계산될 수 있습니다.

### 처리 흐름

> [!NOTE]
> `training_entity`를 기준으로 Feast Historical Retrieval을 수행해 User/Video Feature를 붙이고, Interaction Feature를 계산해 최종 `training_dataset`을 생성
>
> - **Feature Store source table**
>     - **user_static_feature**
>     - **user_dynamic_feature**
>     - **video_feature**
>     - **user_category_similarity**
> - Historical retrieval을 요청할 **기준 dataframe**
>     - **training_entity**
> - `topic_similarity`를 만들기 위한 **Embedding artifact/reference**
>     - **user_topic_embedding**
>     - **category_embedding**

---

## Feature Store

<a id="user_static_feature"></a>

## 📋 user_static_feature

- 유저의 변하지 않는 프로필/페르소나 기반 피처

### 🔸 Source

- Raw table: `asset/virtual_user/vu_1000`
- Feature View: `UserStaticView`
- Entity: `user_id`
- 갱신 주기: persona 생성 시점 또는 batch

### 🔸 Columns

| 중간 테이블 컬럼 | 타입 | 원본 컬럼 | 생성 규칙 | 설명 |
| --- | --- | --- | --- | --- |
| `user_id` | STRING | `user_id` | 그대로 사용 | 유저 키 |
| `event_timestamp` | TIMESTAMP | 고정 기준 시각 | static persona feature가 모든 action log 이전부터 유효하다고 보고<br>TIMESTAMP '1970-01-01 00:00:00 UTC'로 고정한다 | Feast point-in-time join을 위한 timestamp |
| `age_group` | STRING | `age_bucket` | rename | 모델 입력용 연령대 |
| `occupation` | STRING | `occupation` | 그대로 사용. null이면 `"unknown"` | 직업 |
| `preferred_category` | ARRAY<STRING> | `primary_categories` | rename. null이면 빈 배열 `[]` | persona 기반 선호 카테고리 |
| `preferred_topics` | ARRAY<STRING> | `hobby_keywords`, `interest_keywords`, `lifestyle_keywords`, `food_keywords`, `travel_keywords`, `career_keywords`, `family_context_keywords` | 여러 keyword array를 concat. null array는 빈 배열 처리 | topic similarity 계산용 사용자 관심 키워드 |
| `watch_time_band` | STRING | `watch_time_band` | 그대로 사용. null이면 `"unknown"` | 시청 성향 시간대 |

#### 제외하는 Raw columns

| 제외 컬럼 | 제외 이유 |
| --- | --- |
| `source_uuid`, `source_dataset`, `source_hash` | lineage 추적용 메타데이터. 모델 피처 아님 |
| `schema_version`, `prompt_version`, `llm_model`, `generated_at` | 생성 메타데이터. 품질 추적용이지 모델 입력 아님 |
| `marital_status`, `military_status`, `family_type`, `housing_type`, `province`, `district` | 현재 모델 입력 제외, 후속 실험 후보 |
| `age`, `sex`, `education_level`, `bachelors_field`, `country`, `locale` | 현재 모델 입력 제외. 필요하면 별도 feature version의 실험 피처로 추가 |
| `persona_summary` | raw text이므로 모델 직접 입력 X. 추후 embedding 또는 keyword 추출용으로 사용 가능 |
| `source_persona_json` | 원본 페르소나 감사·재현용 payload. 모델에 직접 입력하지 않음 |

#### Feast View에 등록할 Feature columns

| Feature column |
| --- |
| `age_group` |
| `occupation` |
| `preferred_category` |
| `preferred_topics` |
| `watch_time_band` |

> [!WARNING]
> **Static timestamp 가정**
>
> `1970-01-01 00:00:00 UTC`는 persona가 모든 action log보다 먼저 유효했다는
> MVP 가정입니다. 이 방식은 persona 변경 이력을 표현하지 못하므로, 같은
> `user_id`의 persona를 재생성하거나 버전별 point-in-time 조회가 필요해지면
> 실제 생성·유효 시각과 중복 처리 규칙으로 교체해야 합니다.

> [!IMPORTANT]
> **`watch_time_band` vocabulary 정합성**
>
> 현재 virtual user 스키마의 canonical 값인 `morning`, `afternoon`, `evening`,
> `night`, `mixed`를 warehouse에서도 그대로 보존합니다. downstream 모델이 일부
> 값만 사용하려면 원천을 손실 변환하지 말고 별도 feature version에서 명시적으로
> 매핑해야 합니다.

### 🔸 SQL

```sql
CREATE OR REPLACE TABLE `{project}.{dataset}.user_static_feature` AS
SELECT
  user_id,

  -- static persona feature는 action log보다 먼저 존재한다고 보고 고정 valid-from timestamp 사용
  TIMESTAMP '1970-01-01 00:00:00 UTC' AS event_timestamp,

  COALESCE(age_bucket, 'unknown') AS age_group,
  COALESCE(occupation, 'unknown') AS occupation,

  COALESCE(primary_categories, ARRAY<STRING>[]) AS preferred_category,

  ARRAY_CONCAT(
    COALESCE(hobby_keywords, ARRAY<STRING>[]),
    COALESCE(interest_keywords, ARRAY<STRING>[]),
    COALESCE(lifestyle_keywords, ARRAY<STRING>[]),
    COALESCE(food_keywords, ARRAY<STRING>[]),
    COALESCE(travel_keywords, ARRAY<STRING>[]),
    COALESCE(career_keywords, ARRAY<STRING>[]),
    COALESCE(family_context_keywords, ARRAY<STRING>[])
  ) AS preferred_topics,

  CASE
    WHEN LOWER(TRIM(watch_time_band)) IN ('morning', 'am', '오전', '아침') THEN 'morning'
    WHEN LOWER(TRIM(watch_time_band)) IN ('afternoon', '오후') THEN 'afternoon'
    WHEN LOWER(TRIM(watch_time_band)) IN ('evening', 'pm', '저녁') THEN 'evening'
    WHEN LOWER(TRIM(watch_time_band)) IN ('night', 'late_night', '밤', '심야') THEN 'night'
    WHEN LOWER(TRIM(watch_time_band)) IN ('mixed', '혼합') THEN 'mixed'
    ELSE 'unknown'
  END AS watch_time_band

FROM `{project}.{dataset}.asset_virtual_user_vu_1000`
WHERE user_id IS NOT NULL;
```

---

<a id="user_dynamic_feature"></a>

## 📋 user_dynamic_feature

- action log를 기반으로 “최근 유저 행동”을 집계한 피처
- CTR 모델에서 유저의 최근 활동성, 최근 클릭/시청/좋아요 성향, 과거 선호 카테고리를 표현

### 🔸 Source

- Raw table: `data_lake/action_log`
- Join table: `data_lake/youtube_trending_kr`
- Feature View: `UserDynamicView`
- Entity: `user_id`
- 갱신 주기: batch 또는 event-driven

#### 주의

학습 row의 `event_timestamp` 이후 행동은 절대 사용하면 안 된다.

즉, 동적 유저 피처는 반드시 기준 시점 이전의 action log만 사용해 계산해야 한다.

MVP에서는 **일 단위 snapshot(Asia/Seoul 기준 날짜 경계)**을 생성한다.

#### 🔸 Cold-start 처리

일 단위 snapshot 기준 시점 이전에 행동 이력이 없는 유저는 dynamic feature를 default 값으로 채운다.

| Feature | default |
| --- | --- |
| `recent_click_count_7d` | `0` |
| `recent_view_count_7d` | `0` |
| `recent_watch_time_7d` | `0` |
| `recent_like_count_7d` | `0` |
| `historical_category_affinity` | `"unknown"` |
| `total_event_count_7d` | `0` |

MVP의 daily snapshot 방식에서는 impression 당일 00:00 이후부터 impression 시점 이전까지의 행동도 반영하지 않는다.

예를 들어 `2026-07-05 10:00:00`에 발생한 impression은 `2026-07-05 00:00:00` snapshot을 사용한다.  
이 snapshot의 7일 window는 `[2026-06-28 00:00:00, 2026-07-05 00:00:00)`이다.

따라서 `2026-07-05 00:00:00`부터 `2026-07-05 10:00:00` 사이의 행동은 반영되지 않는다.

이는 feature freshness 측면에서는 일부 정보를 덜 사용하는 보수적인 방식이지만, MVP 단계에서는 구현 복잡도를 낮추고 leakage를 방지하기 위해 이 방식을 사용하고 추후 row-level rolling aggregation 또는 더 촘촘한 snapshot 주기로 개선한다.

### 🔸 Columns

| 중간 테이블 컬럼 | 타입 | 원본/조인 컬럼 | 계산 규칙 | cold-start/default |
| --- | --- | --- | --- | --- |
| `user_id` | STRING | `action_log.user_id` | 그대로 사용 | 필수 |
| `event_timestamp` | TIMESTAMP | snapshot 기준 시각 | 피처가 계산된 기준 시각 | 필수 |
| `recent_click_count_7d` | INT64 | `event_type` | 기준 시점 이전 7일 동안 `event_type = 'click'` 개수 | 0 |
| `recent_view_count_7d` | INT64 | `event_type` | 기준 시점 이전 7일 동안 `event_type = 'view'` 개수 | 0 |
| `recent_watch_time_7d` | INT64 | `watch_time_sec` | 기준 시점 이전 7일 동안 `event_type = 'view'`의 `watch_time_sec` 합계 | 0 |
| `recent_like_count_7d` | INT64 | `event_type` | 기준 시점 이전 7일 동안 `event_type = 'like'` 개수 | 0 |
| `historical_category_affinity` | STRING | `action_log.video_id` + `youtube_trending_kr.video_category` | 기준 시점 이전 30일 동안 `click`, `view`, `like`가 가장 많았던 `video_category` | `"unknown"` |
| `total_event_count_7d` | INT64 | `event_type` | 기준 시점 이전 7일 동안 전체 이벤트 수 | 0 |

#### `historical_category_affinity` 계산 규칙

`action_log`에는 `video_category`가 없으므로 `video_id` 기준으로 `youtube_trending_kr`와 join해야 한다.

기본 계산 방식:

1. `action_log`와 `youtube_trending_kr`를 `video_id`로 join한다.
2. 기준 시점 이전 이벤트만 사용한다.
3. `event_type IN ('click', 'view', 'like')`인 이벤트를 반응 이벤트로 본다.
4. 기준 시점 이전 30일 동안 가장 많이 등장한 `video_category`를 유저별로 선택한다.
5. 이력이 없으면 `"unknown"`으로 채운다.

> [!IMPORTANT]
> 이 Data Warehouse 계약에서 `historical_category_affinity`는 **최근 30일의
> `click`·`view`·`like` 반응 이벤트 전체**를 기준으로 합니다. 현재
> `src/pipeline/build_training_dataset.py`의 로컬 CSV prototype은 기간 제한 없이
> 과거 click만 집계하므로 정의가 다릅니다. 해당 prototype은 이 BigQuery
> materialization의 구현으로 간주하지 않으며, 전환 시 이 문서의 정의로
> 통일하거나 feature version을 분리해야 합니다.

#### Feast View에 등록할 Feature columns

| Feature column |
| --- |
| `recent_click_count_7d` |
| `recent_view_count_7d` |
| `recent_watch_time_7d` |
| `recent_like_count_7d` |
| `historical_category_affinity` |
| `total_event_count_7d` |

### 🔸 SQL

```sql
CREATE OR REPLACE TABLE `{project}.{dataset}.user_dynamic_feature` AS
WITH action_log AS (
  SELECT
    user_id,
    video_id,
    event_type,
    event_timestamp,
    COALESCE(watch_time_sec, 0) AS watch_time_sec
  FROM `{project}.{dataset}.data_lake_action_log`
  WHERE user_id IS NOT NULL
    AND event_timestamp IS NOT NULL
),

video_latest AS (
  SELECT
    video_id,
    NULLIF(TRIM(video_category), '') AS video_category
  FROM `{project}.{dataset}.data_lake_youtube_trending_kr`
  WHERE video_id IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY video_id
    ORDER BY video_trending_date DESC, collected_at DESC, video_published_at DESC
  ) = 1
),

action_with_category AS (
  SELECT
    a.user_id,
    a.video_id,
    a.event_type,
    a.event_timestamp,
    a.watch_time_sec,
    v.video_category
  FROM action_log a
  LEFT JOIN video_latest v
    ON a.video_id = v.video_id
),

date_bounds AS (
  SELECT
    DATE(MIN(event_timestamp), 'Asia/Seoul') AS min_date,
    DATE(MAX(event_timestamp), 'Asia/Seoul') AS max_date
  FROM action_log
),

snapshots AS (
  SELECT
    TIMESTAMP(snapshot_date, 'Asia/Seoul') AS event_timestamp
  FROM date_bounds,
  UNNEST(GENERATE_DATE_ARRAY(min_date, max_date)) AS snapshot_date
),

users AS (
  SELECT DISTINCT user_id
  FROM action_log
),

user_snapshots AS (
  SELECT
    u.user_id,
    s.event_timestamp
  FROM users u
  CROSS JOIN snapshots s
),

user_7d AS (
  SELECT
    us.user_id,
    us.event_timestamp,

    COUNTIF(
      a.event_type = 'click'
      AND a.event_timestamp >= TIMESTAMP_SUB(us.event_timestamp, INTERVAL 7 DAY)
      AND a.event_timestamp < us.event_timestamp
    ) AS recent_click_count_7d,

    COUNTIF(
      a.event_type = 'view'
      AND a.event_timestamp >= TIMESTAMP_SUB(us.event_timestamp, INTERVAL 7 DAY)
      AND a.event_timestamp < us.event_timestamp
    ) AS recent_view_count_7d,

    SUM(
      IF(
        a.event_type = 'view'
        AND a.event_timestamp >= TIMESTAMP_SUB(us.event_timestamp, INTERVAL 7 DAY)
        AND a.event_timestamp < us.event_timestamp,
        COALESCE(a.watch_time_sec, 0),
        0
      )
    ) AS recent_watch_time_7d,

    COUNTIF(
      a.event_type = 'like'
      AND a.event_timestamp >= TIMESTAMP_SUB(us.event_timestamp, INTERVAL 7 DAY)
      AND a.event_timestamp < us.event_timestamp
    ) AS recent_like_count_7d,

    COUNTIF(
      a.event_timestamp >= TIMESTAMP_SUB(us.event_timestamp, INTERVAL 7 DAY)
      AND a.event_timestamp < us.event_timestamp
    ) AS total_event_count_7d

  FROM user_snapshots us
  LEFT JOIN action_with_category a
    ON us.user_id = a.user_id
   AND a.event_timestamp >= TIMESTAMP_SUB(us.event_timestamp, INTERVAL 7 DAY)
   AND a.event_timestamp < us.event_timestamp
  GROUP BY
    us.user_id,
    us.event_timestamp
),

category_counts AS (
  SELECT
    us.user_id,
    us.event_timestamp,
    a.video_category,
    COUNT(*) AS category_event_count
  FROM user_snapshots us
  JOIN action_with_category a
    ON us.user_id = a.user_id
   AND a.event_timestamp >= TIMESTAMP_SUB(us.event_timestamp, INTERVAL 30 DAY)
   AND a.event_timestamp < us.event_timestamp
  WHERE a.event_type IN ('click', 'view', 'like')
    AND a.video_category IS NOT NULL
  GROUP BY
    us.user_id,
    us.event_timestamp,
    a.video_category
),

category_rank AS (
  SELECT
    user_id,
    event_timestamp,
    video_category,
    ROW_NUMBER() OVER (
      PARTITION BY user_id, event_timestamp
      ORDER BY category_event_count DESC, video_category
    ) AS rn
  FROM category_counts
)

SELECT
  u.user_id,
  u.event_timestamp,
  COALESCE(u.recent_click_count_7d, 0) AS recent_click_count_7d,
  COALESCE(u.recent_view_count_7d, 0) AS recent_view_count_7d,
  COALESCE(u.recent_watch_time_7d, 0) AS recent_watch_time_7d,
  COALESCE(u.recent_like_count_7d, 0) AS recent_like_count_7d,
  COALESCE(c.video_category, 'unknown') AS historical_category_affinity,
  COALESCE(u.total_event_count_7d, 0) AS total_event_count_7d
FROM user_7d u
LEFT JOIN category_rank c
  ON u.user_id = c.user_id
 AND u.event_timestamp = c.event_timestamp
 AND c.rn = 1;
```

---

<a id="video_feature"></a>

## 📋 video_feature

- 영상 자체의 메타데이터와 인기도 컬럼을 모델 입력에 맞게 가공한 피처
    - raw Video 컬럼 중 모델에 필요한 컬럼만 선택하고, 문자열/비율/날짜 계산을 수행한다

### 🔸 Source

- Raw table: `data_lake/youtube_trending_kr`
- Feature View: `VideoFeatureView`
- Entity: `video_id`
- 갱신 주기: YouTube 데이터 수집 이후 batch

### 🔸 Columns

| 중간 테이블 컬럼 | 타입 | 원본 컬럼 | 생성 규칙 | 설명 |
| --- | --- | --- | --- | --- |
| `video_id` | STRING | `video_id` | 그대로 사용 | 영상 키 |
| `event_timestamp` | TIMESTAMP | `video_trending_date` | 스냅샷 유효 시각을 그대로 사용 | 피처 수집/유효 시각 |
| `category_id` | STRING | `video_category` | rename. null 또는 빈 문자열이면 `"unknown"` | 모델 입력용 카테고리 |
| `duration_sec` | INT64 | `video_duration` | ISO 8601 duration 문자열을 초 단위로 변환 | 영상 길이 |
| `view_count` | INT64 | `video_view_count` | rename. null이면 0 | 조회수 |
| `like_ratio` | FLOAT64 | `video_like_count`, `video_view_count` | `SAFE_DIVIDE(video_like_count, video_view_count)` | 좋아요 비율 |
| `comment_ratio` | FLOAT64 | `video_comment_count`, `video_view_count` | `SAFE_DIVIDE(video_comment_count, video_view_count)` | 댓글 비율 |
| `days_since_upload` | INT64 | `video_published_at`, `video_trending_date` | `DATE_DIFF(DATE(video_trending_date), DATE(video_published_at), DAY)` | 업로드 후 경과일 |
| `channel_subscriber_count` | INT64 | `channel_subscriber_count` | null이면 0 | 채널 영향력 |
| `channel_view_count` | INT64 | `channel_view_count` | null이면 0 | 채널 누적 조회수 |
| `channel_video_count` | INT64 | `channel_video_count` | null이면 0 | 채널 영상 수 |

- `feature_created_at`은 row 컬럼으로 저장하지 않는다.
- 피처 생성 시각은 pipeline log 또는 별도 metadata table에서 관리한다.

> [!IMPORTANT]
> `view_count`, `like_ratio`, `comment_ratio`, `channel_subscriber_count`는 관측
> 시점에 따라 변하므로 `event_timestamp`에는 스냅샷 유효 시각인
> `video_trending_date`를 사용합니다. 일일 API 수집에서는 이 값이
> `collected_at`과 같고, 백필에서는 과거 스냅샷 시각을 보존합니다. 백필의
> `collected_at`은 모든 행이 실행 시각을 공유하므로 point-in-time feature
> timestamp로 사용하면 안 됩니다.

#### Feast View에 등록할 Feature columns

| Feature column |
| --- |
| `category_id` |
| `duration_sec` |
| `view_count` |
| `like_ratio` |
| `comment_ratio` |
| `days_since_upload` |
| `channel_subscriber_count` |
| `channel_view_count` |
| `channel_video_count` |

### 🔸 SQL

```sql
CREATE OR REPLACE TABLE `{project}.{dataset}.video_feature` AS
WITH parsed AS (
  SELECT
    video_id,
    video_trending_date AS event_timestamp,
    video_category AS category_id,

    (
      COALESCE(SAFE_CAST(REGEXP_EXTRACT(video_duration, r'P(\d+)D') AS INT64), 0) * 86400
      + COALESCE(SAFE_CAST(REGEXP_EXTRACT(video_duration, r'(\d+)H') AS INT64), 0) * 3600
      + COALESCE(SAFE_CAST(REGEXP_EXTRACT(video_duration, r'(\d+)M') AS INT64), 0) * 60
      + COALESCE(SAFE_CAST(REGEXP_EXTRACT(video_duration, r'(\d+)S') AS INT64), 0)
    ) AS duration_sec,

    COALESCE(video_view_count, 0) AS view_count,

    SAFE_DIVIDE(video_like_count, NULLIF(video_view_count, 0)) AS like_ratio,
    SAFE_DIVIDE(video_comment_count, NULLIF(video_view_count, 0)) AS comment_ratio,

    DATE_DIFF(
      DATE(video_trending_date),
      DATE(video_published_at),
      DAY
    ) AS days_since_upload,

    COALESCE(channel_subscriber_count, 0) AS channel_subscriber_count,
    COALESCE(channel_view_count, 0) AS channel_view_count,
    COALESCE(channel_video_count, 0) AS channel_video_count
  FROM `{project}.{dataset}.data_lake_youtube_trending_kr`
  WHERE video_id IS NOT NULL
    AND video_trending_date IS NOT NULL
)

SELECT
  video_id,
  event_timestamp,
  COALESCE(NULLIF(TRIM(category_id), ''), 'unknown') AS category_id,
  COALESCE(duration_sec, 0) AS duration_sec,
  view_count,
  COALESCE(like_ratio, 0.0) AS like_ratio,
  COALESCE(comment_ratio, 0.0) AS comment_ratio,
  COALESCE(days_since_upload, 0) AS days_since_upload,
  channel_subscriber_count,
  channel_view_count,
  channel_video_count
FROM parsed
WHERE event_timestamp IS NOT NULL
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY video_id, event_timestamp
  ORDER BY event_timestamp DESC
) = 1;
```

### 제외하거나 후순위로 둘 raw 컬럼

| raw 컬럼 | 처리 방향 |
| --- | --- |
| `video_title` | 모델 직접 입력 X. 추후 text embedding 또는 keyword feature로 가공 가능 |
| `video_description` | 모델 직접 입력 X. 추후 embedding/NLP 피처 후보 |
| `video_tags` | 모델 직접 입력 X. 추후 tag similarity 또는 topic feature 후보 |
| `video_default_thumbnail` | baseline 제외 |
| `video_dimension`, `video_definition`, `video_licensed_content` | baseline 제외. 필요 시 추후 실험 피처 |
| `channel_description`, `channel_localized_description` | 모델 직접 입력 X. 추후 text embedding 후보 |
| `channel_custom_url`, `channel_localized_title` | 모델 피처로는 우선 제외 |

- `video_description`, `video_tags`, `channel_description` 같은 텍스트 컬럼은 나중에 embedding이나 NLP 피처를 만들 때는 쓸 수 있음 (raw 형태 말고 임베딩 또는 score 형태)

---

<a id="training_entity"></a>

## 📋 training_entity

- 어떤 user/video/event_timestamp 조합을 학습 대상으로 삼을지 정의하는 기준 테이블
- Feast에 ‘**이 시점 기준으로 피처 붙여줘’**라고 요청하기 위한 기준 dataframe에 가까움

### 🔸 Source

- Raw table: `data_lake/action_log`
- 기준 이벤트: `event_type = 'impression'` 또는 노출에 해당하는 이벤트
- Positive event: `event_type = 'click'`

### 🔸 Columns

| 컬럼 | 타입 | 생성 규칙 | 설명 |
| --- | --- | --- | --- |
| `dataset_id` | STRING | dataset 생성 config에서 주입 | 어떤 실험/데이터셋 버전인지 식별 |
| `user_id` | STRING | impression action log에서 추출 | 노출된 유저 |
| `video_id` | STRING | impression action log에서 추출 | 노출된 영상 |
| `event_timestamp` | TIMESTAMP | impression 이벤트의 timestamp | 어느 시점 기준으로 피처를 조회할지 결정 |
| `clicked` | INT64 | clicked label 생성 규칙으로 산출 | 클릭 여부 |
| `source_event_id` | STRING | impression 이벤트의 `event_id` | 원본 이벤트 추적용 |

`label_window_sec`, `created_at`은 row 컬럼으로 저장하지 않고, `training_dataset_metadata`에서 관리한다.

#### Clicked label 생성 규칙

- `impression` 이벤트를 기준 row로 사용한다.
- 같은 `user_id`, `video_id`에 대해 impression 이후 30분 이내
  `event_type = 'click'` 이벤트가 있으면 `clicked = 1`로 둔다.
- 없으면 `clicked = 0`으로 둔다.
- 30분 window는 `label_window_sec = 1800`으로 metadata/config에 기록한다.

> [!WARNING]
> 현재 action log에는 click과 impression을 직접 연결하는 `impression_id`,
> `request_id`, `session_id`가 없습니다. 따라서 아래 설계 SQL은 같은
> `user_id`·`video_id`의 impression window가 겹치면 하나의 click로 여러
> impression을 positive 처리할 수 있습니다. 실행 파이프라인을 구현하기 전에
> 이 규칙을 그대로 채택할지, click 직전의 가장 가까운 impression 한 건으로
> attribution할지 결정하고 `feature_set_version`에 기록해야 합니다.

### 🔸 SQL

```sql
DECLARE dataset_id STRING DEFAULT 'ctr_train_v1';
DECLARE label_window_sec INT64 DEFAULT 1800;

CREATE OR REPLACE TABLE `{project}.{dataset}.training_entity` AS
WITH impressions AS (
  SELECT
    event_id AS source_event_id,
    user_id,
    video_id,
    event_timestamp
  FROM `{project}.{dataset}.data_lake_action_log`
  WHERE event_type = 'impression'
    AND user_id IS NOT NULL
    AND video_id IS NOT NULL
    AND event_timestamp IS NOT NULL
),

clicks AS (
  SELECT
    user_id,
    video_id,
    event_timestamp AS click_timestamp
  FROM `{project}.{dataset}.data_lake_action_log`
  WHERE event_type = 'click'
    AND user_id IS NOT NULL
    AND video_id IS NOT NULL
    AND event_timestamp IS NOT NULL
)

SELECT
  dataset_id AS dataset_id,
  i.user_id,
  i.video_id,
  i.event_timestamp,
  IF(COUNT(c.click_timestamp) > 0, 1, 0) AS clicked,
  i.source_event_id
FROM impressions i
LEFT JOIN clicks c
  ON i.user_id = c.user_id
 AND i.video_id = c.video_id
 AND c.click_timestamp > i.event_timestamp
 AND c.click_timestamp <= TIMESTAMP_ADD(i.event_timestamp, INTERVAL label_window_sec SECOND)
GROUP BY
  i.user_id,
  i.video_id,
  i.event_timestamp,
  i.source_event_id;
```

---

<a id="user_topic_embedding"></a>

## 📋 user_topic_embedding

- `topic_similarity` 계산을 위한 중간 산출물
    - 모델에 직접 입력하지 않고, 사용자 관심 키워드와 영상 카테고리 간 유사도 점수 계산에 사용
    - BigQuery는 `ARRAY<ARRAY<FLOAT64>>` 같은 중첩 배열을 직접 다루기 어렵기 때문에, 사용자별 여러 keyword embedding은 한 row에 nested list로 넣기보다 **키워드 1개당 row 1개**로 저장한다.

### 🔸 Columns

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `user_id` | STRING | 유저 키 |
| `event_timestamp` | TIMESTAMP | embedding이 유효한 기준 시각. static persona 기반이면 `1970-01-01 00:00:00 UTC` 사용 |
| `topic` | STRING | 사용자 관심 키워드 또는 disambiguation phrase |
| `topic_embedding` | ARRAY<FLOAT64> | 해당 topic의 embedding vector |
| `embedding_model` | STRING | embedding 생성에 사용한 모델명 |
| `embedding_dim` | INT64 | embedding 차원 |
| `embedding_version` | STRING | user topic embedding 버전 |
| `topic_source` | STRING | 원본 keyword source. 예: `hobby_keywords`, `interest_keywords`, `food_keywords` |

### 🔸 생성 규칙

1. `user_static_feature.preferred_topics`를 explode한다.
2. 각 topic을 embedding model로 개별 인코딩한다.
3. topic별 embedding을 `user_topic_embedding`에 저장한다.
4. online serving 시점에는 embedding을 새로 생성하지 않는다.

---

<a id="category_embedding"></a>

## 📋 category_embedding

- 고정된 YouTube 카테고리 설명문과 해당 설명문의 embedding을 저장하는 정적 참조 테이블
    - `category_embedding`은 `user_id`나 `video_id` entity에 종속되지 않으므로 Feast Feature View로 등록하지 않는다.

### 🔸 Columns

| 컬럼 | 타입 | 설명 |
| --- | --- | --- |
| `category_id` | STRING | canonical 카테고리 키. 현재 저장소에서는 `"Music"` 같은 카테고리 이름 문자열 |
| `category_name` | STRING | 카테고리명 |
| `category_description` | STRING | 카테고리 설명문 |
| `category_embedding` | ARRAY<FLOAT64> | 카테고리 설명문 embedding |
| `embedding_model` | STRING | embedding 생성에 사용한 모델명 |
| `embedding_dim` | INT64 | embedding 차원 |
| `embedding_version` | STRING | category embedding 버전 |

### 🔸 생성 규칙

1. 카테고리별 설명문을 1회 작성한다.
2. 각 설명문을 embedding model로 인코딩한다.
3. `category_embedding`에 저장한다.
4. 카테고리 설명문이나 embedding model이 바뀌면 `embedding_version`을 변경한다.

---

<a id="user_category_similarity"></a>

## 📋 user_category_similarity

- 사용자 관심 topic embedding과 카테고리 embedding을 조합해, 유저별 카테고리 유사도를 미리 계산한 피처 테이블
- `topic_similarity`를 serving 시점에 매번 vector cosine으로 계산하지 않고, offline/batch 단계에서 scalar score로 미리 계산해 둔다.
- 최종 모델 입력에는 `topic_similarity`만 사용하고, embedding vector 자체는 모델에 직접 입력하지 않는다.

### 🔸 Source

- Artifact table: `user_topic_embedding`
- Reference / Artifact table: `category_embedding`
- Feature View: `UserCategorySimilarityView`
- Entity: `user_id`, `category_id`
- Category key: 카테고리 이름 STRING
- 갱신 주기: user topic embedding 또는 category embedding 갱신 이후 batch

### 🔸 Columns

| 중간 테이블 컬럼 | 타입 | 원본/계산 기준 | 생성 규칙 | 설명 |
| --- | --- | --- | --- | --- |
| `user_id` | STRING | `user_topic_embedding.user_id` | 그대로 사용 | 유저 키 |
| `category_id` | STRING | `category_embedding.category_id` | 그대로 사용 | 카테고리 키 |
| `event_timestamp` | TIMESTAMP | `user_topic_embedding.event_timestamp` | similarity가 유효한 기준 시각 | Feast point-in-time join 기준 timestamp |
| `topic_similarity` | FLOAT64 | `topic_embedding`, `category_embedding` | 사용자 topic embedding들과 category embedding 간 cosine similarity 중 최댓값 | 모델 입력용 scalar feature |
| `topic_similarity_top_topic` | STRING | `user_topic_embedding.topic` | max cosine similarity를 만든 topic | 디버깅/분석용 |
| `embedding_model` | STRING | embedding 생성 모델 | user/category embedding이 같은 embedding space에서 생성됐는지 확인 | embedding 모델명 |
| `embedding_dim` | INT64 | embedding vector 차원 | user/category embedding 차원이 같은지 확인 | embedding 차원 |
| `user_topic_embedding_version` | STRING | `user_topic_embedding.embedding_version` | 그대로 사용 | 사용자 topic embedding 버전 |
| `category_embedding_version` | STRING | `category_embedding.embedding_version` | 그대로 사용 | 카테고리 embedding 버전 |
| `similarity_method` | STRING | 고정값 | `cosine` | 유사도 계산 방식 |
| `similarity_pooling` | STRING | 고정값 | `max` | 여러 topic similarity를 하나로 줄이는 방식 |

#### 🔸 Feast View에 등록할 Feature columns

| Feature column |
| --- |
| `topic_similarity` |
| `topic_similarity_top_topic` |

`topic_similarity_top_topic`은 모델 입력에는 사용하지 않고 디버깅/분석용으로 보관한다.

### 🔸 계산 규칙

1. `user_topic_embedding`과 `category_embedding`을 cross join한다.
2. 같은 embedding model, embedding dim, version 조건을 만족하는 row만 사용한다.
3. 사용자 topic별로 category embedding과 cosine similarity를 계산한다.
4. 유저-카테고리 단위로 가장 높은 cosine score를 `topic_similarity`로 저장한다.
5. 가장 높은 score를 만든 topic을 `topic_similarity_top_topic`으로 저장한다.
6. topic embedding이 없으면 `topic_similarity = 0.0`, `topic_similarity_top_topic = 'unknown'`으로 처리한다.

> [!WARNING]
> 6번은 최종 `training_dataset`의 cold-start 계약입니다. 아래 SQL은
> `user_topics`에 row가 없는 사용자를 결과에 생성하지 않으므로
> `user_category_similarity` 자체에는 기본값 row가 생기지 않습니다. 마지막
> SELECT의 `COALESCE`도 이미 존재하는 row의 null만 처리합니다. 따라서
> `training_dataset`에서 LEFT JOIN 후 `0.0`·`unknown`으로 채우거나, source
> materialization에서 user-category grid를 별도로 생성해야 합니다.

### 🔸 SQL

```sql
DECLARE user_topic_embedding_version STRING DEFAULT 'user_topic_embedding_v1';
DECLARE category_embedding_version STRING DEFAULT 'category_embedding_v1';

CREATE OR REPLACE TABLE `{project}.{dataset}.user_category_similarity` AS
WITH user_topics AS (
  SELECT
    user_id,
    event_timestamp,
    topic,
    topic_embedding,
    embedding_model,
    embedding_dim,
    embedding_version
  FROM `{project}.{dataset}.user_topic_embedding`
  WHERE user_id IS NOT NULL
    AND topic IS NOT NULL
    AND topic_embedding IS NOT NULL
    AND embedding_version = user_topic_embedding_version
),

categories AS (
  SELECT
    category_id,
    category_name,
    category_embedding,
    embedding_model,
    embedding_dim,
    embedding_version
  FROM `{project}.{dataset}.category_embedding`
  WHERE category_id IS NOT NULL
    AND category_embedding IS NOT NULL
    AND embedding_version = category_embedding_version
),

topic_category_cosine AS (
  SELECT
    u.user_id,
    u.event_timestamp,
    c.category_id,
    u.topic,
    u.embedding_model,
    u.embedding_dim,
    u.embedding_version AS user_topic_embedding_version,
    c.embedding_version AS category_embedding_version,

    SAFE_DIVIDE(
      SUM(user_val * category_val),
      NULLIF(
        SQRT(SUM(user_val * user_val)) * SQRT(SUM(category_val * category_val)),
        0
      )
    ) AS cosine_score

  FROM user_topics u
  JOIN categories c
    ON u.embedding_model = c.embedding_model
   AND u.embedding_dim = c.embedding_dim
  CROSS JOIN UNNEST(u.topic_embedding) AS user_val WITH OFFSET user_idx
  JOIN UNNEST(c.category_embedding) AS category_val WITH OFFSET category_idx
    ON user_idx = category_idx
  GROUP BY
    u.user_id,
    u.event_timestamp,
    c.category_id,
    u.topic,
    u.embedding_model,
    u.embedding_dim,
    u.embedding_version,
    c.embedding_version
),

ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY user_id, event_timestamp, category_id
      ORDER BY cosine_score DESC, topic
    ) AS rn
  FROM topic_category_cosine
)

SELECT
  user_id,
  category_id,
  event_timestamp,
  COALESCE(cosine_score, 0.0) AS topic_similarity,
  COALESCE(topic, 'unknown') AS topic_similarity_top_topic,
  embedding_model,
  embedding_dim,
  user_topic_embedding_version,
  category_embedding_version,
  'cosine' AS similarity_method,
  'max' AS similarity_pooling
FROM ranked
WHERE rn = 1;
```

### 🔸 Feature View 등록 기준

`user_category_similarity`는 embedding vector를 직접 저장하는 테이블이 아니라, 모델이 사용할 수 있는 scalar feature인 `topic_similarity`를 저장한다.

따라서 `UserCategorySimilarityView`로 Feast에 등록할 수 있다.

| 항목 | 값 |
| --- | --- |
| Feature View | `UserCategorySimilarityView` |
| Entity | `user_id`, `category_id` |
| Timestamp | `event_timestamp` |
| Source table | `user_category_similarity` |

---

<a id="training-dataset-contract"></a>

## training_dataset 연계 계약

이 문서의 테이블은 최종 모델 입력을 만들기 위한 source와 artifact입니다. 최종
`training_dataset`의 전체 컬럼, interaction feature, BigQuery as-of join fallback,
`training_dataset_metadata`, 검증 체크리스트는
[`training-dataset.md`](./training-dataset.md)를 기준으로 유지합니다.

생성 순서는 다음과 같습니다.

1. `training_entity`를 entity dataframe으로 사용합니다.
2. Feast Historical Retrieval로 `UserStaticView`, `UserDynamicView`,
   `VideoFeatureView`를 point-in-time join합니다.
3. `VideoFeatureView.category_id`를 얻은 뒤 `user_id` + `category_id`로
   `UserCategorySimilarityView`를 조회합니다.
4. `preferred_category_match`, `historical_category_match`를 계산합니다.
5. label과 feature를 합쳐 `training_dataset`을 저장하고 dataset-level 설정은
   `training_dataset_metadata`에 기록합니다.

`src/pipeline/build_training_dataset.py`는 로컬 CSV 기반 prototype이며, 위 4개
Feature View와 BigQuery source table을 materialize하거나 Feast Historical
Retrieval을 수행하는 구현은 아닙니다. 두 경로의 결과를 같은 feature version으로
취급해서는 안 됩니다.
