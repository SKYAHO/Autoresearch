#!/usr/bin/env python3
"""
Feast offline store 중간 피처 테이블(user_static_feature/user_dynamic_feature/
video_feature/training_entity) 배치 생성.

docs/guides/data-warehouse.md에 문서화된 SQL을 그대로 실행한다. user_topic_embedding/
category_embedding/user_category_similarity와 Feast get_historical_features()
연결은 별도 이슈(#214)에서 다룬다 — 이 스크립트는 그 전 단계인 4개 순수 SQL
테이블만 채운다 (issue #207 Phase 4a).

테이블 스키마(REQUIRED 등)는 Terraform이 소유한다 — 이 스크립트는 데이터만
갱신한다. 그래서 `CREATE OR REPLACE TABLE ... AS SELECT`(BigQuery가 SELECT
결과에서 스키마를 다시 유추해 REQUIRED 제약을 지워버림, WRITE_TRUNCATE도 동일
문제)를 쓰지 않고, 기존 테이블에 `TRUNCATE TABLE` + `INSERT INTO`를 트랜잭션
으로 묶어 데이터만 교체한다 — 스키마는 그대로 유지되고, REQUIRED 컬럼에 NULL이
들어가려 하면 BigQuery가 INSERT 자체를 거부한다(불량 데이터 조기 검출).
트랜잭션으로 묶는 이유: TRUNCATE와 INSERT를 따로 실행하면 그 사이에 테이블이
빈 상태로 노출되는 순간이 생겨, 하필 그때 Feast가 읽으면 빈 결과를 가져간다.

대상 테이블은 사전에 존재해야 한다(Terraform이 생성) — 존재하지 않으면
TRUNCATE TABLE 단계에서 즉시 실패한다(의도된 동작, 조용히 새 테이블을 만들지
않는다).
"""

import os

BIGQUERY_PROJECT = os.environ.get("CTR_TRAINING_BQ_PROJECT", "ar-infra-501607")
BIGQUERY_DATASET = os.environ.get("CTR_TRAINING_BQ_DATASET", "feast_offline_store")
TRAINING_ENTITY_DATASET_ID = os.environ.get("CTR_TRAINING_ENTITY_DATASET_ID", "ctr_train_v1")
TRAINING_ENTITY_LABEL_WINDOW_SEC = int(
    os.environ.get("CTR_TRAINING_LABEL_WINDOW_SEC", "1800")
)


def _run_truncate_insert(client, project: str, dataset: str, table: str, select_sql: str) -> None:
    """TRUNCATE TABLE + INSERT INTO를 트랜잭션으로 묶어 실행한다.

    select_sql은 세미콜론 없는 SELECT(또는 WITH ... SELECT) 본문이어야 한다.
    """
    full_table = f"`{project}.{dataset}.{table}`"
    sql = f"""
BEGIN TRANSACTION;
TRUNCATE TABLE {full_table};
INSERT INTO {full_table}
{select_sql};
COMMIT TRANSACTION;
"""
    client.query(sql).result()


def build_user_static_feature(client, project: str = BIGQUERY_PROJECT, dataset: str = BIGQUERY_DATASET) -> None:
    """asset_virtual_user_vu_1000 원본을 user_static_feature로 변환한다.

    docs/guides/data-warehouse.md#user_static_feature SQL과 동일한 로직.
    """
    select_sql = f"""
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
    WHEN LOWER(TRIM(watch_time_band)) IN ('evening', 'pm', '저녁', '오후') THEN 'evening'
    WHEN LOWER(TRIM(watch_time_band)) IN ('night', 'late_night', '밤', '심야') THEN 'night'
    ELSE 'unknown'
  END AS watch_time_band

FROM `{project}.{dataset}.asset_virtual_user_vu_1000`
WHERE user_id IS NOT NULL
"""
    _run_truncate_insert(client, project, dataset, "user_static_feature", select_sql)


def build_user_dynamic_feature(client, project: str = BIGQUERY_PROJECT, dataset: str = BIGQUERY_DATASET) -> None:
    """data_lake_action_log 원본을 일 단위 snapshot 기준 user_dynamic_feature로 변환한다.

    docs/guides/data-warehouse.md#user_dynamic_feature SQL과 동일한 로직
    (7일 recent 집계 + 30일 historical_category_affinity).
    """
    select_sql = f"""
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
    video_category
  FROM `{project}.{dataset}.data_lake_youtube_trending_kr`
  WHERE video_id IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY video_id
    ORDER BY COALESCE(collected_at, video_trending_date, video_published_at) DESC
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
 AND c.rn = 1
"""
    _run_truncate_insert(client, project, dataset, "user_dynamic_feature", select_sql)


