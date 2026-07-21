"""Feast offline store를 Redis online store로 materialize하는 공개 batch 명령."""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from autoresearch.jobs import BATCH_CONTRACT_VERSION
from feature_repo.bootstrap import ensure_redis_ca_bundle, load_feature_store

logger = logging.getLogger(__name__)
_REVISION = os.getenv("AUTORESEARCH_REVISION", "unknown")
JOB_NAME = "feast_materialize"


class BatchArgumentError(ValueError):
    """공개 batch 명령의 문법·범위 오류."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BatchArgumentError(message)


def _iso_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _view_list(value: str) -> list[str]:
    views = [item.strip() for item in value.split(",")]
    if not views or any(not item for item in views):
        raise argparse.ArgumentTypeError(
            "must be a comma-separated feature view list"
        )
    return views


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
    parser.add_argument("--repo-path", default="feature_repo")
    parser.add_argument("--views", type=_view_list)
    parser.add_argument("--start-ts", type=_iso_datetime)
    parser.add_argument("--end-ts", type=_iso_datetime)
    parser.add_argument(
        "--dry-run",
        nargs="?",
        const=True,
        default=False,
        type=_boolean,
    )
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if (args.start_ts is None) != (args.end_ts is None):
        raise BatchArgumentError(
            "--start-ts and --end-ts must be provided together"
        )
    if args.start_ts is not None and args.start_ts >= args.end_ts:
        raise BatchArgumentError("--start-ts must be earlier than --end-ts")


def _online_client(online_config: Any) -> Any:
    import importlib

    type_path = str(online_config.type)
    if "." not in type_path:
        raise RuntimeError(
            "dry-run requires a custom online store adapter type"
        )
    module_path, class_name = type_path.rsplit(".", 1)
    store_class = getattr(importlib.import_module(module_path), class_name)
    return store_class()._get_client(online_config)


def _dry_run(store: Any) -> dict[str, object]:
    views = sorted(view.name for view in store.list_feature_views())
    client = _online_client(store.config.online_store)
    client.ping()
    return {"mode": "dry_run", "views": views, "redis_ping": True}


def _validate_repo_path(repo_path: str) -> None:
    if not (Path(repo_path).resolve() / "feature_store.yaml").exists():
        raise BatchArgumentError(f"feature_store.yaml not found under {repo_path}")


def _run(args: argparse.Namespace) -> dict[str, object]:
    _validate_repo_path(args.repo_path)
    ensure_redis_ca_bundle()
    store = load_feature_store(args.repo_path)
    if args.dry_run:
        return {"status": "succeeded", **_dry_run(store)}

    registered = {view.name for view in store.list_feature_views()}
    views = args.views or sorted(registered)
    unknown = sorted(set(views) - registered)
    if unknown:
        raise RuntimeError(f"unknown feature views: {', '.join(unknown)}")

    end_ts = args.end_ts or datetime.now(UTC)
    if args.start_ts is not None:
        store.materialize(
            start_date=args.start_ts, end_date=end_ts, feature_views=views
        )
        mode = "range"
    else:
        store.materialize_incremental(end_date=end_ts, feature_views=views)
        mode = "incremental"
    return {
        "status": "succeeded",
        "mode": mode,
        "views": views,
        "start_ts": args.start_ts.isoformat() if args.start_ts else None,
        "end_ts": end_ts.isoformat(),
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
        logger.error("Invalid feast_materialize arguments: %s", exc)
        _emit(
            _summary(
                status="failed", details={"error_type": "invalid_arguments"}
            )
        )
        return 2

    try:
        result = dict(_run(args))
    except BatchArgumentError as exc:
        logger.error("Invalid feast_materialize arguments: %s", exc)
        _emit(
            _summary(
                status="failed", details={"error_type": "invalid_arguments"}
            )
        )
        return 2
    except Exception as exc:  # noqa: BLE001 - process boundary maps failures to exit 1
        logger.error("feast_materialize failed (%s)", type(exc).__name__)
        _emit(
            _summary(
                status="failed", details={"error_type": "runtime_failure"}
            )
        )
        return 1

    status = str(result.pop("status", "succeeded"))
    _emit(_summary(status=status, details=result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
