"""YouTube 과거 parquet을 KR 날짜 파티션으로 적재하는 공개 batch 명령."""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Sequence

from pyarrow.fs import GcsFileSystem

from autoresearch.jobs import BATCH_CONTRACT_VERSION
from autoresearch.youtube_collection.backfill import backfill_from_parquet


logger = logging.getLogger(__name__)
_REVISION = os.getenv("AUTORESEARCH_REVISION", "unknown")


class BatchArgumentError(ValueError):
    """공개 batch 명령의 문법·범위 오류."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BatchArgumentError(message)


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
    parser.add_argument("--source-path", required=True)
    parser.add_argument("--youtube-base-path", required=True)
    parser.add_argument(
        "--overwrite",
        nargs="?",
        const=True,
        default=False,
        type=_overwrite,
        required=True,
        help="required acknowledgement that matching date partitions are replaced",
    )
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    args.source_path = _canonical_gcs_path(args.source_path)
    args.youtube_base_path = _canonical_gcs_path(args.youtube_base_path)
    if not args.overwrite:
        raise BatchArgumentError("--overwrite=true is required for full backfill")


def _run(args: argparse.Namespace) -> dict[str, object]:
    filesystem = GcsFileSystem()
    rows = backfill_from_parquet(
        args.source_path,
        _strip_gs(args.youtube_base_path),
        filesystem=filesystem,
    )
    return {
        "status": "succeeded",
        "rows": rows,
        "output_base_path": args.youtube_base_path,
        "overwrite": True,
    }


def _emit(payload: dict[str, object]) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str), flush=True
    )


def _summary(
    *,
    status: str,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "event": "job_summary",
        "contract_version": BATCH_CONTRACT_VERSION,
        "job": "youtube_backfill",
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
        logger.error("Invalid YouTube backfill arguments: %s", exc)
        _emit(_summary(status="failed", details={"error_type": "invalid_arguments"}))
        return 2

    try:
        result = dict(_run(args))
    except Exception as exc:  # noqa: BLE001 - process boundary maps failures to exit 1
        logger.error("YouTube backfill failed (%s)", type(exc).__name__)
        _emit(_summary(status="failed", details={"error_type": "runtime_failure"}))
        return 1

    status = str(result.pop("status", "succeeded"))
    _emit(_summary(status=status, details=result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
