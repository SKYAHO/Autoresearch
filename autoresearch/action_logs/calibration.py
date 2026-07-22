"""click_threshold 캘리브레이션 분석 — 목표 CTR에 맞는 커트라인 산출."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ThresholdRecommendation:
    """목표 CTR을 달성하는 커트라인과 진단."""

    recommended_threshold: float
    achieved_ctr: float
    target_ctr: float
    users: int
    impressions: int
    sweep: tuple[tuple[float, float], ...]
    per_user_max_quantiles: Mapping[str, float]


def _quantile(sorted_desc: list[float], q: float) -> float:
    # sorted_desc: 내림차순. q 분위(0~1)에 해당하는 값(가장 가까운 순위).
    if not sorted_desc:
        return 0.0
    asc = sorted_desc[::-1]
    idx = min(len(asc) - 1, max(0, round(q * (len(asc) - 1))))
    return asc[idx]


def recommend_click_threshold(
    per_user_max_propensity: Sequence[float],
    impressions: int,
    target_ctr: float,
) -> ThresholdRecommendation:
    """유저별 최고 click_propensity 분포에서 목표 CTR에 맞는 커트라인을 추천한다.

    per-slate 최대 1클릭이므로 CTR(t) = (max>=t 인 유저 수) / impressions.
    목표 CTR을 위해 n_click=round(target_ctr*impressions)명이 클릭해야 하므로,
    유저별 최고값 내림차순의 n_click번째 값을 커트라인으로 추천한다.
    커트라인 지점에서 값이 동점(tie)이면 동점자 전원이 커트라인을 통과하므로
    `achieved_ctr`이 `target_ctr`을 초과(overshoot)할 수 있다 — 이는 CTR(t) 정의와
    일치하는 정상 동작이며, 호출자는 실제 결과 CTR을 `achieved_ctr`로 확인해야 한다.
    """
    values = sorted(per_user_max_propensity, reverse=True)
    users = len(values)
    if users == 0 or impressions <= 0:
        raise ValueError("per_user_max_propensity must be non-empty and impressions > 0")
    ceiling = users / impressions
    if not 0.0 < target_ctr <= ceiling:
        raise ValueError(f"target_ctr must be in (0, {ceiling:.4f}] (CTR ceiling)")

    n_click = min(users, max(1, round(target_ctr * impressions)))
    threshold = values[n_click - 1]
    clickers = sum(1 for v in values if v >= threshold)
    achieved = round(clickers / impressions, 4)

    candidates = sorted(set(values))
    sweep = tuple(
        (round(t, 4), round(sum(1 for v in values if v >= t) / impressions, 4))
        for t in candidates
    )
    quantiles = {
        f"p{int(q * 100)}": round(_quantile(values, q), 4)
        for q in (0.5, 0.75, 0.9, 0.95, 0.99)
    }
    return ThresholdRecommendation(
        recommended_threshold=threshold,
        achieved_ctr=achieved,
        target_ctr=target_ctr,
        users=users,
        impressions=impressions,
        sweep=sweep,
        per_user_max_quantiles=quantiles,
    )
