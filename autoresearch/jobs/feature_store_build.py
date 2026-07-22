"""data_lake_raw 테이블에서 Feast offline store feature 테이블을 만드는 공개 batch 명령.

BigQuery raw 계층(``data_lake_raw``)의 적재가 끝난 뒤 실행해 feature 계층
(``feast_offline_store``)의 Feast source 테이블에 **대상 날짜 하루치만** 적재한다.
SQL 계약은 ``docs/guides/data-warehouse.md``를 단일 출처로 삼는다.

적재는 ``--partition-date``가 가리키는 행만 ``DELETE``한 뒤 ``INSERT INTO``하는
증분 방식이다. 같은 날짜로 다시 실행하면 그 날짜 행만 다시 만들어지므로 재실행이
멱등하다. ``CREATE OR REPLACE``나 ``WRITE_TRUNCATE``는 Terraform이 소유한 대상
테이블 스키마(REQUIRED/REPEATED mode 포함)를 query 결과 스키마로 교체하므로
사용하지 않는다.

이 명령이 담당하지 않는 인접 책임:

- raw 계층 적재는 ``lake_to_bigquery`` 경로가 담당한다.
- ``user_static_feature``와 ``user_category_similarity``는 날짜 개념이 없는 정적
  feature이며 ``scripts/build_static_features.py``가 소유한다.
- 전체 기간 재계산 경로는 제공하지 않는다. 과거를 다시 만들어야 하면 날짜별로
  이 명령을 반복 실행한다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date, datetime
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
    """feature 테이블 하나를 증분 적재하기 위한 선언.

    ``columns``는 ``INSERT INTO`` 컬럼 목록이자 ``select_sql``의 출력 순서다.
    ``entity_keys`` + ``event_timestamp``는 Feast point-in-time join의 유일
    키이므로 적재 후 중복 검증에 사용한다.

    ``partition_predicate``는 대상 날짜에 해당하는 행을 고르는 ``WHERE`` 조건이다.
    적재 전 ``DELETE``와 적재 후 검증이 같은 조건을 쓰므로, 지우는 범위와 검사하는
    범위가 어긋날 수 없다. ``select_sql``과 함께 ``{partition_date}``를 받는다.
    """

    name: str
    entity_keys: tuple[str, ...]
    columns: tuple[str, ...]
    select_sql: str
    partition_predicate: str


# 대상 날짜 스냅샷의 기준 시각. user_dynamic_feature의 event_timestamp이자 모든
# 룩백 윈도우의 상한(미포함)이다. 스칼라 변수를 쓸 수 없어 SQL 곳곳에 그대로
# 전개된다.
_SNAPSHOT_TS = "TIMESTAMP(DATE '{partition_date}', 'Asia/Seoul')"

# category affinity가 30일을 보므로 raw 스캔 윈도우도 30일이다. 7일 집계는 이 안에
# 포함된다.
_LOOKBACK_DAYS = 30

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
    AND event_timestamp >= TIMESTAMP_SUB(
      TIMESTAMP(DATE '{partition_date}', 'Asia/Seoul'), INTERVAL 30 DAY)
    AND event_timestamp < TIMESTAMP(DATE '{partition_date}', 'Asia/Seoul')
),
video_latest AS (
  SELECT
    video_id,
    video_category
  FROM `{project}.{raw_dataset}.data_lake_youtube_trending_kr`
  WHERE video_id IS NOT NULL
    AND collected_at >= TIMESTAMP_SUB(
      TIMESTAMP(DATE '{partition_date}', 'Asia/Seoul'), INTERVAL 30 DAY)
    AND collected_at < TIMESTAMP(DATE '{partition_date}', 'Asia/Seoul')
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
users AS (
  -- 이미 feature 테이블에 등장한 적 있는 유저는 계속 스냅샷을 받는다. 룩백
  -- 윈도우에 활동이 없어도 아래 LEFT JOIN이 전부 0으로 채우므로, Feast가 오래된
  -- 스냅샷으로 fallback해 stale한 값을 돌려주는 일이 없다. 신규 유저는 두 번째
  -- 항에서 들어온다. DELETE가 먼저 실행되어 대상 날짜 행은 이미 지워진 뒤다.
  SELECT DISTINCT user_id
  FROM `{project}.{dataset}.user_dynamic_feature`
  WHERE user_id IS NOT NULL
  UNION DISTINCT
  SELECT DISTINCT user_id
  FROM action_log
),
user_snapshots AS (
  SELECT
    user_id,
    TIMESTAMP(DATE '{partition_date}', 'Asia/Seoul') AS event_timestamp
  FROM users
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
    AND DATE(collected_at, 'Asia/Seoul') = DATE '{partition_date}'
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
    # 스냅샷 시각이 대상 날짜 KST 자정 한 값이므로 등호로 정확히 짚는다.
    partition_predicate=f"event_timestamp = {_SNAPSHOT_TS}",
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
    # event_timestamp가 collected_at 그대로라 하루 안에 여러 시각이 들어온다.
    # 날짜 단위로 묶어 지운다.
    partition_predicate="DATE(event_timestamp, 'Asia/Seoul') = DATE '{partition_date}'",
)

# 여기서 만들지 않는 feature 테이블:
# - user_static_feature: persona asset이 바뀔 때만 갱신되는 정적 feature라 날짜
#   개념이 없다. scripts/build_static_features.py가 소유한다.
# - user_category_similarity: 원본인 user_topic_embedding / category_embedding
#   artifact 테이블과 함께 같은 스크립트가 소유한다.
FEATURE_TABLES: tuple[FeatureTableSpec, ...] = (
    USER_DYNAMIC_FEATURE,
    VIDEO_FEATURE,
)
_TABLES_BY_NAME = {spec.name: spec for spec in FEATURE_TABLES}


def _table_list(value: str) -> list[str]:
    tables = [item.strip() for item in value.split(",")]
    if not tables or any(not item for item in tables):
        raise argparse.ArgumentTypeError("must be a comma-separated table list")
    return tables


def _partition_date(value: str) -> date:
    """``YYYY-MM-DD``만 허용한다.

    이 값은 SQL 리터럴로 전개되므로, 형식을 여기서 좁혀 두면 주입 가능한 문자열이
    SQL에 닿지 않는다.
    """

    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(
            "must be an ISO date in YYYY-MM-DD form"
        ) from None


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
    parser.add_argument(
        "--partition-date",
        required=True,
        type=_partition_date,
        help="적재할 대상 날짜(KST, YYYY-MM-DD). 이 날짜 행만 지우고 다시 넣는다",
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


def partition_predicate(spec: FeatureTableSpec, *, partition_date: date) -> str:
    """대상 날짜 행을 고르는 WHERE 조건을 만든다."""

    return spec.partition_predicate.format(partition_date=partition_date.isoformat())


def build_incremental_sql(
    spec: FeatureTableSpec,
    *,
    project: str,
    dataset: str,
    raw_dataset: str,
    partition_date: date,
) -> str:
    """대상 날짜 행만 교체하는 DELETE + INSERT 스크립트를 만든다.

    대상 테이블 스키마는 손대지 않는다. 같은 날짜로 다시 실행하면 DELETE가 먼저
    돌아 이전 결과를 걷어내므로 중복 키가 생기지 않는다.
    """

    select_sql = spec.select_sql.format(
        project=project,
        dataset=dataset,
        raw_dataset=raw_dataset,
        partition_date=partition_date.isoformat(),
    )
    target = table_fqn(spec, project=project, dataset=dataset)
    columns = ",\n  ".join(spec.columns)
    predicate = partition_predicate(spec, partition_date=partition_date)
    return (
        f"DELETE FROM {target}\nWHERE {predicate};\n"
        f"INSERT INTO {target} (\n  {columns}\n)\n"
        f"{select_sql.rstrip().rstrip(';')};\n"
    )


def build_validation_sql(
    spec: FeatureTableSpec, *, project: str, dataset: str, partition_date: date
) -> str:
    """이번 run이 적재한 행을 비어있음·NULL 키·중복 키 3가지로 검사하는 SQL.

    검사 범위를 대상 날짜로 한정한다. 이번 run이 만든 결과만 책임지므로 과거
    데이터 문제로 일일 run이 죽지 않고, 테이블이 커져도 스캔량이 늘지 않는다.
    """

    target = table_fqn(spec, project=project, dataset=dataset)
    key_columns = (*spec.entity_keys, "event_timestamp")
    null_predicate = " OR ".join(f"{column} IS NULL" for column in key_columns)
    key_tuple = ", ".join(key_columns)
    predicate = partition_predicate(spec, partition_date=partition_date)
    return f"""\
