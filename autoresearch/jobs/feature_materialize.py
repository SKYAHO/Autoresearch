import re


FEATURE_TABLES: tuple[str, ...] = (
    "user_static_feature",
    "user_dynamic_feature",
    "video_feature",
)


def _string_array(column_name: str) -> str:
    return (
        "IFNULL(ARRAY(SELECT item.element "
        f"FROM UNNEST({column_name}.list) AS item), ARRAY<STRING>[])"
    )


_FEATURE_SELECTS: dict[str, str] = {
    "user_static_feature": """
SELECT
  user_id,
  TIMESTAMP '1970-01-01 00:00:00 UTC' AS event_timestamp,
  COALESCE(age_bucket, 'unknown') AS age_group,
  COALESCE(occupation, 'unknown') AS occupation,
  {primary_categories} AS preferred_category,
  ARRAY_CONCAT(
    {hobby_keywords}, {interest_keywords}, {lifestyle_keywords},
    {food_keywords}, {travel_keywords}, {career_keywords},
    {family_context_keywords}
  ) AS preferred_topics,
  CASE
    WHEN LOWER(TRIM(watch_time_band)) IN ('morning', 'am', '오전', '아침') THEN 'morning'
    WHEN LOWER(TRIM(watch_time_band)) IN ('evening', 'pm', '저녁', '오후') THEN 'evening'
    WHEN LOWER(TRIM(watch_time_band)) IN ('night', 'late_night', '밤', '심야') THEN 'night'
    ELSE 'unknown'
  END AS watch_time_band
FROM `{project_id}.{dataset_id}.asset_virtual_user_vu_1000`
WHERE user_id IS NOT NULL
""",
    "user_dynamic_feature": """
WITH action_log AS (
  SELECT
    user_id,
    video_id,
    event_type,
    event_timestamp,
    COALESCE(watch_time_sec, 0) AS watch_time_sec
  FROM `{project_id}.{dataset_id}.data_lake_action_log`
  WHERE user_id IS NOT NULL
    AND event_timestamp IS NOT NULL
),

video_latest AS (
  SELECT
    video_id,
    video_category
  FROM `{project_id}.{dataset_id}.data_lake_youtube_trending_kr`
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
""",
    "video_feature": """
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
  FROM `{project_id}.{dataset_id}.data_lake_youtube_trending_kr`
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
""",
}


def build_materialize_script(
    project_id: str, dataset_id: str, table_name: str
) -> str:
    """Build the transactional BigQuery script for one supported feature table."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", project_id):
        raise ValueError("invalid project_id")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", dataset_id):
        raise ValueError("invalid dataset_id")
    if table_name not in FEATURE_TABLES:
        raise ValueError(f"unsupported feature table: {table_name}")

    target = f"`{project_id}.{dataset_id}.{table_name}`"
    select_sql = _FEATURE_SELECTS[table_name].format(
        project_id=project_id,
        dataset_id=dataset_id,
        primary_categories=_string_array("primary_categories"),
        hobby_keywords=_string_array("hobby_keywords"),
        interest_keywords=_string_array("interest_keywords"),
        lifestyle_keywords=_string_array("lifestyle_keywords"),
        food_keywords=_string_array("food_keywords"),
        travel_keywords=_string_array("travel_keywords"),
        career_keywords=_string_array("career_keywords"),
        family_context_keywords=_string_array("family_context_keywords"),
    )
    return f"""
BEGIN TRANSACTION;
CREATE TEMP TABLE materialized_rows AS
{select_sql};
ASSERT (SELECT COUNT(*) FROM materialized_rows) > 0
  AS 'materialized feature result must not be empty';
DELETE FROM {target} WHERE TRUE;
INSERT INTO {target} SELECT * FROM materialized_rows;
COMMIT TRANSACTION;
"""
