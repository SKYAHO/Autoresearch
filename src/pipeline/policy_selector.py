"""model 정책의 노출 선택기 — Reranker 점수 Top-K + exploration 슬롯.

exploration은 폐루프 재학습의 피드백 편향(모델이 좋아하는 것만 노출→학습)을
완화하는 장치다. spec: docs/specs/2026-07-20-policy-simulation-round.md
"""

from __future__ import annotations

__arch__ = {
    "stage": "training",
    "role": "Reranker 점수로 정책별 노출 후보를 선택합니다.",
    "owns": [
        "Top-K exploitation과 exploration 슬롯 배정",
        "결정론적 노출 순위와 Exposure 메타데이터",
    ],
    "not_owns": [
        "Reranker 모델 추론",
        "정책 라운드 이벤트 로그 생성",
    ],
}

import random
from dataclasses import dataclass

from src.serving.schemas import RerankedVideo


@dataclass(frozen=True, slots=True)
class Exposure:
    """정책이 노출하기로 결정한 영상 1건과 로그 태깅용 메타데이터."""

    video_id: str
    rank: int
    ctr_score: float | None
    is_exploration: bool | None


def select_exposures(
    ranked: list[RerankedVideo],
    k: int,
    exploration_ratio: float,
    rng: random.Random,
) -> list[Exposure]:
    """점수 내림차순 ranked에서 exploitation 상위 + 비-Top-K 균등 랜덤 exploration으로
    최대 k개 노출을 뽑는다. rank는 반환 순서 1-base. seed 고정 시 결정론."""
    if k < 1:
        raise ValueError("k must be at least 1")
    if not 0.0 <= exploration_ratio <= 1.0:
        raise ValueError("exploration_ratio must be between 0 and 1")

    n_total = min(k, len(ranked))
    if n_total == len(ranked):
        n_explore = 0  # 전 후보 노출 — exploration이 뽑을 잔여 pool이 없다
    else:
        n_explore = min(round(n_total * exploration_ratio), len(ranked) - n_total)
    n_exploit = n_total - n_explore

    exposures = [
        Exposure(video_id=item.video_id, rank=index + 1, ctr_score=item.ctr_score, is_exploration=False)
        for index, item in enumerate(ranked[:n_exploit])
    ]
    remainder = ranked[n_exploit:]
    for offset, item in enumerate(rng.sample(remainder, n_explore)):
        exposures.append(
            Exposure(
                video_id=item.video_id,
                rank=n_exploit + offset + 1,
                ctr_score=item.ctr_score,
                is_exploration=True,
            )
        )
    return exposures