def build_video_feature(client, project: str = BIGQUERY_PROJECT, dataset: str = BIGQUERY_DATASET) -> None:
    """data_lake_youtube_trending_kr 원본을 video_feature로 변환한다.

    docs/guides/data-warehouse.md#video_feature SQL과 동일한 로직
    (채널 통계 3종 포함, video_id+event_timestamp 기준 최신 1건만 유지).
    """
    select_sql = f"""
WITH parsed AS (
  SELECT
    video_id,
    collected_at AS event_timestamp,
    video_category AS category_id,

    (
      COALESCE(SAFE_CAST(REGEXP_EXTRACT(video_duration, r'P(\\d+)D') AS INT64), 0) * 86400
      + COALESCE(SAFE_CAST(REGEXP_EXTRACT(video_duration, r'(\\d+)H') AS INT64), 0) * 3600
      + COALESCE(SAFE_CAST(REGEXP_EXTRACT(video_duration, r'(\\d+)M') AS INT64), 0) * 60
      + COALESCE(SAFE_CAST(REGEXP_EXTRACT(video_duration, r'(\\d+)S') AS INT64), 0)
    ) AS duration_sec,

    COALESCE(video_view_count, 0) AS view_count,

    SAFE_DIVIDE(video_like_count, NULLIF(video_view_count, 0)) AS like_ratio,
    SAFE_DIVIDE(video_comment_count, NULLIF(video_view_count, 0)) AS comment_ratio,

    DATE_DIFF(
      DATE(collected_at),
      DATE(video_published_at),
      DAY
    ) AS days_since_upload,

    COALESCE(channel_subscriber_count, 0) AS channel_subscriber_count,
    COALESCE(channel_view_count, 0) AS channel_view_count,
    COALESCE(channel_video_count, 0) AS channel_video_count
  FROM `{project}.{dataset}.data_lake_youtube_trending_kr`
  WHERE video_id IS NOT NULL
    AND collected_at IS NOT NULL
)

SELECT
  video_id,
  event_timestamp,
  COALESCE(category_id, 'unknown') AS category_id,
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
) = 1
"""
    _run_truncate_insert(client, project, dataset, "video_feature", select_sql)


def build_training_entity(
    client,
    project: str = BIGQUERY_PROJECT,
    dataset: str = BIGQUERY_DATASET,
    dataset_id: str = TRAINING_ENTITY_DATASET_ID,
    label_window_sec: int = TRAINING_ENTITY_LABEL_WINDOW_SEC,
) -> None:
    """data_lake_action_log의 impression/click을 training_entity로 변환한다.

    docs/guides/data-warehouse.md#training_entity SQL과 동일한 로직
    (click을 label_window_sec 이내 가장 가까운 직전 impression에 귀속).
    """
    select_sql = f"""
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
    event_id AS click_event_id,
    user_id,
    video_id,
    event_timestamp AS click_timestamp
  FROM `{project}.{dataset}.data_lake_action_log`
  WHERE event_type = 'click'
    AND user_id IS NOT NULL
    AND video_id IS NOT NULL
    AND event_timestamp IS NOT NULL
),

click_attribution_candidates AS (
  SELECT
    c.click_event_id,
    i.source_event_id,
    ROW_NUMBER() OVER (
      PARTITION BY c.click_event_id
      ORDER BY i.event_timestamp DESC
    ) AS rn
  FROM clicks c
  JOIN impressions i
    ON c.user_id = i.user_id
   AND c.video_id = i.video_id
   AND i.event_timestamp < c.click_timestamp
   AND i.event_timestamp >= TIMESTAMP_SUB(c.click_timestamp, INTERVAL {label_window_sec} SECOND)
),

positive_impressions AS (
  SELECT DISTINCT
    source_event_id
  FROM click_attribution_candidates
  WHERE rn = 1
)

SELECT
  '{dataset_id}' AS dataset_id,
  i.user_id,
  i.video_id,
  i.event_timestamp,
  IF(p.source_event_id IS NOT NULL, 1, 0) AS clicked,
  i.source_event_id
FROM impressions i
LEFT JOIN positive_impressions p
  ON i.source_event_id = p.source_event_id
"""
    _run_truncate_insert(client, project, dataset, "training_entity", select_sql)


def main() -> None:
    from google.cloud import bigquery

    client = bigquery.Client(project=BIGQUERY_PROJECT)

    print("[1/4] user_static_feature...")
    build_user_static_feature(client)
    print("[2/4] user_dynamic_feature...")
    build_user_dynamic_feature(client)
    print("[3/4] video_feature...")
    build_video_feature(client)
    print("[4/4] training_entity...")
    build_training_entity(client)
    print("완료.")


if __name__ == "__main__":
    main()
