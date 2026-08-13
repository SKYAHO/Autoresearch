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
유한하지 않은 지표(NaN/inf)는 두 계층 모두에서 거부한다 — 흘려보내면 평균·편차가
조용히 NaN이 되고 판정이 "노이즈 안/밖"이 아니라 아무 말도 못 하게 된다(#445).

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

DEFAULT_METRIC_NAME = "val_roc_auc"

# 양측 95% t 임계값(자유도별). 시드 3개면 자유도 2라 임계값이 4.303으로, 정규
# 근사의 1.96보다 훨씬 크다. 2.0 같은 고정 배수를 쓰면 작은 표본에서 "노이즈 밖"을
# 실제보다 자주 선언한다 — 거짓 채택을 막으려는 모듈이 관대한 쪽으로 기우는 셈이라
# 자유도에 맞는 값을 쓴다(#407 리뷰 3).
#
# df 1~30을 빈칸 없이 채운다. 띄엄띄엄 두면 표에 없는 자유도마다 근사가 끼는데,
# 그 근사는 `reason` 문자열에 "경계(±t2.228, df=14)"처럼 **틀린 t값이 df와 짝지어**
# 기록되는 형태로 남는다. 시드 12~14개, 16~19개는 현실적으로 자주 쓰는 수라
# 채우는 편이 낫다(#456 리뷰 1).
_T_CRITICAL_95 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    11: 2.201,
    12: 2.179,
    13: 2.160,
    14: 2.145,
    15: 2.131,
    16: 2.120,
    17: 2.110,
    18: 2.101,
    19: 2.093,
    20: 2.086,
    21: 2.080,
    22: 2.074,
    23: 2.069,
    24: 2.064,
    25: 2.060,
    26: 2.056,
    27: 2.052,
    28: 2.048,
    29: 2.045,
    30: 2.042,
}
# 자유도 1(시드 2개)이 최소다. 그보다 작으면 호출부 계약이 깨진 것이라 조용히
# 흡수하지 않고 세운다 — 이 모듈은 성립할 수 없는 입력을 전부 ValueError로 막는다
# (`summarize_metric`의 NaN, `validate_seeds`의 중복 시드와 같은 방침).
_T_CRITICAL_SMALLEST_DF = min(_T_CRITICAL_95)
# 표를 넘는 자유도(df > 30)는 이 항목을 하한으로 유지한다. t는 자유도 ∞에서 1.96으로
# 수렴하므로 2.042를 계속 쓰면 오차가 4.19%로 **수렴**하고(발산하지 않는다) 방향은
# 항상 보수적이다. 반대로 1.96으로 낮추면 df 31~60 구간에서 실제보다 관대해진다.
_T_CRITICAL_LARGEST_DF = max(_T_CRITICAL_95)


def _t_critical_95(degrees_of_freedom: int) -> float:
    """자유도에 맞는 양측 95% t 임계값.

    df 1~30은 표에서 **정확히** 조회된다 — 이 구간에 근사는 없다. 표를 넘는
    자유도만 가장 보수적인 항목(df=30, 2.042)을 하한으로 쓴다. t는 자유도가
    커질수록 작아지므로 이 하한은 늘 참값 이상이고, 오차는 df→∞에서 4.19%로
    수렴한다(발산하지 않는다).

    표 1~30이 연속이라는 것이 이 함수의 전제다. 그 전제는 코드가 아니라
    `test_t_critical_table_covers_realistic_seed_counts_without_approximating`가
    지킨다 — 빈칸을 메꾸는 분기를 코드에 두면 어떤 입력으로도 실행되지 않는
    죽은 코드가 된다(#456 리뷰 2).

    Raises:
        ValueError: 자유도가 1 미만이면. 호출부가 시드 2개 이상을 보장하므로
            도달할 수 없고, 도달했다면 그 계약이 깨진 것이다.
    """
    if degrees_of_freedom < _T_CRITICAL_SMALLEST_DF:
        raise ValueError(
            f"자유도가 {degrees_of_freedom}입니다 — 최소 자유도는 "
            f"{_T_CRITICAL_SMALLEST_DF}(시드 2개)이므로 호출부 계약이 깨졌습니다."
        )
    if degrees_of_freedom in _T_CRITICAL_95:
        return _T_CRITICAL_95[degrees_of_freedom]
    return _T_CRITICAL_95[_T_CRITICAL_LARGEST_DF]


def t_critical_95(degrees_of_freedom: int) -> float:
    """양측 95% t 신뢰구간에 실제 적용할 임계값을 반환한다.

    통계 결과를 다른 immutable evidence 계약에 옮길 때도 표준오차가 0인 경우를
    포함해 적용값을 정확히 기록하도록, 내부 표 조회를 공개한다.
    """
    return _t_critical_95(degrees_of_freedom)


