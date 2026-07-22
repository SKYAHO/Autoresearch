"""data_lake_raw 테이블에서 Feast offline store feature 테이블을 만드는 공개 batch 명령.

BigQuery raw 계층(``data_lake_raw``)의 적재가 끝난 뒤 실행해 feature 계층
(``feast_offline_store``)의 Feast source 테이블을 재구축한다. SQL 계약은
``docs/guides/data-warehouse.md``를 단일 출처로 삼는다.

적재는 ``TRUNCATE TABLE`` + ``INSERT INTO``로 수행한다. ``CREATE OR REPLACE``나
``WRITE_TRUNCATE``는 Terraform이 소유한 대상 테이블 스키마(REQUIRED/REPEATED
mode 포함)를 query 결과 스키마로 교체하므로 사용하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

from autoresearch.jobs import BATCH_CONTRACT_VERSION

if TYPE_CHECKING:
    from google.cloud import bigquery

logger = logging.getLogger(__name__)
_REVISION = os.getenv("AUTORESEARCH_REVISION", "unknown")
JOB_NAME = "feature_store_build"

DEFAULT_PROJECT = "ar-infra-501607"
DEFAULT_DATASET = "feast_offline_store"
DEFAULT_RAW_DATASET = "data_lake_raw"
DEFAULT_LOCATION = "asia-northeast3"


class BatchArgumentError(ValueError):
    """공개 batch 명령의 문법·범위 오류."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BatchArgumentError(message)


@dataclass(frozen=True)
class FeatureTableSpec:
    """feature 테이블 하나를 재구축하기 위한 선언.

    ``columns``는 ``INSERT INTO`` 컬럼 목록이자 ``select_sql``의 출력 순서다.
    ``entity_keys`` + ``event_timestamp``는 Feast point-in-time join의 유일
    키이므로 적재 후 중복 검증에 사용한다.
    """

    name: str
    entity_keys: tuple[str, ...]
    columns: tuple[str, ...]
    select_sql: str


