"""액션 로그 single, shard, merge 공개 batch 명령.

전체 파이프라인 기준으로 이 모듈은 "action log 배치의 공개 CLI 계약" 구간만
담당한다 — 인자 문법·조합 검증과 도메인 러너(`action_logs.daily`) 호출 매핑.
노출 순위의 출처는 `--exposure-source`로 고른다: `model`(BigQuery
`user_recommendations` 파티션), `rerank-api`(Inference Server `/rerank` 실시간
호출, single 전용), `heuristic`(규칙 기반). 노출 조립·클릭 판정·저장 로직은
각각 `src.pipeline`·`autoresearch.action_logs`가 소유하며 여기서 담당하지
않는다.

spec: docs/specs/2026-07-13-public-batch-execution-contract.md,
      docs/specs/2026-07-23-rerank-api-exposure-source.md
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
from datetime import date
from collections.abc import Mapping
from typing import Sequence

from pyarrow.fs import GcsFileSystem

from autoresearch.action_logs.pipeline import CandidateProvider, ExposureMetadata
from autoresearch.action_logs.daily import (
    CandidateProviderFactory,
    merge_daily_action_log_shards,
    run_daily_action_log,
    run_daily_action_log_shard,
)
from autoresearch.action_logs.schema import validate_candidate_ratios
from autoresearch.jobs import BATCH_CONTRACT_VERSION
from autoresearch.jobs._telemetry import configure_action_log_telemetry_logging


logger = logging.getLogger(__name__)
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_REVISION = os.getenv("AUTORESEARCH_REVISION", "unknown")


class BatchArgumentError(ValueError):
    """공개 batch 명령의 문법·범위·조합 오류."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BatchArgumentError(message)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0.0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def _ratio(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("must be a finite number between 0 and 1")
    return parsed


def _overwrite(value: str | bool) -> bool:
    """KPO argument list와 호환되는 명시적 overwrite boolean을 해석한다."""

    if isinstance(value, bool):
        return value
    normalized = value.casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise argparse.ArgumentTypeError("must be true or false")


