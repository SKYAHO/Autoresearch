"""YouTube와 action-log final partition의 공개 품질 검증 명령."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from collections import Counter
from datetime import date
from typing import Sequence

import pyarrow as pa
import pyarrow.parquet as pq
from pyarrow.fs import GcsFileSystem
from pydantic import ValidationError

from autoresearch.action_logs.pipeline import EVENT_LOG_PARQUET_SCHEMA
from autoresearch.action_logs.schema import EventLog
from autoresearch.jobs import BATCH_CONTRACT_VERSION


logger = logging.getLogger(__name__)
REQUIRED_EVENT_TYPES = ("impression", "click", "view")
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_PARTITION_FILE = "part-0.parquet"
_REVISION = os.getenv("AUTORESEARCH_REVISION", "unknown")


class BatchArgumentError(ValueError):
    """공개 batch 명령의 문법·범위 오류."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BatchArgumentError(message)


def _partition_date(value: str) -> date:
    if _DATE_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a valid calendar date") from exc


def _non_empty(value: str) -> str:
    parsed = value.strip()
    if not parsed:
        raise argparse.ArgumentTypeError("must not be empty")
    return parsed


def _canonical_gcs_path(value: str) -> str:
    if not value.startswith("gs://"):
        raise BatchArgumentError("paths must use gs://bucket/path")
    remainder = value[5:]
    if not remainder or "/" not in remainder or "\\" in remainder:
        raise BatchArgumentError("paths must use gs://bucket/path")
    bucket, object_path = remainder.split("/", 1)
    segments = object_path.split("/")
    if (
        not bucket
        or not object_path
        or any(part in {"", ".", ".."} for part in segments)
    ):
        raise BatchArgumentError(
            "GCS paths must be normalized without empty, . or .. segments"
        )
    return value


def _strip_gs(path: str) -> str:
    return path[5:] if path.startswith("gs://") else path


def _partition_path(base_path: str, partition_date: date) -> str:
    return f"{base_path}/dt={partition_date:%Y-%m-%d}/{_PARTITION_FILE}"


def _video_id(row: dict[str, object]) -> str:
    return str(row.get("video_id") or "")


def _user_id(row: dict[str, object]) -> str:
    return str(row.get("user_id") or "")


def summarize_rows(
    youtube_rows: list[dict[str, object]],
    action_rows: list[dict[str, object]],
    virtual_user_rows: list[dict[str, object]],
) -> dict[str, object]:
    """기존 품질 판정을 유지한 집계만 반환하며 식별자는 노출하지 않는다."""

    youtube_video_ids = [_video_id(row) for row in youtube_rows]
    non_null_youtube_video_ids = [
        video_id for video_id in youtube_video_ids if video_id
    ]
    youtube_video_id_set = set(non_null_youtube_video_ids)

    event_type_counts = Counter(
        str(row.get("event_type") or "") for row in action_rows
    )
    event_type_counts.pop("", None)
    impressions = event_type_counts.get("impression", 0)
    clicks = event_type_counts.get("click", 0)

    action_video_ids = {_video_id(row) for row in action_rows if _video_id(row)}
    missing_video_ids = action_video_ids - youtube_video_id_set
    action_user_ids = {_user_id(row) for row in action_rows if _user_id(row)}
    virtual_user_ids = {_user_id(row) for row in virtual_user_rows if _user_id(row)}
    missing_user_ids = action_user_ids - virtual_user_ids

    return {
        "youtube_rows": len(youtube_rows),
        "youtube_null_video_ids": len(youtube_video_ids)
        - len(non_null_youtube_video_ids),
        "youtube_duplicate_video_ids": len(non_null_youtube_video_ids)
        - len(youtube_video_id_set),
        "action_rows": len(action_rows),
        "event_type_counts": dict(sorted(event_type_counts.items())),
        "ctr": round(clicks / impressions, 6) if impressions else 0.0,
        "llm_models": sorted(
            {
                str(row.get("llm_model"))
                for row in action_rows
                if row.get("llm_model")
            }
        ),
        "action_video_ids_missing_from_youtube": len(missing_video_ids),
        "action_user_ids_missing_from_virtual_users": len(missing_user_ids),
    }


def summarize_final_schema(
    action_rows: list[dict[str, object]],
    action_schema: pa.Schema,
) -> dict[str, object]:
    """Arrow final schema와 EventLog row 불변식을 검증해 count만 반환한다."""

    actual_names = set(action_schema.names)
    missing_columns = sorted(set(EVENT_LOG_PARQUET_SCHEMA.names) - actual_names)
    type_mismatches = sorted(
        field.name
        for field in EVENT_LOG_PARQUET_SCHEMA
        if field.name in actual_names
        and action_schema.field(field.name).type != field.type
    )
    invalid_rows = 0
    event_fields = tuple(EventLog.model_fields)
    for row in action_rows:
        try:
            EventLog.model_validate({name: row.get(name) for name in event_fields})
        except ValidationError:
            invalid_rows += 1
    return {
        "action_schema_missing_columns": missing_columns,
        "action_schema_type_mismatches": type_mismatches,
        "action_schema_invalid_rows": invalid_rows,
    }


