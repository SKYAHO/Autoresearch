"""user_recommendations 기반 모델 노출 조립 provider.

champion 모델의 유저별 순위(70%) + 트렌딩(20%) + 랜덤(10%)으로 노출 batch를
구성하고, 노출별 정책 태그(ExposureMetadata)를 별도 맵으로 유지한다 — LLM
프롬프트에는 태그·점수를 노출하지 않는다.

spec: docs/specs/2026-07-22-model-exposure-assembly.md
"""

from __future__ import annotations

__arch__ = {
    "stage": "training",
    "role": "champion 모델 순위를 일일 노출 70% 슬라이스로 조립합니다.",
    "owns": [
        "user_recommendations 파티션 리더",
        "70/20/10 노출 조립·정책 태그",
    ],
    "not_owns": [
        "LLM 판정·클릭 정규화",
        "일일 CLI 배선·cutover(#222)",
    ],
}

import logging
import random
from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from google.cloud import bigquery

from autoresearch.action_logs.pipeline import CandidateProvider, ExposureMetadata

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RankedVideo:
    """user_recommendations 1행 — 유저별 모델 순위 항목."""

    video_id: str
    rank: int
    ctr_score: float | None


def build_model_exposures(
    user_id: str,
    ranking: Sequence[RankedVideo],
    videos: Sequence[dict],
    rng: random.Random,
    *,
    model_run_id: str | None,
    candidates_per_user: int = 24,
    personalized_ratio: float = 0.7,
    popular_ratio: float = 0.2,
    exploration_ratio: float = 0.1,
) -> tuple[list[dict], dict[tuple[str, str], ExposureMetadata]]:
    """유저 1명의 노출 batch를 (후보 dict 목록, 태그 맵)으로 조립한다.

    슬롯 산식은 기존 build_candidates와 동일: popular·explore를 round로 뜨고
    나머지가 model. 부족분은 trending → random 순으로 채우되 태그는 실제 소스를
    따른다(모델로 위장 금지 — spec 부족분 규칙).
    """
    videos_by_id = {str(v.get("video_id", "")): v for v in videos if v.get("video_id")}
    if not videos_by_id:
        return [], {}

    n_total = min(candidates_per_user, len(videos_by_id))
    ratio_sum = personalized_ratio + popular_ratio + exploration_ratio
    if ratio_sum <= 0:
        personalized_ratio, popular_ratio, exploration_ratio = 1.0, 0.0, 0.0
        ratio_sum = 1.0
    n_popular = min(round(n_total * popular_ratio / ratio_sum), n_total)
    n_explore = min(round(n_total * exploration_ratio / ratio_sum), n_total - n_popular)
    n_model = n_total - n_popular - n_explore

    # (video, source, model_rank, ctr_score) — 태그는 최종 셔플 후 맵으로 변환
    selected: list[tuple[dict, str, int | None, float | None]] = []
    seen: set[str] = set()

    def take(video: dict, source: str, model_rank: int | None, ctr: float | None) -> bool:
        video_id = str(video.get("video_id", ""))
        if not video_id or video_id in seen:
            return False
        seen.add(video_id)
        selected.append((video, source, model_rank, ctr))
        return True

    skipped_joins = 0
    taken_model = 0
    for item in ranking:
        if taken_model >= n_model:
            break
        video = videos_by_id.get(item.video_id)
        if video is None:
            skipped_joins += 1
            continue
        if take(video, "model", item.rank, item.ctr_score):
            taken_model += 1

    popular_pool = sorted(
        videos_by_id.values(),
        key=lambda v: (-int(v.get("view_count", 0) or 0), str(v.get("video_id", ""))),
    )
    taken_popular = 0
    for video in popular_pool:
        if taken_popular >= n_popular:
            break
        if take(video, "trending", None, None):
            taken_popular += 1

    remaining = [v for v in videos_by_id.values() if str(v.get("video_id", "")) not in seen]
    rng.shuffle(remaining)
    taken_explore = 0
    for video in remaining:
        if taken_explore >= n_explore:
            break
        if take(video, "random", None, None):
            taken_explore += 1

    # 부족분: trending → random 순으로 이어 채움 (태그는 실제 소스)
    for video in popular_pool:
        if len(selected) >= n_total:
            break
        take(video, "trending", None, None)
    leftovers = [v for v in videos_by_id.values() if str(v.get("video_id", "")) not in seen]
    rng.shuffle(leftovers)
    for video in leftovers:
        if len(selected) >= n_total:
            break
        take(video, "random", None, None)

    if skipped_joins:
        logger.warning(
            "model exposure join skipped %d ranked videos absent from trending pool (user_id=%s)",
            skipped_joins,
            user_id,
        )

    rng.shuffle(selected)
    candidates: list[dict] = []
    metadata: dict[tuple[str, str], ExposureMetadata] = {}
    for position, (video, source, model_rank, ctr) in enumerate(selected, start=1):
        video_id = str(video.get("video_id", ""))
        candidates.append(video)
        metadata[(user_id, video_id)] = ExposureMetadata(
            policy="model",
            rank=model_rank if source == "model" else position,
            ctr_score=ctr,
            is_exploration=source == "random",
            policy_version=model_run_id,
            exposure_source=source,  # type: ignore[arg-type]
        )
    return candidates, metadata
