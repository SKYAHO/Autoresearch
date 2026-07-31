"""다중 시드 반복 학습과 유의성 판정 (#407).

[파이프라인] 학습 구간의 **판정 보조**를 담당한다. 같은 조건을 여러 시드로 반복
학습해 지표의 평균·편차를 재고, baseline과의 차이가 그 편차 범위 안인지 밖인지
판단할 근거를 남긴다.

[왜 필요한가] 학습 파이프라인이 단일 시드 1회 학습만 지원해, 지표 차이가 진짜
개선인지 데이터 분할이 흔들려 나온 노이즈인지 판정할 방법이 없었다. #396은
시드 42 하나로 +0.0019를 재고 임계 +0.002에 0.0001 모자라 기각했는데, 시드가
달랐으면 뒤집혔을 수 있다는 걸 알 방법이 없었다. Capability probe round_002는
실제로 뒤집힌 사례다 — +2.63pp로 채택 판정이 났지만 시드 5개 중 2개만 baseline을
넘었고 평균은 baseline 아래였다.

[기능] 순수 통계 계층(`summarize_metric`, `compare_to_baseline`)과 반복 실행
(`run_seed_sweep`)을 분리한다. 통계 계층은 학습 없이 단위 테스트할 수 있다.

[비책임] 학습 자체는 `src/pipeline/train.py`, 채점은 `src/pipeline/evaluate.py`,
MLflow 좌표는 `src/tracking/namespace.py`가 소유한다. 이 모듈은 판정을 대신하지
않고 **판정 근거만** 만든다 — 채택/기각은 가설의 성공 기준이 정한다.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Optional

# 차이가 노이즈 밖이라고 부르기 위한 배수. 차이의 표준오차 대비 이 값을 넘어야 한다.
# 정규 근사에서 약 95%에 해당하는 관습적 기준이며, 시드 3개로 t 검정을 하기에는
# 표본이 너무 작아 p값 대신 "편차 대비 몇 배인가"를 근거로 남긴다.
NOISE_SIGMA_MULTIPLIER = 2.0
DEFAULT_METRIC_NAME = "val_roc_auc"


@dataclass(frozen=True, slots=True)
class MetricSummary:
    """한 조건을 여러 시드로 돌린 지표 요약.

    Attributes:
        n: 시드 개수.
        mean: 평균.
        std: 표본 표준편차(ddof=1). n이 1이면 NaN — 편차를 잴 수 없다.
        minimum: 최솟값.
        maximum: 최댓값.
    """

    n: int
    mean: float
    std: float
    minimum: float
    maximum: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "mean": self.mean,
            "std": None if math.isnan(self.std) else self.std,
            "min": self.minimum,
            "max": self.maximum,
        }


@dataclass(frozen=True, slots=True)
class SignificanceVerdict:
    """baseline 대비 차이가 노이즈 범위 안인지에 대한 근거.

    Attributes:
        delta: candidate 평균 − baseline 평균.
        standard_error: 차이의 표준오차. 판정 불가면 NaN.
        threshold: 노이즈 밖으로 보기 위한 경계(= 표준오차 × 배수). 판정 불가면 NaN.
        within_noise: 차이가 노이즈 범위 안이면 True, 밖이면 False,
            **판정할 수 없으면 None**(시드가 부족해 편차를 모르는 경우).
        reason: 사람이 읽는 근거 한 줄.
    """

    delta: float
    standard_error: float
    threshold: float
    within_noise: Optional[bool]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "delta": self.delta,
            "standard_error": None
            if math.isnan(self.standard_error)
            else self.standard_error,
            "threshold": None if math.isnan(self.threshold) else self.threshold,
            "within_noise": self.within_noise,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class SeedSweepResult:
    """시드 스윕 1회의 결과."""

    seeds: list[int]
    metrics: list[float]
    summary: MetricSummary
    metric_name: str = DEFAULT_METRIC_NAME

    def to_dict(self) -> dict[str, Any]:
        """이슈 본문 결과 항목에 그대로 옮길 수 있는 형태로 만든다(#407)."""
        return {
            "metric_name": self.metric_name,
            "seeds": list(self.seeds),
            "metrics": list(self.metrics),
            "summary": self.summary.to_dict(),
        }


def summarize_metric(values: Sequence[float]) -> MetricSummary:
    """지표 목록의 평균·표본 표준편차·범위를 낸다.

    시드가 1개면 `std`를 0이 아니라 NaN으로 둔다. 0으로 두면 "노이즈가 전혀 없다"로
    읽혀 어떤 차이든 유의하다고 판정되는데, 실제로는 편차를 재지 못한 것이다.

    Args:
        values: 시드별 지표 값.

    Returns:
        요약 통계.

    Raises:
        ValueError: 값이 하나도 없으면.
    """
    if not values:
        raise ValueError("지표 값이 비었습니다 — 요약할 대상이 없습니다.")
    numbers = [float(v) for v in values]
    return MetricSummary(
        n=len(numbers),
        mean=statistics.fmean(numbers),
        std=statistics.stdev(numbers) if len(numbers) > 1 else float("nan"),
        minimum=min(numbers),
        maximum=max(numbers),
    )


def compare_to_baseline(
    *, candidate: MetricSummary, baseline: MetricSummary
) -> SignificanceVerdict:
    """baseline 대비 차이가 편차 범위 안인지 판단할 근거를 만든다.

    채택/기각을 대신 정하지 않는다 — 가설의 성공 기준이 그 판단을 소유하고,
    이 함수는 "그 차이가 시드를 바꿨을 때도 남는 크기인가"만 답한다.

    Args:
        candidate: 변경군 요약.
        baseline: 대조군 요약.

    Returns:
        차이·표준오차·경계·판정을 담은 근거.
    """
    delta = candidate.mean - baseline.mean

    if candidate.n < 2 or baseline.n < 2:
        return SignificanceVerdict(
            delta=delta,
            standard_error=float("nan"),
            threshold=float("nan"),
            within_noise=None,
            reason=(
                f"시드가 부족해 편차를 잴 수 없습니다"
                f"(candidate n={candidate.n}, baseline n={baseline.n}). "
                "노이즈인지 판정하려면 각 조건을 최소 2개 시드로 돌려야 합니다."
            ),
        )

    standard_error = math.sqrt(
        candidate.std**2 / candidate.n + baseline.std**2 / baseline.n
    )
    threshold = NOISE_SIGMA_MULTIPLIER * standard_error
    within_noise = abs(delta) <= threshold
    return SignificanceVerdict(
        delta=delta,
        standard_error=standard_error,
        threshold=threshold,
        within_noise=within_noise,
        reason=(
            f"Δ={delta:+.4f}, 차이의 표준오차={standard_error:.4f}, "
            f"경계(±{NOISE_SIGMA_MULTIPLIER:g}SE)={threshold:.4f} — "
            + ("편차 범위 안" if within_noise else "편차 범위 밖")
        ),
    )


def run_seed_sweep(
    seeds: Sequence[int],
    *,
    train_once: Callable[..., float],
    metric_name: str = DEFAULT_METRIC_NAME,
    **train_kwargs: Any,
) -> SeedSweepResult:
    """시드 목록으로 같은 조건을 반복 학습하고 지표를 모은다.

    Args:
        seeds: 반복할 시드 목록.
        train_once: `random_state` 키워드를 받아 지표 하나를 돌려주는 호출.
        metric_name: 요약할 지표 이름(기록용).
        **train_kwargs: `train_once`에 그대로 전달할 인자.

    Returns:
        시드별 지표와 요약.

    Raises:
        ValueError: 시드 목록이 비었거나 중복이 있으면.
    """
    if not seeds:
        raise ValueError("시드 목록이 비었습니다 — 반복할 대상이 없습니다.")
    if len(set(seeds)) != len(seeds):
        raise ValueError(
            f"시드 목록에 중복이 있습니다: {list(seeds)}. "
            "같은 시드는 같은 결과를 내므로 편차가 인위적으로 작아집니다."
        )

    metrics: list[float] = []
    for seed in seeds:
        metrics.append(float(train_once(random_state=seed, **train_kwargs)))

    return SeedSweepResult(
        seeds=list(seeds),
        metrics=metrics,
        summary=summarize_metric(metrics),
        metric_name=metric_name,
    )
