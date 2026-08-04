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
    DegradationPoint,
    EvaluationStatus,
    PerDayResult,
    RollingOriginResult,
    compute_min_auc_drop,
    derive_hard_retrain_limit,
    detect_degradation_point,
)
from src.pipeline.training_provenance import (  # noqa: E402
    DatasetColumn,
    TrainingSnapshotManifest,
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


# ============================================================================
# Task 2 — §4.1·§4.2 hard retrain limit (#485)
# 측정(RollingOriginResult)과 정책(이 함수)을 분리한다. 관측되지 않은 것을
# "안전"으로 바꾸지 않는다 — 미탐지는 값이 아니라 사유로 남는다.
# ============================================================================


def _result_with(degradation_point: DegradationPoint) -> RollingOriginResult:
    """`derive_hard_retrain_limit` 단위 테스트용 최소 결과 객체."""
    manifest = TrainingSnapshotManifest(
        dataset_sha256="0" * 64,
        schema_sha256="1" * 64,
        row_count=10,
        columns=[DatasetColumn(name="clicked", dtype="int64")],
        created_at="2026-07-20T00:00:00Z",
        events_start_date="2026-07-17",
        events_end_date="2026-07-19",
        feature_service="ctr_training_v1",
        registry_uri="gs://fake/registry.db",
        registry_generation="1",
        registry_sha256="2" * 64,
    )
    return RollingOriginResult(
        cutoff_date="2026-07-20",
        window_days=3,
        horizon_days=5,
        baseline_val_roc_auc=0.80,
        forward_baseline_roc_auc=0.76,
        forward_baseline_source=0,
        min_auc_drop=0.05,
        per_day=[],
        degradation_point=degradation_point,
        training_snapshot_manifest=manifest,
    )


def test_derive_hard_retrain_limit_subtracts_safety_margin():
    result = _result_with(DegradationPoint(elapsed_days=7, date="2026-07-27"))

    limit = derive_hard_retrain_limit(result, safety_margin_days=2)

    assert limit.limit_days == 5
    assert limit.reason is None


def test_derive_hard_retrain_limit_no_degradation_yields_none_not_safe():
    """미탐지를 "안전"으로 바꾸지 않는다 — spec §4.1의 핵심 원칙.

    horizon이 짧아서 못 본 것과 실제로 안 꺾인 것은 다른 사실인데, 값으로 채우면
    둘이 같은 숫자가 된다.
    """
    result = _result_with(DegradationPoint(reason="no_degradation_detected"))

    limit = derive_hard_retrain_limit(result, safety_margin_days=2)

    assert limit.limit_days is None
    assert limit.reason == "no_degradation_observed_within_horizon"


def test_derive_hard_retrain_limit_passes_insufficient_valid_points_through():
    # 이 사유는 이름을 바꾸지 않고 그대로 전달한다 — 측정 단계의 사실이 정책 단계에서
    # 다른 말로 바뀌면 원인 추적이 끊긴다.
    result = _result_with(DegradationPoint(reason="insufficient_valid_points"))

    limit = derive_hard_retrain_limit(result, safety_margin_days=2)

    assert limit.limit_days is None
    assert limit.reason == "insufficient_valid_points"


def test_derive_hard_retrain_limit_keeps_two_no_value_reasons_distinct():
    """두 미탐지 사유가 하나로 뭉개지지 않는지 — 뭉개면 원인 구분이 사라진다."""
    no_degradation = derive_hard_retrain_limit(
        _result_with(DegradationPoint(reason="no_degradation_detected")),
        safety_margin_days=2,
    )
    insufficient = derive_hard_retrain_limit(
        _result_with(DegradationPoint(reason="insufficient_valid_points")),
        safety_margin_days=2,
    )

    assert no_degradation.limit_days is None and insufficient.limit_days is None
    assert no_degradation.reason != insufficient.reason


def test_derive_hard_retrain_limit_clamps_negative_to_zero():
    # safety_margin이 열화 시점보다 크면 "이미 지났다" — 음수 일수는 의미가 없다.
    result = _result_with(DegradationPoint(elapsed_days=1, date="2026-07-21"))

    limit = derive_hard_retrain_limit(result, safety_margin_days=3)

    assert limit.limit_days == 0
    assert limit.reason == "safety_margin_exceeds_degradation_point"


def test_derive_hard_retrain_limit_does_not_compute_next_retrain_at():
    # last_trained_at은 이 모듈이 모르는 값이다 — #472가 게이트에서 조합한다.
    limit = derive_hard_retrain_limit(
        _result_with(DegradationPoint(elapsed_days=7, date="2026-07-27")),
        safety_margin_days=2,
    )

    assert not hasattr(limit, "next_retrain_at")
