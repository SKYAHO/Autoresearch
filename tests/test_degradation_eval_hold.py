"""fail-closed `hold` 종료 조건 테스트 (#485 §6, plan Task 3).

통계 추정 없이 멈춰야 하는 상태를 판정한다. `condition_mismatch`(두 조건의 cutoff·
window·horizon·snapshot·split·seed 불일치)는 두 조건 비교가 전제라 `#514` 소관이므로
여기서 다루지 않는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.pipeline.degradation_eval import (  # noqa: E402
    DegradationPoint,
    EvaluationStatus,
    PerDayResult,
    RollingOriginResult,
    TemporalHoldReason,
    evaluate_temporal_hold,
)
from src.pipeline.training_provenance import (  # noqa: E402
    DatasetColumn,
    TrainingSnapshotManifest,
)

CUTOFF = "2026-07-20"


def _manifest(*, events_end_date: str = "2026-07-19") -> TrainingSnapshotManifest:
    return TrainingSnapshotManifest(
        dataset_sha256="0" * 64,
        schema_sha256="1" * 64,
        row_count=10,
        columns=[DatasetColumn(name="clicked", dtype="int64")],
        created_at="2026-07-20T00:00:00Z",
        events_start_date="2026-07-17",
        events_end_date=events_end_date,
        feature_service="ctr_training_v1",
        registry_uri="gs://fake/registry.db",
        registry_generation="1",
        registry_sha256="2" * 64,
    )


def _day(elapsed: int, status: EvaluationStatus, roc_auc: float | None) -> PerDayResult:
    return PerDayResult(
        date=f"2026-07-{20 + elapsed:02d}",
        elapsed_days=elapsed,
        status=status,
        roc_auc=roc_auc,
    )


def _result(
    *,
    per_day: list[PerDayResult],
    horizon_days: int,
    events_end_date: str = "2026-07-19",
) -> RollingOriginResult:
    return RollingOriginResult(
        cutoff_date=CUTOFF,
        window_days=3,
        horizon_days=horizon_days,
        baseline_val_roc_auc=0.80,
        forward_baseline_roc_auc=0.76,
        forward_baseline_source=0,
        min_auc_drop=0.05,
        per_day=per_day,
        degradation_point=DegradationPoint(reason="no_degradation_detected"),
        training_snapshot_manifest=_manifest(events_end_date=events_end_date),
    )


def _valid_days(count: int) -> list[PerDayResult]:
    return [_day(i, EvaluationStatus.VALID, 0.75) for i in range(count)]


def test_healthy_result_is_not_held():
    result = _result(per_day=_valid_days(3), horizon_days=3)

    assert evaluate_temporal_hold(result) is None


def test_missing_evidence_is_held():
    assert evaluate_temporal_hold(None) is TemporalHoldReason.TEMPORAL_EVIDENCE_MISSING


def test_insufficient_valid_points_is_held():
    # 유효일 1개 < 2 — 곡선을 만들 수 없다.
    per_day = [
        _day(0, EvaluationStatus.VALID, 0.75),
        _day(1, EvaluationStatus.MISSING_DATE, None),
        _day(2, EvaluationStatus.MISSING_DATE, None),
    ]

    result = _result(per_day=per_day, horizon_days=3)

    assert (
        evaluate_temporal_hold(result)
        is TemporalHoldReason.TEMPORAL_INSUFFICIENT_VALID_POINTS
    )


def test_incomplete_horizon_is_held():
    # horizon_days=5를 요청했는데 관측이 3일치뿐 — 미래 구간이 잘렸다.
    result = _result(per_day=_valid_days(3), horizon_days=5)

    assert evaluate_temporal_hold(result) is TemporalHoldReason.TEMPORAL_HORIZON_INCOMPLETE


def test_ordering_violation_is_held():
    # events_end_date가 cutoff 당일이면 학습이 평가 구간을 봤다는 뜻이다.
    # 선행 spec §2.1이 cutoff-1로 고정하므로 정상 경로에선 안 나오는 심층 방어.
    result = _result(per_day=_valid_days(3), horizon_days=3, events_end_date=CUTOFF)

    assert evaluate_temporal_hold(result) is TemporalHoldReason.TEMPORAL_ORDERING_VIOLATED


def test_ordering_violation_when_training_window_extends_past_cutoff():
    result = _result(
        per_day=_valid_days(3), horizon_days=3, events_end_date="2026-07-21"
    )

    assert evaluate_temporal_hold(result) is TemporalHoldReason.TEMPORAL_ORDERING_VIOLATED


# ---------------------------------------------------------------------------
# 판정 순서: evidence 부재 → 시간 순서 위반 → 미래 구간 누락 → 데이터 부족.
# 두 조건이 동시에 성립하면 더 근본적인 쪽이 나와야 한다.
# ---------------------------------------------------------------------------


def test_ordering_violation_precedes_incomplete_horizon():
    # 둘 다 성립 — 시간 순서 위반이 더 근본적이다(데이터 자체가 오염됨).
    result = _result(
        per_day=_valid_days(3), horizon_days=5, events_end_date=CUTOFF
    )

    assert evaluate_temporal_hold(result) is TemporalHoldReason.TEMPORAL_ORDERING_VIOLATED


def test_incomplete_horizon_precedes_insufficient_valid_points():
    # 둘 다 성립 — 구간이 잘린 게 원인이고 표본 부족은 그 결과다.
    per_day = [_day(0, EvaluationStatus.VALID, 0.75)]

    result = _result(per_day=per_day, horizon_days=5)

    assert evaluate_temporal_hold(result) is TemporalHoldReason.TEMPORAL_HORIZON_INCOMPLETE


def test_hold_reasons_are_distinct_values():
    # 사유가 뭉개지면 원인 추적이 끊긴다.
    values = {reason.value for reason in TemporalHoldReason}

    assert values == {
        "temporal_insufficient_valid_points",
        "temporal_horizon_incomplete",
        "temporal_ordering_violated",
        "temporal_evidence_missing",
    }
