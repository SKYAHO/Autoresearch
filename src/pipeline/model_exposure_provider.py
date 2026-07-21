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

import pandas as pd
from google.cloud import bigquery

from autoresearch.action_logs.pipeline import CandidateProvider, ExposureMetadata

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RankedVideo:
    """user_recommendations 1행 — 유저별 모델 순위 항목."""

    video_id: str
    rank: int
    ctr_score: float | None


@dataclass(frozen=True, slots=True)
class RankingsPartition:
    """dt 파티션 1개의 유저별 모델 순위 + 계보."""

    by_user: dict[str, list[RankedVideo]]
    model_run_id: str | None


def resolve_recommendations_table_id(table: str | None) -> str:
    """user_recommendations 대상 테이블의 정규화된 id를 만든다(기본값 단일 출처)."""
    import os

    from src.pipeline.build_training_dataset import BIGQUERY_DATASET, BIGQUERY_PROJECT

    resolved = table or os.environ.get(
        "CTR_TRAINING_BQ_RECOMMENDATIONS_TABLE", "user_recommendations"
    )
    return f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}.{resolved}"


def load_user_rankings(
    client: bigquery.Client, table_id: str, dt: date
) -> RankingsPartition:
    """user_recommendations의 dt 파티션을 1회 조회해 유저별 순위 맵으로 만든다.

    파티션이 비면 fail-fast — 휴리스틱 대체는 #222의 명시적 플래그로만 한다.
    """
    query = f"""
    SELECT user_id, video_id, rank, ctr_score, model_run_id
    FROM `{table_id}`
    WHERE dt = '{dt.isoformat()}'
    ORDER BY user_id, rank
    """
    frame = client.query(query).to_dataframe()
    if frame.empty:
        raise RuntimeError(
            f"No user_recommendations rows for dt={dt.isoformat()} in {table_id}"
        )

    run_ids = sorted(set(frame["model_run_id"].dropna().astype(str)))
    if len(run_ids) > 1:
        logger.warning("multiple model_run_id in partition, using lexicographic min: %s", run_ids)
        # 계보-내용 정합: policy_version으로 기록될 run의 행만 사용한다.
        # (#216 배치는 WRITE_TRUNCATE로 파티션당 단일 run을 보장하므로 이 경로는
        # 상류 계약 위반 신호이며, 다른 run의 순위를 섞어 조립하지 않는다.)
        frame = frame[frame["model_run_id"].astype(str) == run_ids[0]]
    model_run_id = run_ids[0] if run_ids else None

    by_user: dict[str, list[RankedVideo]] = {}
    for row in frame.itertuples(index=False):
        by_user.setdefault(str(row.user_id), []).append(
            RankedVideo(
                video_id=str(row.video_id),
                rank=int(row.rank),
                ctr_score=None if pd.isna(row.ctr_score) else float(row.ctr_score),
            )
        )
    return RankingsPartition(by_user=by_user, model_run_id=model_run_id)


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


@dataclass(slots=True)
class ModelExposureRound:
    """provider와 노출 태그 맵의 쌍 — 맵은 provider 호출이 진행되며 채워진다."""

    provider: CandidateProvider
    metadata: dict[tuple[str, str], ExposureMetadata] = field(default_factory=dict)
    model_run_id: str | None = None


def make_model_exposure_provider(
    rankings: RankingsPartition,
    videos: Sequence[dict],
    *,
    candidates_per_user: int = 24,
    personalized_ratio: float = 0.7,
    popular_ratio: float = 0.2,
    exploration_ratio: float = 0.1,
) -> ModelExposureRound:
    """CandidateProvider seam에 주입 가능한 모델 노출 provider를 만든다."""
    if not videos:
        raise RuntimeError("trending videos are required to assemble exposures")

    metadata: dict[tuple[str, str], ExposureMetadata] = {}

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        user_id = str(virtual_user.get("user_id", ""))
        candidates, user_meta = build_model_exposures(
            user_id,
            rankings.by_user.get(user_id, []),
            videos,
            user_rng,
            model_run_id=rankings.model_run_id,
            candidates_per_user=candidates_per_user,
            personalized_ratio=personalized_ratio,
            popular_ratio=popular_ratio,
            exploration_ratio=exploration_ratio,
        )
        metadata.update(user_meta)
        return candidates

    return ModelExposureRound(
        provider=provider, metadata=metadata, model_run_id=rankings.model_run_id
    )
