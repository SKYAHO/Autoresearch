"""src/models/downsampling.py 순수 함수 단위 테스트 (#300).

계약: docs/specs/2026-07-24-negative-downsampling-calibration.md
"""

import numpy as np
import pandas as pd
import pytest

from src.models.downsampling import (
    apply_downsampling_calibration,
    downsample_negatives,
)


# ── apply_downsampling_calibration ─────────────────────────────


def test_calibration_matches_spec_numeric_check():
    # 스펙 결정 2의 수치 검산: 원본 CTR 0.0099를 negative 10%만 남겨(w=0.1)
    # 학습하면 q≈0.0909, 보정하면 원분포 0.0099로 정확 복원돼야 한다.
    q = 0.0909
    p = apply_downsampling_calibration(q, sampling_rate=0.1)
    assert p == pytest.approx(0.0099, abs=1e-4)


def test_calibration_moves_probability_down_when_downsampled():
    # downsampling은 CTR을 실제보다 높게 보이게 하므로 보정은 확률을 내려야 한다.
    q = 0.5
    p = apply_downsampling_calibration(q, sampling_rate=0.1)
    assert p < q


def test_calibration_identity_at_sampling_rate_one():
    # w=1.0이면 항등(보정 없음) — downsampling 미사용/하위호환 경로의 no-op.
    q = np.array([0.01, 0.2, 0.9])
    out = apply_downsampling_calibration(q, sampling_rate=1.0)
    assert out is q  # 그대로 반환


def test_calibration_is_monotonic():
    # 단조증가여야 순위 기반 지표(AUC)가 불변이다(스펙 결정 5).
    q = np.linspace(0.001, 0.999, 50)
    p = apply_downsampling_calibration(q, sampling_rate=0.1)
    assert np.all(np.diff(p) > 0)


def test_calibration_preserves_series_index():
    q = pd.Series([0.0909, 0.5], index=["a", "b"], name="proba")
    p = apply_downsampling_calibration(q, sampling_rate=0.1)
    assert isinstance(p, pd.Series)
    assert list(p.index) == ["a", "b"]
    assert p.name == "proba"
    assert p.loc["a"] == pytest.approx(0.0099, abs=1e-4)


def test_calibration_endpoints_stay_in_bounds():
    # q=0 -> 0, q=1 -> 1 (경계 보존).
    assert apply_downsampling_calibration(0.0, 0.1) == pytest.approx(0.0)
    assert apply_downsampling_calibration(1.0, 0.1) == pytest.approx(1.0)


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.5])
def test_calibration_rejects_out_of_range_rate(bad):
    with pytest.raises(ValueError, match="sampling_rate"):
        apply_downsampling_calibration(0.5, sampling_rate=bad)


def test_calibration_preserves_auc_ranking():
    # 스펙 결정 5: 보정이 monotonic이라 ROC-AUC는 보정 전/후 동일해야 한다.
    from sklearn.metrics import roc_auc_score

    rng = np.random.default_rng(0)
    q = rng.random(500)
    y = (rng.random(500) < q).astype(int)  # 스코어와 상관된 레이블
    calibrated = apply_downsampling_calibration(q, sampling_rate=0.1)
    assert roc_auc_score(y, calibrated) == pytest.approx(roc_auc_score(y, q))


# ── downsample_negatives ───────────────────────────────────────


def _train_frame(n_pos: int, n_neg: int) -> tuple[pd.DataFrame, pd.Series]:
    y = pd.Series([1] * n_pos + [0] * n_neg)
    X = pd.DataFrame({"f": range(n_pos + n_neg)})
    return X, y


def test_downsample_keeps_all_positives_and_fraction_of_negatives():
    X, y = _train_frame(n_pos=100, n_neg=1000)
    X_ds, y_ds, realized = downsample_negatives(X, y, sampling_rate=0.1, random_state=0)
    assert (y_ds == 1).sum() == 100  # positive 전량 유지
    assert (y_ds == 0).sum() == 100  # 1000의 10%
    assert realized == pytest.approx(0.1)


def test_downsample_reports_realized_rate():
    # 라운딩으로 nominal과 실제가 다를 수 있어 실현값을 돌려준다.
    X, y = _train_frame(n_pos=10, n_neg=333)
    _, y_ds, realized = downsample_negatives(X, y, sampling_rate=0.1, random_state=0)
    kept_neg = int((y_ds == 0).sum())
    assert realized == pytest.approx(kept_neg / 333)


def test_downsample_identity_at_rate_one():
    X, y = _train_frame(n_pos=5, n_neg=50)
    X_ds, y_ds, realized = downsample_negatives(X, y, sampling_rate=1.0)
    assert realized == 1.0
    assert len(y_ds) == 55


def test_downsample_is_deterministic_with_seed():
    X, y = _train_frame(n_pos=20, n_neg=500)
    a = downsample_negatives(X, y, sampling_rate=0.2, random_state=7)[1]
    b = downsample_negatives(X, y, sampling_rate=0.2, random_state=7)[1]
    assert list(a.index) == list(b.index)


def test_downsample_preserves_original_row_order():
    X, y = _train_frame(n_pos=5, n_neg=50)
    _, y_ds, _ = downsample_negatives(X, y, sampling_rate=0.2, random_state=1)
    assert list(y_ds.index) == sorted(y_ds.index)


def test_downsample_handles_no_negatives():
    X, y = _train_frame(n_pos=10, n_neg=0)
    X_ds, y_ds, realized = downsample_negatives(X, y, sampling_rate=0.1)
    assert realized == 1.0
    assert len(y_ds) == 10


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.5])
def test_downsample_rejects_out_of_range_rate(bad):
    X, y = _train_frame(n_pos=5, n_neg=50)
    with pytest.raises(ValueError, match="sampling_rate"):
        downsample_negatives(X, y, sampling_rate=bad)
