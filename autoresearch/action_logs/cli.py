"""액션 로그 애플리케이션의 Airflow 비종속 실행 진입점입니다."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from typing import Sequence

from pyarrow.fs import GcsFileSystem

from autoresearch.action_logs.daily import (
    merge_daily_action_log_shards,
    run_daily_action_log,
    run_daily_action_log_shard,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("single", "shard", "merge"), required=True)
    parser.add_argument("--partition-date", required=True)
    parser.add_argument("--interval-start", required=True)
    parser.add_argument("--interval-end", required=True)
    parser.add_argument("--youtube-base-path")
    parser.add_argument("--virtual-users-path")
    parser.add_argument("--output-base-path", required=True)
    parser.add_argument("--quarantine-base-path")
    parser.add_argument("--shard-output-base-path")
    parser.add_argument("--shard-quarantine-base-path")
    parser.add_argument("--progress-base-path")
    parser.add_argument("--checkpoint-base-path")
    parser.add_argument("--shard-index", type=int)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--max-users", type=int, default=300)
    parser.add_argument("--generator-name", default="openrouter")
    parser.add_argument("--model-name")
    parser.add_argument("--candidates-per-user", type=int, default=24)
    parser.add_argument("--target-ctr", type=float, default=0.02)
    parser.add_argument("--personalized-ratio", type=float, default=0.7)
    parser.add_argument("--popular-ratio", type=float, default=0.2)
    parser.add_argument("--exploration-ratio", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--chunk-size", type=int, default=0)
    parser.add_argument("--max-quarantine-ratio", type=float, default=0.5)
    parser.add_argument(
        "--filesystem",
        choices=("local", "gcs"),
        default="gcs",
        help="로컬 테스트에서는 local, GKE에서는 gcs를 사용합니다.",
    )
    return parser


def _required(parser: argparse.ArgumentParser, value: str | None, name: str) -> str:
    if value is None or not value.strip():
        parser.error(f"{name} is required for this mode")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    partition_date = datetime.fromisoformat(args.partition_date).date()
    interval_start = datetime.fromisoformat(args.interval_start)
    interval_end = datetime.fromisoformat(args.interval_end)
    filesystem = GcsFileSystem() if args.filesystem == "gcs" else None

    common = {
        "partition_date": partition_date,
        "interval_start": interval_start,
        "interval_end": interval_end,
        "filesystem": filesystem,
    }
    if args.mode == "merge":
        summary = merge_daily_action_log_shards(
            **common,
            shard_count=args.shard_count,
            shard_output_base_path=_required(
                parser, args.shard_output_base_path, "--shard-output-base-path"
            ),
            output_base_path=args.output_base_path,
            shard_quarantine_base_path=args.shard_quarantine_base_path,
            quarantine_base_path=args.quarantine_base_path,
            max_quarantine_ratio=args.max_quarantine_ratio,
        )
    else:
        run_common = {
            **common,
            "youtube_base_path": _required(
                parser, args.youtube_base_path, "--youtube-base-path"
            ),
            "virtual_users_path": _required(
                parser, args.virtual_users_path, "--virtual-users-path"
            ),
            "max_users": args.max_users,
            "quarantine_base_path": args.quarantine_base_path,
            "candidates_per_user": args.candidates_per_user,
            "target_ctr": args.target_ctr,
            "personalized_ratio": args.personalized_ratio,
            "popular_ratio": args.popular_ratio,
            "exploration_ratio": args.exploration_ratio,
            "seed": args.seed,
            "max_concurrency": args.max_concurrency,
            "chunk_size": args.chunk_size,
            "max_quarantine_ratio": args.max_quarantine_ratio,
            "generator_name": args.generator_name,
            "model_name": args.model_name,
        }
        if args.mode == "shard":
            if args.shard_index is None:
                parser.error("--shard-index is required for shard mode")
            summary = run_daily_action_log_shard(
                **run_common,
                shard_index=args.shard_index,
                shard_count=args.shard_count,
                output_base_path=_required(
                    parser, args.shard_output_base_path, "--shard-output-base-path"
                ),
                progress_base_path=args.progress_base_path,
                checkpoint_base_path=args.checkpoint_base_path,
            )
        else:
            summary = run_daily_action_log(
                **run_common,
                output_base_path=args.output_base_path,
            )

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
