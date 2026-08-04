"""degradation_point 판정 테스트 (#471, spec §2.4).

"2개 연속 유효 관측치에서 degraded"는 달력상 연속이 아니라 유효 관측치 순서상
연속이다 — 무효일(결손 등)은 사이에 끼어도 건너뛸 뿐 카운트를 리셋하지 않는다.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.pipeline.degradation_eval import (  # noqa: E402
    EvaluationStatus,
    PerDayResult,
    compute_min_auc_drop,
    detect_degradation_point,
)

BASELINE = 0.80
MIN_AUC_DROP = 0.05  # threshold = 0.75


def _day(elapsed_days: int, status: EvaluationStatus, roc_auc: float | None) -> PerDayResult:
    return PerDayResult(
        date=f"2026-07-{20 + elapsed_days:02d}",
        elapsed_days=elapsed_days,
        status=status,
        roc_auc=roc_auc,
    )


def test_detects_first_two_consecutive_valid_degraded_days():
    per_day = [
        _day(0, EvaluationStatus.VALID, 0.80),
        _day(1, EvaluationStatus.VALID, 0.70),  # degraded
        _day(2, EvaluationStatus.VALID, 0.68),  # degraded — 2번째 연속
        _day(3, EvaluationStatus.VALID, 0.65),
    ]

    result = detect_degradation_point(per_day, baseline=BASELINE, min_auc_drop=MIN_AUC_DROP)

    assert result.elapsed_days == 2
    assert result.date == "2026-07-22"
    assert result.reason is None


def test_missing_date_between_degraded_days_does_not_break_consecutiveness():
    # 월(0)·수(2) degraded, 화(1) missing_date — 무효일을 건너뛰고 수요일에 탐지된다.
    per_day = [
        _day(0, EvaluationStatus.VALID, 0.70),  # degraded
        _day(1, EvaluationStatus.MISSING_DATE, None),
        _day(2, EvaluationStatus.VALID, 0.68),  # degraded — 유효일 기준 2번째 연속
    ]

    result = detect_degradation_point(per_day, baseline=BASELINE, min_auc_drop=MIN_AUC_DROP)

    assert result.elapsed_days == 2
    assert result.reason is None


def test_non_degraded_valid_day_resets_consecutive_count():
    per_day = [
        _day(0, EvaluationStatus.VALID, 0.70),  # degraded
        _day(1, EvaluationStatus.VALID, 0.79),  # 정상 — 리셋
        _day(2, EvaluationStatus.VALID, 0.70),  # degraded (1번째부터 다시)
    ]

    result = detect_degradation_point(per_day, baseline=BASELINE, min_auc_drop=MIN_AUC_DROP)

    assert result.elapsed_days is None
    assert result.reason == "no_degradation_detected"


def test_insufficient_valid_points_when_fewer_than_two_valid_days():
    per_day = [
        _day(0, EvaluationStatus.VALID, 0.70),
        _day(1, EvaluationStatus.MISSING_DATE, None),
        _day(2, EvaluationStatus.SINGLE_CLASS, None),
    ]

    result = detect_degradation_point(per_day, baseline=BASELINE, min_auc_drop=MIN_AUC_DROP)

    assert result.elapsed_days is None
    assert result.date is None
    assert result.reason == "insufficient_valid_points"


def test_no_degradation_detected_when_never_two_consecutive():
    per_day = [
        _day(0, EvaluationStatus.VALID, 0.80),
        _day(1, EvaluationStatus.VALID, 0.79),
        _day(2, EvaluationStatus.VALID, 0.81),
    ]

    result = detect_degradation_point(per_day, baseline=BASELINE, min_auc_drop=MIN_AUC_DROP)

    assert result.elapsed_days is None
    assert result.reason == "no_degradation_detected"


def test_compute_min_auc_drop_uses_floor_when_std_is_small():
    value = compute_min_auc_drop(seed_std=0.001, k=2.0, floor=0.005)

    assert value == pytest.approx(0.005)


def test_compute_min_auc_drop_uses_k_times_std_when_larger_than_floor():
    value = compute_min_auc_drop(seed_std=0.01, k=2.0, floor=0.005)

    assert value == pytest.approx(0.02)