def _t_critical_note(degrees_of_freedom: int) -> str:
    """임계값이 표의 하한으로 대체됐으면 그 사실을 `reason`에 남길 꼬리표를 만든다.

    `reason`은 `to_dict()`를 거쳐 이슈 본문에 그대로 옮겨 적힌다. df 1~30은 정확한
    값이지만 df>30은 하한(2.042)이라, 표시가 없으면 읽는 사람이 "df=39의 t는
    2.042"로 오해한다 — 표를 채운 이유와 같은 문제다(#456 리뷰 1).
    """
    return "" if degrees_of_freedom in _T_CRITICAL_95 else " 하한"


class SeedSweepError(RuntimeError):
    """시드 스윕이 중간에 실패했을 때, 이미 끝난 시드 결과를 함께 전달한다(#407)."""

    def __init__(self, message: str, *, completed: dict[int, float]) -> None:
        super().__init__(message)
        self.completed = completed


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
        ValueError: 값이 하나도 없거나, 유한하지 않은 값(NaN/inf)이 섞여 있으면(#445).
    """
    if not values:
        raise ValueError("지표 값이 비었습니다 — 요약할 대상이 없습니다.")
    numbers = [float(v) for v in values]
    # NaN/inf가 섞이면 평균·편차가 조용히 NaN이 되고, 판정도 "노이즈 안/밖"이
    # 아니라 아무 말도 못 하는 결과가 된다(#445).
    non_finite = [
        (index, value) for index, value in enumerate(numbers) if not math.isfinite(value)
    ]
    if non_finite:
        raise ValueError(
            f"유한하지 않은 지표가 있습니다: {non_finite} (위치, 값). "
            "평균·편차가 NaN이 되어 판정이 성립하지 않습니다."
        )
    return MetricSummary(
        n=len(numbers),
        mean=statistics.fmean(numbers),
        std=statistics.stdev(numbers) if len(numbers) > 1 else float("nan"),
        minimum=min(numbers),
        maximum=max(numbers),
    )


def compare_to_baseline(
    *,
    candidate: MetricSummary,
    baseline: MetricSummary,
    paired_deltas: Optional[Sequence[float]] = None,
) -> SignificanceVerdict:
    """baseline 대비 차이가 편차 범위 안인지 판단할 근거를 만든다.

    채택/기각을 대신 정하지 않는다 — 가설의 성공 기준이 그 판단을 소유하고,
    이 함수는 "그 차이가 시드를 바꿨을 때도 남는 크기인가"만 답한다.

    **짝지음(pairing)이 기본이다.** baseline과 candidate를 같은 시드 목록으로 돌리면
    `random_state`가 split과 모델 양쪽에 쓰이므로 시드 42의 두 조건은 같은 분할을
    본다. 이때 분할 노이즈는 짝지어 빼면 상쇄되는데, 독립 표본 식을 쓰면 그 노이즈가
    분모에 그대로 남아 실제보다 둔감해진다 — 두 조건이 같은 방향으로 함께 움직이는
    경우 결론이 갈린다(#407 리뷰 2).

    Args:
        candidate: 변경군 요약.
        baseline: 대조군 요약.
        paired_deltas: 같은 시드끼리 뺀 차이 목록. 주면 짝지은 식을 쓴다.

    Returns:
        차이·표준오차·경계·판정을 담은 근거.
    """
    delta = candidate.mean - baseline.mean

    if paired_deltas is not None:
        n = len(paired_deltas)
        if n < 2:
            return _undecidable(
                delta,
                f"짝지은 시드가 {n}개뿐이라 편차를 잴 수 없습니다. "
                "최소 2개 시드로 두 조건을 모두 돌려야 합니다.",
            )
        paired = summarize_metric(paired_deltas)
        standard_error = paired.std / math.sqrt(n)
        critical = _t_critical_95(n - 1)
        threshold = critical * standard_error
        within_noise = abs(paired.mean) <= threshold
        return SignificanceVerdict(
            delta=paired.mean,
            standard_error=standard_error,
            threshold=threshold,
            within_noise=within_noise,
            reason=(
                f"짝지은 Δ 평균={paired.mean:+.4f}, 표준오차={standard_error:.4f}, "
                f"경계(±t{critical:.3f}{_t_critical_note(n - 1)}, df={n - 1})"
                f"={threshold:.4f} — "
                + ("편차 범위 안" if within_noise else "편차 범위 밖")
            ),
        )

    if candidate.n < 2 or baseline.n < 2:
        return _undecidable(
            delta,
            f"시드가 부족해 편차를 잴 수 없습니다"
            f"(candidate n={candidate.n}, baseline n={baseline.n}). "
            "노이즈인지 판정하려면 각 조건을 최소 2개 시드로 돌려야 합니다.",
        )

    standard_error = math.sqrt(
        candidate.std**2 / candidate.n + baseline.std**2 / baseline.n
    )
    # Welch 근사 대신 보수적으로 작은 쪽 자유도를 쓴다.
    critical = _t_critical_95(min(candidate.n, baseline.n) - 1)
    threshold = critical * standard_error
    within_noise = abs(delta) <= threshold
    return SignificanceVerdict(
        delta=delta,
        standard_error=standard_error,
        threshold=threshold,
        within_noise=within_noise,
        reason=(
            f"Δ={delta:+.4f}, 차이의 표준오차={standard_error:.4f}, "
            f"경계(±t{critical:.3f}{_t_critical_note(min(candidate.n, baseline.n) - 1)})"
            f"={threshold:.4f} — "
            + ("편차 범위 안" if within_noise else "편차 범위 밖")
            + " (짝짓지 않은 비교 — 같은 시드로 두 조건을 돌렸다면 "
            "paired_deltas를 주는 편이 민감합니다)"
        ),
    )


def _undecidable(delta: float, reason: str) -> SignificanceVerdict:
    return SignificanceVerdict(
        delta=delta,
        standard_error=float("nan"),
        threshold=float("nan"),
        within_noise=None,
        reason=reason,
    )


def paired_deltas_by_seed(
    *, candidate: SeedSweepResult, baseline: SeedSweepResult
) -> list[float]:
    """같은 시드끼리 뺀 차이를 만든다(#407 리뷰 2).

    Raises:
        ValueError: 두 스윕의 시드 목록이 다르면. 짝지을 수 없는데 짝지은 척하면
            분할 노이즈가 상쇄된 것처럼 보여 판정이 낙관적으로 기운다.
    """
    if list(candidate.seeds) != list(baseline.seeds):
        raise ValueError(
            f"시드 목록이 다릅니다 — candidate={list(candidate.seeds)}, "
            f"baseline={list(baseline.seeds)}. 짝지은 비교는 두 조건을 "
            "같은 시드로 돌렸을 때만 성립합니다."
        )
    return [c - b for c, b in zip(candidate.metrics, baseline.metrics)]


def validate_seeds(seeds: Sequence[int]) -> None:
    """시드 목록이 반복 학습에 쓸 수 있는지 확인한다.

    호출부가 부수효과(디렉토리 생성 등)를 만들기 **전에** 부를 수 있도록 분리했다.

    Raises:
        ValueError: 비었거나 중복이 있으면.
    """
    if not seeds:
        raise ValueError("시드 목록이 비었습니다 — 반복할 대상이 없습니다.")
    if len(set(seeds)) != len(seeds):
        raise ValueError(
            f"시드 목록에 중복이 있습니다: {list(seeds)}. "
            "같은 시드는 같은 결과를 내므로 편차가 인위적으로 작아집니다."
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
        SeedSweepError: 시드 하나가 실패하거나 유한하지 않은 지표를 돌려주면(#445).
            이미 끝난 시드 지표는 `completed`에 담겨 함께 전달된다.
    """
    validate_seeds(seeds)

    metrics: list[float] = []
    for index, seed in enumerate(seeds):
        try:
            metric = float(train_once(random_state=seed, **train_kwargs))
            if not math.isfinite(metric):
                # TrainingOutcome.val_roc_auc의 기본값이 NaN이라, 학습을 거치지 않은
                # outcome이 섞이면 여기로 들어온다. 요약까지 흘려보내면 평균·편차가
                # 조용히 NaN이 되므로 시드를 지목해 즉시 멈춘다(#445).
                raise ValueError(
                    f"시드 {seed} 학습이 유한하지 않은 지표({metric})를 돌려줬습니다. "
                    "학습이 실제로 수행됐는지, 지표가 계산 가능한 데이터였는지 확인하세요."
                )
            metrics.append(metric)
        except Exception as error:
            # 학습 1회가 비싼 명령이라, 중간에 죽었을 때 앞선 시드 결과까지 버리면
            # 재실행 비용이 크다. 이미 끝난 지표를 메시지에 담아 올린다(#407 리뷰 4).
            done = dict(zip(list(seeds)[:index], metrics))
            raise SeedSweepError(
                f"시드 {seed} 학습이 실패했습니다({index + 1}/{len(seeds)}번째). "
                f"이미 끝난 시드: {done or '없음'}",
                completed=done,
            ) from error

    return SeedSweepResult(
        seeds=list(seeds),
        metrics=metrics,
        summary=summarize_metric(metrics),
        metric_name=metric_name,
    )