_USER_STATIC_SELECT = """\
SELECT
  user_id,
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

_USER_DYNAMIC_SELECT = """\
WITH action_log AS (
  SELECT
    user_id,
    video_id,
    event_type,
    event_timestamp,
    COALESCE(watch_time_sec, 0) AS watch_time_sec
  FROM `{project}.{raw_dataset}.data_lake_action_log`
  WHERE user_id IS NOT NULL
    AND event_timestamp IS NOT NULL
),
video_latest AS (
  SELECT
    video_id,
    video_category
  FROM `{project}.{raw_dataset}.data_lake_youtube_trending_kr`
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

_VIDEO_SELECT = r"""WITH parsed AS (
  SELECT
    video_id,
    collected_at AS event_timestamp,
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
    DATE_DIFF(DATE(collected_at), DATE(video_published_at), DAY) AS days_since_upload,
    COALESCE(channel_subscriber_count, 0) AS channel_subscriber_count,
    COALESCE(channel_view_count, 0) AS channel_view_count,
    COALESCE(channel_video_count, 0) AS channel_video_count
  FROM `{project}.{raw_dataset}.data_lake_youtube_trending_kr`
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


USER_STATIC_FEATURE = FeatureTableSpec(
    name="user_static_feature",
    entity_keys=("user_id",),
    columns=(
        "user_id",
        "event_timestamp",
        "age_group",
        "occupation",
        "preferred_category",
        "preferred_topics",
        "watch_time_band",
    ),
    select_sql=_USER_STATIC_SELECT,
)
USER_DYNAMIC_FEATURE = FeatureTableSpec(
    name="user_dynamic_feature",
    entity_keys=("user_id",),
    columns=(
        "user_id",
        "event_timestamp",
        "recent_click_count_7d",
        "recent_view_count_7d",
        "recent_watch_time_7d",
        "recent_like_count_7d",
        "historical_category_affinity",
        "total_event_count_7d",
    ),
    select_sql=_USER_DYNAMIC_SELECT,
)
VIDEO_FEATURE = FeatureTableSpec(
    name="video_feature",
    entity_keys=("video_id",),
    columns=(
        "video_id",
        "event_timestamp",
        "category_id",
        "duration_sec",
        "view_count",
        "like_ratio",
        "comment_ratio",
        "days_since_upload",
        "channel_subscriber_count",
        "channel_view_count",
        "channel_video_count",
    ),
    select_sql=_VIDEO_SELECT,
)

# user_category_similarity는 여기서 만들지 않는다. 원본인 user_topic_embedding /
# category_embedding artifact 테이블을 적재하는 배치가 아직 없어 SQL만으로는
# 재구축할 수 없다.
FEATURE_TABLES: tuple[FeatureTableSpec, ...] = (
    USER_STATIC_FEATURE,
    USER_DYNAMIC_FEATURE,
    VIDEO_FEATURE,
)
_TABLES_BY_NAME = {spec.name: spec for spec in FEATURE_TABLES}


def _table_list(value: str) -> list[str]:
    tables = [item.strip() for item in value.split(",")]
    if not tables or any(not item for item in tables):
        raise argparse.ArgumentTypeError("must be a comma-separated table list")
    return tables


def _boolean(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise argparse.ArgumentTypeError("must be true or false")


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        action="version",
        version=json.dumps(
            {
                "application_revision": _REVISION,
                "contract_version": BATCH_CONTRACT_VERSION,
            },
            sort_keys=True,
        ),
    )
    parser.add_argument(
        "--project",
        default=os.getenv("CTR_TRAINING_BQ_PROJECT", DEFAULT_PROJECT),
    )
    parser.add_argument(
        "--dataset",
        default=os.getenv("CTR_TRAINING_BQ_DATASET", DEFAULT_DATASET),
        help="feature 계층 dataset (Feast source 테이블 대상)",
    )
    parser.add_argument(
        "--raw-dataset",
        default=os.getenv("CTR_TRAINING_BQ_RAW_DATASET", DEFAULT_RAW_DATASET),
        help="raw 계층 dataset (data_lake_* 원본 테이블)",
    )
    parser.add_argument(
        "--location",
        default=os.getenv("CTR_TRAINING_BQ_LOCATION", DEFAULT_LOCATION),
    )
    parser.add_argument("--tables", type=_table_list)
    parser.add_argument(
        "--dry-run",
        nargs="?",
        const=True,
        default=False,
        type=_boolean,
        help="BigQuery dry-run으로 SQL만 검증하고 적재하지 않는다",
    )
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    for name in ("project", "dataset", "raw_dataset", "location"):
        if not str(getattr(args, name)).strip():
            raise BatchArgumentError(
                f"--{name.replace('_', '-')} must not be empty"
            )
    if args.dataset == args.raw_dataset:
        raise BatchArgumentError(
            "--dataset and --raw-dataset must point to different datasets"
        )
    unknown = sorted(set(args.tables or ()) - set(_TABLES_BY_NAME))
    if unknown:
        raise BatchArgumentError(f"unknown feature tables: {', '.join(unknown)}")


def selected_specs(tables: Sequence[str] | None) -> tuple[FeatureTableSpec, ...]:
    """``--tables`` 인자를 선언 순서를 유지한 spec 목록으로 변환한다."""

    if not tables:
        return FEATURE_TABLES
    requested = set(tables)
    return tuple(spec for spec in FEATURE_TABLES if spec.name in requested)


def table_fqn(spec: FeatureTableSpec, *, project: str, dataset: str) -> str:
    return f"`{project}.{dataset}.{spec.name}`"


def build_rebuild_sql(
    spec: FeatureTableSpec, *, project: str, dataset: str, raw_dataset: str
) -> str:
    """대상 테이블 스키마를 보존하는 TRUNCATE + INSERT 스크립트를 만든다."""

    select_sql = spec.select_sql.format(
        project=project, dataset=dataset, raw_dataset=raw_dataset
    )
    target = table_fqn(spec, project=project, dataset=dataset)
    columns = ",\n  ".join(spec.columns)
    return (
        f"TRUNCATE TABLE {target};\n"
        f"INSERT INTO {target} (\n  {columns}\n)\n"
        f"{select_sql.rstrip().rstrip(';')};\n"
    )


def build_validation_sql(
    spec: FeatureTableSpec, *, project: str, dataset: str
) -> str:
    """적재 결과를 비어있음·NULL 키·중복 키 3가지 기준으로 검사하는 SQL."""

    target = table_fqn(spec, project=project, dataset=dataset)
    key_columns = (*spec.entity_keys, "event_timestamp")
    null_predicate = " OR ".join(f"{column} IS NULL" for column in key_columns)
    key_tuple = ", ".join(key_columns)
    return f"""\
WITH loaded AS (
  SELECT
    COUNT(*) AS row_count,
    COUNTIF({null_predicate}) AS null_key_count,
    COUNT(*) - COUNT(DISTINCT TO_JSON_STRING(STRUCT({key_tuple}))) AS duplicate_key_count
  FROM {target}
)
SELECT
  IF(row_count = 0,
     ERROR('validation failed: {spec.name} is empty'),
     'ok') AS non_empty_check,
  IF(null_key_count > 0,
     ERROR(FORMAT(
       'validation failed: %d rows with NULL key columns in {spec.name}',
       null_key_count)),
     'ok') AS key_not_null_check,
  IF(duplicate_key_count > 0,
     ERROR(FORMAT(
       'validation failed: %d duplicate key rows in {spec.name}',
       duplicate_key_count)),
     'ok') AS unique_key_check
FROM loaded
"""


def _client(project: str, location: str) -> "bigquery.Client":
    from google.cloud import bigquery

    return bigquery.Client(project=project, location=location)


def _run_query(
    client: "bigquery.Client", sql: str, *, location: str, dry_run: bool
) -> Any:
    from google.cloud import bigquery

    job_config = bigquery.QueryJobConfig(
        dry_run=dry_run, use_query_cache=not dry_run
    )
    job = client.query(sql, job_config=job_config, location=location)
    if not dry_run:
        job.result()
    return job


def _run(args: argparse.Namespace) -> dict[str, object]:
    specs = selected_specs(args.tables)
    client = _client(args.project, args.location)
    built: list[str] = []
    for spec in specs:
        rebuild_sql = build_rebuild_sql(
            spec,
            project=args.project,
            dataset=args.dataset,
            raw_dataset=args.raw_dataset,
        )
        _run_query(
            client, rebuild_sql, location=args.location, dry_run=args.dry_run
        )
        validation_sql = build_validation_sql(
            spec, project=args.project, dataset=args.dataset
        )
        _run_query(
            client, validation_sql, location=args.location, dry_run=args.dry_run
        )
        built.append(spec.name)
        logger.info("rebuilt feature table %s", spec.name)
    return {
        "status": "succeeded",
        "mode": "dry_run" if args.dry_run else "rebuild",
        "project": args.project,
        "dataset": args.dataset,
        "raw_dataset": args.raw_dataset,
        "tables": built,
    }


def _emit(payload: dict[str, object]) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        flush=True,
    )


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
        logger.error("Invalid feature_store_build arguments: %s", exc)
        _emit(
            _summary(
                status="failed", details={"error_type": "invalid_arguments"}
            )
        )
        return 2

    try:
        result = dict(_run(args))
    except BatchArgumentError as exc:
        logger.error("Invalid feature_store_build arguments: %s", exc)
        _emit(
            _summary(
                status="failed", details={"error_type": "invalid_arguments"}
            )
        )
        return 2
    except Exception as exc:  # noqa: BLE001 - process boundary maps failures to exit 1
        logger.error("feature_store_build failed (%s)", type(exc).__name__)
        _emit(
            _summary(status="failed", details={"error_type": "runtime_failure"})
        )
        return 1

    status = str(result.pop("status", "succeeded"))
    _emit(_summary(status=status, details=result))
    return 0


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    raise SystemExit(main())
