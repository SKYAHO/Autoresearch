"""draft parquet에서 목표 CTR에 맞는 click_threshold를 추천하는 공개 batch 명령."""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from typing import Sequence

from autoresearch.action_logs.calibration import recommend_click_threshold
from autoresearch.action_logs.pipeline import read_action_log_draft_parquet
from autoresearch.action_logs.schema import ImpressionDraft

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft-path", required=True)
    parser.add_argument("--target-ctr", type=float, required=True)
    return parser


def _per_user_max(drafts: list[ImpressionDraft]) -> tuple[list[float], int]:
    best: dict[str, float] = defaultdict(float)
    impressions = 0
    for d in drafts:
        impressions += 1
        if d.click_propensity > best[d.user_id]:
            best[d.user_id] = d.click_propensity
    return list(best.values()), impressions


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        drafts = read_action_log_draft_parquet(args.draft_path)
        per_user_max, impressions = _per_user_max(drafts)
        rec = recommend_click_threshold(per_user_max, impressions, args.target_ctr)
        payload = {
            "status": "succeeded",
            "recommended_threshold": rec.recommended_threshold,
            "achieved_ctr": rec.achieved_ctr,
            "target_ctr": rec.target_ctr,
            "users": rec.users,
            "impressions": rec.impressions,
            "per_user_max_quantiles": dict(rec.per_user_max_quantiles),
            "sweep": [list(row) for row in rec.sweep],
        }
    except ValueError as exc:
        # recommend_click_threshold만 ValueError를 던진다(안전한 curated 메시지).
        # Pydantic v2 ValidationError는 ValueError를 상속하지 않고,
        # FileNotFoundError 등은 OSError 계열이므로 이 except는 오직
        # recommend_click_threshold의 입력 검증 실패만 잡는다 — 경로 등
        # 민감 정보가 없어 메시지를 그대로 노출해도 안전하다.
        logger.error("calibration rejected input: %s", exc)
        print(
            json.dumps(
                {"status": "failed", "error_type": "ValueError", "error": str(exc)},
                ensure_ascii=False,
            )
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - process boundary maps failures to exit 1
        # 예상치 못한 실패(파일 IO, 스키마 등) — 경로 등 민감 정보가 섞일 수
        # 있으므로 메시지는 노출하지 않고 타입만 남긴다.
        logger.error("click-threshold calibration failed (%s)", type(exc).__name__)
        print(
            json.dumps(
                {"status": "failed", "error_type": type(exc).__name__},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
