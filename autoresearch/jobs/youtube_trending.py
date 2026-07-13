"""YouTube 일일 트렌딩 partition 공개 batch 명령."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import UTC, date, datetime
from typing import Mapping, Sequence

from pyarrow.fs import GcsFileSystem

from autoresearch.jobs import BATCH_CONTRACT_VERSION
from autoresearch.youtube_collection.client import ResilientYouTubeClient
from autoresearch.youtube_collection.fetch import collect_trending
from autoresearch.youtube_collection.load import PARTITION_FILE, write_partition


logger = logging.getLogger(__name__)
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_REGION_PATTERN = re.compile(r"[A-Za-z]{2}")
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


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _region_code(value: str) -> str:
    if _REGION_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("must be a two-letter region code")
    return value.upper()


def _overwrite(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise argparse.ArgumentTypeError("must be true or false")


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


def _output_path(base_path: str, partition_date: date) -> str:
    return (
        f"{base_path}/dt={partition_date:%Y-%m-%d}/{PARTITION_FILE}"
    )


def _exists(filesystem: GcsFileSystem, path: str) -> bool:
    info = filesystem.get_file_info(_strip_gs(path))
    file_type = getattr(info, "type", None)
    type_name = getattr(file_type, "name", None) or getattr(info, "type_name", "")
    return type_name != "NotFound"


def _load_api_keys(environment: Mapping[str, str] | None = None) -> list[str]:
    env = os.environ if environment is None else environment
    keys = [
        key.strip()
        for key in env.get("YOUTUBE_API_KEYS", "").split(",")
        if key.strip()
    ]
    if not keys:
        fallback = env.get("YOUTUBE_API_KEY", "").strip()
        if fallback:
            keys = [fallback]
    if not keys:
        raise RuntimeError("YouTube API key environment variable is required")
    return keys


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
    parser.add_argument("--region-code", type=_region_code, default="KR")
    parser.add_argument("--max-results", type=_positive_int, default=200)
    parser.add_argument("--proxy-url")
    parser.add_argument(
        "--overwrite",
        nargs="?",
        const=True,
        default=False,
        type=_overwrite,
    )
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    args.youtube_base_path = _canonical_gcs_path(args.youtube_base_path)


def _run(args: argparse.Namespace) -> dict[str, object]:
    filesystem = GcsFileSystem()
    output_path = _output_path(args.youtube_base_path, args.partition_date)
    if _exists(filesystem, output_path) and not args.overwrite:
        return {
            "status": "skipped",
            "output_path": output_path,
            "videos": 0,
        }

    api_keys = _load_api_keys()
    proxy_url = args.proxy_url or os.getenv("YOUTUBE_PROXY_URL") or None
    callables = ResilientYouTubeClient(
        keys=api_keys,
        proxy_url=proxy_url,
    ).make_callables()
    videos = collect_trending(
        callables.list_videos,
        callables.list_channels,
        callables.list_categories,
        collected_at=datetime.now(UTC),
        region_code=args.region_code,
        max_results=args.max_results,
    )
    write_partition(
        videos,
        _strip_gs(args.youtube_base_path),
        args.partition_date,
        filesystem=filesystem,
    )
    return {
        "status": "succeeded",
        "output_path": output_path,
        "videos": len(videos),
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
        "job": "youtube_trending",
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
        logger.error("Invalid YouTube batch arguments: %s", exc)
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
        logger.error("YouTube batch failed (%s)", type(exc).__name__)
        _emit(
            _summary(
                status="failed",
                partition_date=args.partition_date,
                details={"error_type": "runtime_failure"},
            )
        )
        return 1

    status = str(result.pop("status", "succeeded"))
    _emit(
        _summary(
            status=status,
            partition_date=args.partition_date,
            details=result,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