WITH loaded AS (
  SELECT
    COUNT(*) AS row_count,
    COUNTIF({null_predicate}) AS null_key_count,
    COUNT(*) - COUNT(DISTINCT TO_JSON_STRING(STRUCT({key_tuple}))) AS duplicate_key_count
  FROM {target}
  WHERE {predicate}
)
SELECT
  IF(row_count = 0,
     ERROR('validation failed: {spec.name} has no rows for {partition_date.isoformat()}'),
     'ok') AS non_empty_check,
  IF(null_key_count > 0,
     ERROR(FORMAT(
       'validation failed: %d rows with NULL key columns in {spec.name} for {partition_date.isoformat()}',
       null_key_count)),
     'ok') AS key_not_null_check,
  IF(duplicate_key_count > 0,
     ERROR(FORMAT(
       'validation failed: %d duplicate key rows in {spec.name} for {partition_date.isoformat()}',
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
        incremental_sql = build_incremental_sql(
            spec,
            project=args.project,
            dataset=args.dataset,
            raw_dataset=args.raw_dataset,
            partition_date=args.partition_date,
        )
        _run_query(
            client, incremental_sql, location=args.location, dry_run=args.dry_run
        )
        validation_sql = build_validation_sql(
            spec,
            project=args.project,
            dataset=args.dataset,
            partition_date=args.partition_date,
        )
        _run_query(
            client, validation_sql, location=args.location, dry_run=args.dry_run
        )
        built.append(spec.name)
        logger.info(
            "loaded feature table %s for %s",
            spec.name,
            args.partition_date.isoformat(),
        )
    return {
        "status": "succeeded",
        "mode": "dry_run" if args.dry_run else "incremental",
        "project": args.project,
        "dataset": args.dataset,
        "raw_dataset": args.raw_dataset,
        "partition_date": args.partition_date.isoformat(),
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