def validate_summary(
    summary: dict[str, object],
    *,
    expected_model: str,
) -> list[str]:
    """기존 품질 기준과 final schema 기준의 validation error를 반환한다."""

    errors: list[str] = []
    if int(summary["youtube_rows"]) <= 0:
        errors.append("youtube parquet has no rows")
    if int(summary["youtube_null_video_ids"]) > 0:
        errors.append("youtube parquet has null video_id values")
    if int(summary["youtube_duplicate_video_ids"]) > 0:
        errors.append("youtube parquet has duplicate video_id values")
    if int(summary["action_rows"]) <= 0:
        errors.append("action log parquet has no rows")

    event_type_counts = summary["event_type_counts"]
    if not isinstance(event_type_counts, dict):
        errors.append("event_type_counts is not a dict")
        event_type_counts = {}
    for event_type in REQUIRED_EVENT_TYPES:
        if int(event_type_counts.get(event_type, 0)) <= 0:
            errors.append(f"missing required event_type: {event_type}")

    llm_models = summary["llm_models"]
    if not isinstance(llm_models, list) or expected_model not in llm_models:
        errors.append(f"expected llm_model {expected_model} not found")
    if int(summary["action_video_ids_missing_from_youtube"]) > 0:
        errors.append("action log contains video_id values missing from youtube parquet")
    if int(summary["action_user_ids_missing_from_virtual_users"]) > 0:
        errors.append(
            "action log contains user_id values missing from virtual user parquet"
        )
    if summary["action_schema_missing_columns"]:
        errors.append("action log parquet is missing final schema columns")
    if summary["action_schema_type_mismatches"]:
        errors.append("action log parquet has final schema type mismatches")
    if int(summary["action_schema_invalid_rows"]) > 0:
        errors.append("action log parquet contains invalid EventLog rows")
    return errors


def read_parquet(path: str) -> tuple[list[dict[str, object]], pa.Schema]:
    """GCS parquet 한 파일을 row와 Arrow schema로 읽는다."""

    table = pq.read_table(_strip_gs(path), filesystem=GcsFileSystem())
    return table.to_pylist(), table.schema


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
    parser.add_argument("--partition-date", type=_partition_date, required=True)
    parser.add_argument("--youtube-base-path", required=True)
    parser.add_argument("--virtual-users-path", required=True)
    parser.add_argument("--action-log-base-path", required=True)
    parser.add_argument("--expected-model", type=_non_empty, required=True)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    for name in (
        "youtube_base_path",
        "virtual_users_path",
        "action_log_base_path",
    ):
        setattr(args, name, _canonical_gcs_path(getattr(args, name)))


def _run(args: argparse.Namespace) -> dict[str, object]:
    youtube_rows, _ = read_parquet(
        _partition_path(args.youtube_base_path, args.partition_date)
    )
    action_rows, action_schema = read_parquet(
        _partition_path(args.action_log_base_path, args.partition_date)
    )
    virtual_user_rows, _ = read_parquet(args.virtual_users_path)
    quality = summarize_rows(youtube_rows, action_rows, virtual_user_rows)
    quality.update(summarize_final_schema(action_rows, action_schema))
    return {
        "quality": quality,
        "errors": validate_summary(quality, expected_model=args.expected_model),
    }


def _emit(payload: dict[str, object]) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str), flush=True
    )


def _summary(
    *,
    status: str,
    partition_date: date | None,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "event": "job_summary",
        "contract_version": BATCH_CONTRACT_VERSION,
        "job": "action_log_quality",
        "status": status,
    }
    if partition_date is not None:
        payload["partition_date"] = partition_date.isoformat()
    if details:
        payload.update(details)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 인자를 검증·실행하고 공개 종료 코드를 반환한다."""

    parser = _build_parser()
    args: argparse.Namespace | None = None
    try:
        args = parser.parse_args(argv)
        _validate_args(args)
    except BatchArgumentError as exc:
        logger.error("Invalid action-log quality arguments: %s", exc)
        _emit(
            _summary(
                status="failed",
                partition_date=getattr(args, "partition_date", None),
                details={"error_type": "invalid_arguments"},
            )
        )
        return 2

    try:
        result = dict(_run(args))
    except Exception as exc:  # noqa: BLE001 - process boundary maps failures to exit 1
        logger.error("Action-log quality check failed (%s)", type(exc).__name__)
        _emit(
            _summary(
                status="failed",
                partition_date=args.partition_date,
                details={"error_type": "runtime_failure"},
            )
        )
        return 1

    errors = result.get("errors")
    status = "failed" if isinstance(errors, list) and errors else "succeeded"
    _emit(
        _summary(
            status=status,
            partition_date=args.partition_date,
            details=result,
        )
    )
    return 1 if status == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
