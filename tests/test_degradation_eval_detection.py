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
    resolve_forward_baseline,
    summarize_valid_roc_auc,
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


def test_derive_hard_retrain_limit_rejects_negative_safety_margin():
    """PR #520 리뷰 이해도 확인 — 음수면 limit_days > elapsed_days가 되고 reason도 None.

    소비자(`#472`)가 잘못된 입력이었음을 알 방법이 없으므로 입력에서 막는다.
    """
    result = _result_with(DegradationPoint(elapsed_days=7, date="2026-07-27"))

    with pytest.raises(ValueError, match="safety_margin_days"):
        derive_hard_retrain_limit(result, safety_margin_days=-3)


def test_derive_hard_retrain_limit_does_not_compute_next_retrain_at():
    # last_trained_at은 이 모듈이 모르는 값이다 — #472가 게이트에서 조합한다.
    limit = derive_hard_retrain_limit(
        _result_with(DegradationPoint(elapsed_days=7, date="2026-07-27")),
        safety_margin_days=2,
    )

    assert not hasattr(limit, "next_retrain_at")


# ============================================================================
# PR #520 리뷰 — summarize_valid_roc_auc 입력 검증과 정렬 전제 (Medium#4, Low#7·#8)
# ============================================================================


def _scored(elapsed: int, roc_auc: float | None, status=EvaluationStatus.VALID):
    return PerDayResult(
        date=f"2026-07-{20 + elapsed:02d}",
        elapsed_days=elapsed,
        status=status,
        roc_auc=roc_auc,
    )


def test_summarize_rejects_non_positive_recent_window():
    """recent_window_days<=0이 조용히 틀린 값을 만들던 결함(리뷰 Medium#4).

    0이면 valid_scores[-0:]가 전체 리스트가 되어 recent==overall이 되고, 음수면
    앞쪽을 잘라낸 나머지 평균이 "최근 성능"으로 나간다 — 둘 다 None도 예외도 아니라
    소비자가 잘못을 알 수 없다.
    """
    days = [_scored(i, 0.70 + i * 0.01) for i in range(5)]

    for bad in (0, -2):
        with pytest.raises(ValueError, match="recent_window_days"):
            summarize_valid_roc_auc(days, recent_window_days=bad)


def test_summarize_excludes_valid_days_without_score():
    # VALID인데 roc_auc가 None인 행은 평균에 못 들어간다(리뷰 Low#7 — 술어 통일).
    days = [_scored(0, 0.80), _scored(1, None), _scored(2, 0.70)]

    overall, recent = summarize_valid_roc_auc(days, recent_window_days=2)

    assert overall == pytest.approx(0.75)
    assert recent == pytest.approx(0.75)


def test_summarize_orders_by_elapsed_days_not_list_order():
    """리뷰 Low#8 — "최근"이 리스트 꼬리가 아니라 elapsed_days 기준이어야 한다.

    JSON 왕복이나 hand-built 결과를 받는 #472/#514 경로에서는 리스트 순서가
    보장되지 않는다.
    """
    shuffled = [_scored(2, 0.70), _scored(0, 0.90), _scored(1, 0.80)]

    overall, recent = summarize_valid_roc_auc(shuffled, recent_window_days=2)

    assert overall == pytest.approx(0.80)
    # elapsed_days 기준 최근 2개는 1일차(0.80)·2일차(0.70)다.
    assert recent == pytest.approx(0.75)


def test_resolve_forward_baseline_orders_by_elapsed_days():
    shuffled = [_scored(2, 0.70), _scored(0, 0.90), _scored(1, 0.80)]

    baseline, source = resolve_forward_baseline(shuffled)

    assert baseline == pytest.approx(0.90)
    assert source == 0


def test_resolve_forward_baseline_skips_valid_day_without_score():
    days = [_scored(0, None), _scored(1, 0.80)]

    baseline, source = resolve_forward_baseline(days)

    assert baseline == pytest.approx(0.80)
    assert source == 1


# ============================================================================
# Task 4 — 판정 엔진에 넘길 원시값 추출 (#485 §5.3)
# degradation_eval은 experiment_evaluation을 import하지 않는다(의존 방향 유지) —
# 값만 뽑아 주고, 신호 계산은 판정 엔진이 한다.
# ============================================================================


def test_temporal_signal_inputs_extracts_primitives():
    from src.pipeline.degradation_eval import temporal_signal_inputs

    days = [_scored(0, 0.80), _scored(1, 0.78), _scored(2, 0.70)]
    result = _result_with(DegradationPoint(elapsed_days=2, date="2026-07-22"))
    result = result.model_copy(
        update={"per_day": days, "recent_roc_auc_mean": 0.74, "recent_window_days": 2}
    )

    inputs = temporal_signal_inputs(result)

    assert inputs == {
        "degradation_elapsed_days": 2,
        "recent_roc_auc_mean": 0.74,
        "valid_day_count": 3,
        "recent_window_days": 2,
    }


def test_temporal_signal_inputs_counts_only_scorable_days():
    from src.pipeline.degradation_eval import temporal_signal_inputs

    days = [_scored(0, 0.80), _scored(1, None), _scored(2, None, EvaluationStatus.MISSING_DATE)]
    result = _result_with(DegradationPoint(reason="no_degradation_detected"))
    result = result.model_copy(update={"per_day": days})

    inputs = temporal_signal_inputs(result)

    assert inputs["valid_day_count"] == 1
    assert inputs["degradation_elapsed_days"] is None