def _partition_date(value: str) -> date:
    if _DATE_PATTERN.fullmatch(value) is None:
        raise argparse.ArgumentTypeError("must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a valid calendar date") from exc


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
    parser.add_argument("--mode", choices=("single", "shard", "merge"), required=True)
    parser.add_argument("--partition-date", type=_partition_date, required=True)
    parser.add_argument("--youtube-base-path")
    parser.add_argument("--virtual-users-path")
    parser.add_argument("--output-base-path")
    parser.add_argument("--quarantine-base-path")
    parser.add_argument("--shard-output-base-path")
    parser.add_argument("--progress-base-path")
    parser.add_argument("--checkpoint-base-path")
    parser.add_argument("--max-users", type=_positive_int)
    parser.add_argument("--shard-index", type=_non_negative_int)
    parser.add_argument("--shard-count", type=_positive_int)
    parser.add_argument("--generator-name", default="rule_based")
    parser.add_argument("--model-name")
    parser.add_argument("--candidates-per-user", type=_positive_int, default=24)
    parser.add_argument("--click-threshold", type=_ratio)
    parser.add_argument("--personalized-ratio", type=_ratio, default=0.7)
    parser.add_argument("--popular-ratio", type=_ratio, default=0.2)
    parser.add_argument("--exploration-ratio", type=_ratio, default=0.1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-concurrency", type=_positive_int, default=1)
    parser.add_argument("--chunk-size", type=_non_negative_int, default=0)
    parser.add_argument("--max-quarantine-ratio", type=_ratio)
    parser.add_argument(
        "--exposure-source", choices=("model", "heuristic", "rerank-api")
    )
    parser.add_argument("--recommendations-table")
    parser.add_argument("--rerank-url")
    parser.add_argument("--rerank-timeout-sec", type=_positive_float)
    parser.add_argument(
        "--overwrite",
        nargs="?",
        const=True,
        default=False,
        type=_overwrite,
    )
    return parser


def _require(args: argparse.Namespace, *names: str) -> None:
    missing = [name.replace("_", "-") for name in names if getattr(args, name) is None]
    if missing:
        raise BatchArgumentError(
            f"mode={args.mode} requires " + ", ".join(f"--{name}" for name in missing)
        )


def _reject(args: argparse.Namespace, *names: str) -> None:
    supplied = [
        name.replace("_", "-") for name in names if getattr(args, name) is not None
    ]
    if supplied:
        raise BatchArgumentError(
            f"mode={args.mode} does not accept "
            + ", ".join(f"--{name}" for name in supplied)
        )


def _build_candidate_provider_factory(
    args: argparse.Namespace,
) -> CandidateProviderFactory | None:
    """model·rerank-api 소스에서만 src.pipeline을 지연 import해 factory를 만든다.

    heuristic 모드는 None을 반환하며 src·BigQuery·requests에 의존하지 않는다.
    """

    if args.exposure_source == "rerank-api":

        def rerank_factory(
            videos: list[dict],
        ) -> tuple[CandidateProvider, Mapping[tuple[str, str], ExposureMetadata]]:
            from src.pipeline.rerank_api import (
                RerankApiSettings,
                make_rerank_api_exposure_provider,
            )

            round_ = make_rerank_api_exposure_provider(
                RerankApiSettings(
                    base_url=args.rerank_url,
                    timeout_sec=args.rerank_timeout_sec,
                ),
                videos,
                candidates_per_user=args.candidates_per_user,
                personalized_ratio=args.personalized_ratio,
                popular_ratio=args.popular_ratio,
                exploration_ratio=args.exploration_ratio,
            )
            return round_.provider, round_.metadata

        return rerank_factory

    if args.exposure_source != "model":
        return None

    def factory(videos: list[dict]) -> tuple[CandidateProvider, Mapping[tuple[str, str], ExposureMetadata]]:
        from google.cloud import bigquery

        from src.pipeline.build_training_dataset import BIGQUERY_PROJECT
        from src.pipeline.model_exposure_provider import (
            load_user_rankings,
            make_model_exposure_provider,
            resolve_recommendations_table_id,
        )

        table_id = resolve_recommendations_table_id(args.recommendations_table)
        client = bigquery.Client(project=BIGQUERY_PROJECT)
        rankings = load_user_rankings(client, table_id, args.partition_date)
        round_ = make_model_exposure_provider(
            rankings,
            videos,
            candidates_per_user=args.candidates_per_user,
            personalized_ratio=args.personalized_ratio,
            popular_ratio=args.popular_ratio,
            exploration_ratio=args.exploration_ratio,
        )
        return round_.provider, round_.metadata

    return factory


def _validate_args(args: argparse.Namespace) -> None:
    if args.mode in {"single", "shard"}:
        args.exposure_source = args.exposure_source or "model"
        if args.exposure_source != "model" and args.recommendations_table is not None:
            raise BatchArgumentError(
                "--recommendations-table is only valid with "
                "--exposure-source model"
            )
        if args.exposure_source == "rerank-api":
            if args.mode == "shard":
                # shard는 GCS 체크포인트 재실행 구조라 실시간 HTTP 순위의
                # 재현성을 보장할 수 없다(재실행 시 다른 점수) — single 전용.
                raise BatchArgumentError(
                    "--exposure-source rerank-api supports mode=single only"
                )
            _require(args, "rerank_url")
            if args.rerank_timeout_sec is None:
                args.rerank_timeout_sec = 30.0
        else:
            supplied = [
                name.replace("_", "-")
                for name in ("rerank_url", "rerank_timeout_sec")
                if getattr(args, name) is not None
            ]
            if supplied:
                raise BatchArgumentError(
                    ", ".join(f"--{name}" for name in supplied)
                    + " is only valid with --exposure-source rerank-api"
                )
        _require(
            args,
            "youtube_base_path",
            "virtual_users_path",
            "output_base_path",
            "click_threshold",
        )
        if args.max_quarantine_ratio is None:
            args.max_quarantine_ratio = 0.5
        try:
            validate_candidate_ratios(
                args.personalized_ratio,
                args.popular_ratio,
                args.exploration_ratio,
            )
        except ValueError as exc:
            raise BatchArgumentError(str(exc)) from exc
    if args.mode == "shard":
        _require(
            args,
            "shard_index",
            "shard_count",
            "progress_base_path",
            "checkpoint_base_path",
        )
        if args.shard_index >= args.shard_count:
            raise BatchArgumentError("--shard-index must be less than --shard-count")
        _reject(args, "shard_output_base_path")
    elif args.mode == "single":
        _reject(
            args,
            "shard_index",
            "shard_count",
            "shard_output_base_path",
            "progress_base_path",
            "checkpoint_base_path",
        )
    elif args.mode == "merge":
        _require(
            args,
            "shard_count",
            "shard_output_base_path",
            "output_base_path",
            "max_quarantine_ratio",
        )
        _reject(
            args,
            "youtube_base_path",
            "virtual_users_path",
            "quarantine_base_path",
            "progress_base_path",
            "checkpoint_base_path",
            "max_users",
            "shard_index",
            "exposure_source",
            "recommendations_table",
            "rerank_url",
            "rerank_timeout_sec",
            "click_threshold",
        )

    path_names = (
        "youtube_base_path",
        "virtual_users_path",
        "output_base_path",
        "quarantine_base_path",
        "shard_output_base_path",
        "progress_base_path",
        "checkpoint_base_path",
    )
    for name in path_names:
        value = getattr(args, name)
        if value is not None:
            setattr(args, name, _canonical_gcs_path(value))


def _run(args: argparse.Namespace) -> dict[str, object]:
    filesystem = GcsFileSystem()
    if args.mode == "single":
        return run_daily_action_log(
            partition_date=args.partition_date,
            youtube_base_path=args.youtube_base_path,
            virtual_users_path=args.virtual_users_path,
            max_users=args.max_users,
            output_base_path=args.output_base_path,
            quarantine_base_path=args.quarantine_base_path,
            filesystem=filesystem,
            candidates_per_user=args.candidates_per_user,
            click_threshold=args.click_threshold,
            personalized_ratio=args.personalized_ratio,
            popular_ratio=args.popular_ratio,
            exploration_ratio=args.exploration_ratio,
            seed=args.seed,
            max_concurrency=args.max_concurrency,
            chunk_size=args.chunk_size,
            max_quarantine_ratio=args.max_quarantine_ratio,
            generator_name=args.generator_name,
            model_name=args.model_name,
            candidate_provider_factory=_build_candidate_provider_factory(args),
            overwrite=args.overwrite,
        )
    if args.mode == "shard":
        return run_daily_action_log_shard(
            partition_date=args.partition_date,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
            youtube_base_path=args.youtube_base_path,
            virtual_users_path=args.virtual_users_path,
            max_users=args.max_users,
            output_base_path=args.output_base_path,
            quarantine_base_path=args.quarantine_base_path,
            filesystem=filesystem,
            candidates_per_user=args.candidates_per_user,
            click_threshold=args.click_threshold,
            personalized_ratio=args.personalized_ratio,
            popular_ratio=args.popular_ratio,
            exploration_ratio=args.exploration_ratio,
            seed=args.seed,
            max_concurrency=args.max_concurrency,
            chunk_size=args.chunk_size,
            max_quarantine_ratio=args.max_quarantine_ratio,
            generator_name=args.generator_name,
            model_name=args.model_name,
            progress_base_path=args.progress_base_path,
            checkpoint_base_path=args.checkpoint_base_path,
            candidate_provider_factory=_build_candidate_provider_factory(args),
            overwrite=args.overwrite,
        )
    return merge_daily_action_log_shards(
        partition_date=args.partition_date,
        shard_count=args.shard_count,
        shard_output_base_path=args.shard_output_base_path,
        output_base_path=args.output_base_path,
        filesystem=filesystem,
        max_quarantine_ratio=args.max_quarantine_ratio,
        overwrite=args.overwrite,
    )


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
        "job": "action_log",
        "status": status,
    }
    if partition_date is not None:
        payload["partition_date"] = partition_date.isoformat()
    if details:
        payload.update(details)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 인자를 검증·실행하고 공개 종료 코드를 반환한다."""

    configure_action_log_telemetry_logging()
    parser = _build_parser()
    args: argparse.Namespace | None = None
    try:
        args = parser.parse_args(argv)
        _validate_args(args)
    except BatchArgumentError as exc:
        logger.error("Invalid action log batch arguments: %s", exc)
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
    except Exception as exc:  # noqa: BLE001 - process boundary maps runtime failures to exit 1
        logger.error("Action log batch failed (%s)", type(exc).__name__)
        _emit(
            _summary(
                status="failed",
                partition_date=args.partition_date,
                details={"error_type": "runtime_failure"},
            )
        )
        return 1

    warnings = result.pop("warnings", [])
    for warning in warnings if isinstance(warnings, list) else []:
        if isinstance(warning, dict):
            _emit(
                {
                    "contract_version": BATCH_CONTRACT_VERSION,
                    "job": "action_log",
                    "partition_date": args.partition_date.isoformat(),
                    **warning,
                }
            )
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
