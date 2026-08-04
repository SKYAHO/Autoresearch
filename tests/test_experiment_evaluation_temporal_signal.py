"""temporal signal의 `#425` 다중 신호 판정 연결 테스트 (#485 Task 4, spec §5).

`confidence`/`robustness_note`/`direction_vs_offline_metric`은 **판정 산출물**
(`ExperimentEvaluation`)에 실린다(spec §5.3 안 A). 산출 규칙은 순수 함수라
MLflow·GCS 없이 검증한다.

`summarize_temporal_signal`은 `RollingOriginResult`를 직접 받지 않고 **원시값**을
받는다 — 판정 엔진이 `degradation_eval`(→ `train` → lightgbm)을 import하면 경량이어야
할 판정 경로에 ML 의존이 딸려온다.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.pipeline.experiment_evaluation import (  # noqa: E402
    EvaluationConfidence,
    SignalDirection,
    TemporalSignal,
    summarize_temporal_signal,
)


def _signal(**overrides) -> TemporalSignal:
    """spec §5.1 기준 `high`가 나오는 입력을 기본값으로 둔다."""
    kwargs = dict(
        degradation_elapsed_days=5,
        recent_roc_auc_mean=0.72,
        valid_day_count=6,  # recent_window_days(3) + 2 = 5 이상
        recent_window_days=3,
        offline_primary_delta=None,
        temporal_delta=None,
    )
    kwargs.update(overrides)
    return summarize_temporal_signal(**kwargs)


# ---------------------------------------------------------------------------
# §5.1 confidence
# ---------------------------------------------------------------------------


def test_confidence_high_when_dense_observations_and_late_degradation():
    assert _signal().confidence is EvaluationConfidence.HIGH


def test_confidence_low_when_degradation_detected_within_first_two_days():
    # 열화가 첫 1~2일에 잡혔다 = 곡선을 이루는 표본이 사실상 1~2개 — 통계적으로 불안정.
    assert _signal(degradation_elapsed_days=1).confidence is EvaluationConfidence.LOW
    assert _signal(degradation_elapsed_days=0).confidence is EvaluationConfidence.LOW


def test_confidence_low_when_recent_mean_is_missing():
    # 유효일이 recent_window_days 미만이면 "최근 성능"을 만들지 않는다(§3).
    assert _signal(recent_roc_auc_mean=None).confidence is EvaluationConfidence.LOW


def test_confidence_medium_when_observations_are_sparse():
    # valid_day_count < recent_window_days + 2
    assert _signal(valid_day_count=4).confidence is EvaluationConfidence.MEDIUM


def test_confidence_medium_when_degradation_not_detected():
    # 미탐지는 "안전"이 아니라 판정 근거가 약하다는 뜻이다.
    assert (
        _signal(degradation_elapsed_days=None).confidence is EvaluationConfidence.MEDIUM
    )


def test_low_takes_precedence_over_medium():
    # 두 조건이 동시 성립하면 더 낮은 신뢰도를 택한다 — 신뢰도를 과대평가하지 않는다.
    signal = _signal(degradation_elapsed_days=1, valid_day_count=4)

    assert signal.confidence is EvaluationConfidence.LOW


# ---------------------------------------------------------------------------
# §5.2 direction_vs_offline_metric
# ---------------------------------------------------------------------------


def test_direction_not_applicable_without_offline_delta():
    # 두 조건 비교(#514) 전까지 단일 조건 실행에는 비교 대상 delta가 없다.
    assert _signal().direction_vs_offline_metric is SignalDirection.NOT_APPLICABLE


def test_direction_not_applicable_when_temporal_delta_missing():
    signal = _signal(offline_primary_delta=0.01, temporal_delta=None)

    assert signal.direction_vs_offline_metric is SignalDirection.NOT_APPLICABLE


def test_direction_agree_when_signs_match():
    assert (
        _signal(offline_primary_delta=0.01, temporal_delta=0.02).direction_vs_offline_metric
        is SignalDirection.AGREE
    )
    assert (
        _signal(offline_primary_delta=-0.01, temporal_delta=-0.02).direction_vs_offline_metric
        is SignalDirection.AGREE
    )


def test_direction_disagree_when_signs_differ():
    signal = _signal(offline_primary_delta=0.01, temporal_delta=-0.02)

    assert signal.direction_vs_offline_metric is SignalDirection.DISAGREE


def test_disagree_is_not_a_failure_but_lowers_confidence_and_notes_it():
    """`#425` 완료 조건 — "기각"·"낮은 신뢰도"는 실패가 아닌 정상 종료 경로다."""
    signal = _signal(offline_primary_delta=0.01, temporal_delta=-0.02)

    # 예외를 던지지 않고 정상 반환한다.
    assert signal.direction_vs_offline_metric is SignalDirection.DISAGREE
    assert signal.confidence is not EvaluationConfidence.HIGH
    assert signal.robustness_note is not None
    assert "신호" in signal.robustness_note


def test_robustness_note_records_low_confidence_reason():
    signal = _signal(degradation_elapsed_days=1)

    assert signal.robustness_note is not None
    assert "표본" in signal.robustness_note


def test_robustness_note_is_none_when_nothing_to_flag():
    assert _signal().robustness_note is None


def test_rejects_recent_window_days_below_one():
    """`summarize_valid_roc_auc`와 같은 가드(PR #527 리뷰 Low#3).

    `RollingOriginResult.recent_window_days`에 `ge=1` 제약이 없어 JSON 왕복이나 손으로
    조립한 결과로 0이 들어올 수 있다. 그러면 밀도 임계값이 `valid_day_count < 2`로
    느슨해져 신뢰도가 **올라가는** 방향이라 조용히 넘기면 안 된다.
    """
    import pytest

    with pytest.raises(ValueError, match="recent_window_days"):
        _signal(recent_window_days=0)
