"""Raw warehouse와 Feast·학습 소비 단계 사이의 feature materialization을 담당한다.

이 모듈은 raw action log와 YouTube trending 데이터를 Terraform이 관리하는
``user_static_feature``, ``user_dynamic_feature``, ``video_feature`` target으로
변환하는 SQL 및 공개 JSONL batch CLI를 제공한다. GCS raw 적재는 인접 수집
단계가, Feast·학습 데이터 조회는 downstream 단계가 담당한다.
``src.pipeline.build_feature_tables``, Airflow schedule, Terraform 관리는 이
모듈의 책임이 아니다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from typing import Any, Sequence

from autoresearch.jobs import BATCH_CONTRACT_VERSION


logger = logging.getLogger(__name__)
_REVISION = os.getenv("AUTORESEARCH_REVISION", "unknown")
JOB_NAME = "feature_materialize"


FEATURE_TABLES: tuple[str, ...] = (
    "user_static_feature",
    "user_dynamic_feature",
    "video_feature",
)
_GCP_PROJECT_ID_PATTERN = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")


class BatchArgumentError(ValueError):
    """공개 batch 명령의 문법·범위 오류."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BatchArgumentError(message)


def _version_json() -> str:
    return json.dumps(
        {
            "application_revision": _REVISION,
            "contract_version": BATCH_CONTRACT_VERSION,
        },
        sort_keys=True,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=_version_json())
    parser.add_argument("--project", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--raw-dataset", required=True)
    return parser


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
FROM `{project_id}.{raw_dataset_id}.asset_virtual_user_vu_1000`
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
  FROM `{project_id}.{raw_dataset_id}.data_lake_action_log`
  WHERE user_id IS NOT NULL
    AND event_timestamp IS NOT NULL
),

video_latest AS (
  SELECT
    video_id,
    video_category
  FROM `{project_id}.{raw_dataset_id}.data_lake_youtube_trending_kr`
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
    "video_feature": r"""
WITH parsed AS (
  SELECT
    video_id,
    collected_at AS event_timestamp,
    video_category AS category_id,

    COALESCE(
      SAFE_ADD(
        SAFE_ADD(
          SAFE_ADD(
            COALESCE(
              SAFE_MULTIPLY(
                SAFE_CAST(REGEXP_EXTRACT(video_duration, r'P(\d+)D') AS INT64),
                86400
              ),
              0
            ),
            COALESCE(
              SAFE_MULTIPLY(
                SAFE_CAST(REGEXP_EXTRACT(video_duration, r'(\d+)H') AS INT64),
                3600
              ),
              0
            )
          ),
          COALESCE(
            SAFE_MULTIPLY(
              SAFE_CAST(REGEXP_EXTRACT(video_duration, r'(\d+)M') AS INT64),
              60
            ),
            0
          )
        ),
        COALESCE(SAFE_CAST(REGEXP_EXTRACT(video_duration, r'(\d+)S') AS INT64), 0)
      ),
      0
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
  FROM `{project_id}.{raw_dataset_id}.data_lake_youtube_trending_kr`
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
    project_id: str, dataset_id: str, raw_dataset_id: str, table_name: str
) -> str:
    """Build the transactional BigQuery script for one supported feature table."""
    if not isinstance(project_id, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_-]*", project_id
    ):
        raise ValueError("invalid project_id")
    if not isinstance(dataset_id, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", dataset_id
    ):
        raise ValueError("invalid dataset_id")
    if not isinstance(raw_dataset_id, str) or not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", raw_dataset_id
    ):
        raise ValueError("invalid raw_dataset_id")
    if table_name not in FEATURE_TABLES:
        raise ValueError("unsupported feature table")

    target = f"`{project_id}.{dataset_id}.{table_name}`"
    select_sql = _FEATURE_SELECTS[table_name].format(
        project_id=project_id,
        dataset_id=dataset_id,
        raw_dataset_id=raw_dataset_id,
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
SELECT COUNT(*) AS final_row_count FROM {target};
"""


def _validate_args(args: argparse.Namespace) -> None:
    if not _GCP_PROJECT_ID_PATTERN.fullmatch(args.project):
        raise BatchArgumentError("invalid GCP project ID")
    if len(args.dataset) > 1024:
        raise BatchArgumentError("invalid dataset ID")
    if len(args.raw_dataset) > 1024:
        raise BatchArgumentError("invalid raw dataset ID")
    try:
        build_materialize_script(
            args.project, args.dataset, args.raw_dataset, FEATURE_TABLES[0]
        )
    except ValueError as exc:
        raise BatchArgumentError(str(exc)) from exc


def _bigquery_client(project_id: str) -> Any:
    from google.cloud import bigquery

    return bigquery.Client(project=project_id)


def _run(args: argparse.Namespace) -> dict[str, object]:
    client = _bigquery_client(args.project)
    job_ids: list[str] = []
    row_counts: dict[str, int] = {}
    for table_name in FEATURE_TABLES:
        script = build_materialize_script(
            args.project, args.dataset, args.raw_dataset, table_name
        )
        job = client.query(script)
        rows = list(job.result())
        if len(rows) != 1:
            raise RuntimeError("invalid final row count result")
        try:
            row_count = rows[0]["final_row_count"]
        except (KeyError, TypeError):
            raise RuntimeError("invalid final row count result") from None
        if not isinstance(row_count, int) or isinstance(row_count, bool):
            raise RuntimeError("invalid final row count result")
        job_ids.append(job.job_id)
        row_counts[table_name] = row_count
    return {
        "status": "succeeded",
        "project": args.project,
        "dataset": args.dataset,
        "raw_dataset": args.raw_dataset,
        "tables": list(FEATURE_TABLES),
        "job_ids": job_ids,
        "row_counts": row_counts,
    }


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _summary(
    *, status: str, details: dict[str, object] | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "event": "job_summary",
        "contract_version": BATCH_CONTRACT_VERSION,
        "job": JOB_NAME,
        "status": status,
    }
    if details:
        payload.update(details)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 인자를 검증·실행하고 공개 종료 코드를 반환한다."""

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        _validate_args(args)
    except BatchArgumentError as exc:
        logger.error("Invalid feature_materialize arguments: %s", exc)
        _emit(_summary(status="failed", details={"error_type": "invalid_arguments"}))
        return 2

    try:
        result = dict(_run(args))
    except BatchArgumentError as exc:
        logger.error("Invalid feature_materialize arguments: %s", exc)
        _emit(_summary(status="failed", details={"error_type": "invalid_arguments"}))
        return 2
    except Exception as exc:  # noqa: BLE001 - process boundary maps failures to exit 1
        logger.error("feature_materialize failed (%s)", type(exc).__name__)
        _emit(_summary(status="failed", details={"error_type": "runtime_failure"}))
        return 1

    status = str(result.pop("status", "succeeded"))
    _emit(_summary(status=status, details=result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
