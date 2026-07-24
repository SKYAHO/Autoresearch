"""DownsamplingCalibrator wrapper + Reranker calibration 체이닝 단위 테스트 (#302).

핵심 계약: 패키징(별도 모델)만 바뀌고 알고리즘(He 2014)은 안 바뀐다 —
wrapper가 apply_downsampling_calibration과 수치적으로 동일해야 한다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.models.calibration import DownsamplingCalibrator
from src.models.downsampling import apply_downsampling_calibration
from src.serving.schemas import CandidateVideo
from src.serving.service import Reranker


# ── DownsamplingCalibrator ─────────────────────────────────────


def test_calibrator_matches_apply_downsampling_calibration():
    # 패키징 변경이지 알고리즘 변경이 아니다: wrapper == 기존 순수 함수.
    q = np.array([0.01, 0.0909, 0.5, 0.9])
    cal = DownsamplingCalibrator(sampling_rate=0.1)
    np.testing.assert_allclose(
        np.asarray(cal.calibrate(q)),
        np.asarray(apply_downsampling_calibration(q, 0.1)),
    )


def test_calibrator_identity_at_rate_one():
    q = np.array([0.2, 0.8])
    out = DownsamplingCalibrator(sampling_rate=1.0).calibrate(q)
    np.testing.assert_allclose(np.asarray(out), q)


def test_calibrator_json_round_trip():
    cal = DownsamplingCalibrator(sampling_rate=0.1)
    restored = DownsamplingCalibrator.from_json(cal.to_json())
    assert restored.sampling_rate == pytest.approx(0.1)


def test_calibrator_save_load(tmp_path):
    path = tmp_path / "calibration.json"
    DownsamplingCalibrator(0.25).save(path)
    assert '"sampling_rate"' in path.read_text(encoding="utf-8")  # JSON, not pickle
    assert DownsamplingCalibrator.load(path).sampling_rate == pytest.approx(0.25)


@pytest.mark.parametrize("bad", [0.0, -0.1, 1.5])
def test_calibrator_rejects_out_of_range_rate(bad):
    with pytest.raises(ValueError, match="sampling_rate"):
        DownsamplingCalibrator(sampling_rate=bad)


# ── Reranker main → calibration 체이닝 ─────────────────────────


class _FakeModel:
    """predict_proba가 후보별 고정 positive 확률을 내는 가짜 모델."""

    def __init__(self, positives):
        self._positives = np.asarray(positives, dtype=float)

    def predict_proba(self, features: pd.DataFrame) -> np.ndarray:
        p = self._positives[: len(features)]
        return np.column_stack([1 - p, p])


def _candidates(n):
    feats = {c: 0.0 for c in ()}  # feature_columns 비우면 검증 우회 위해 아래서 조립
    return [CandidateVideo(video_id=f"v{i}", features=feats) for i in range(n)]


def _reranker(model, calibration=None):
    # feature_columns 비어있으면 missing 검사·categorical 캐스팅을 건너뛴다.
    return Reranker(
        model=model,
        feature_columns=(),
        categorical_categories={},
        calibration=calibration,
    )


def test_reranker_without_calibration_returns_raw_scores():
    # 하위호환: calibration=None이면 raw positive 확률 그대로(기존 1-모델 동작).
    model = _FakeModel([0.0909, 0.5])
    outcome = _reranker(model).rerank_with_diagnostics(_candidates(2))
    scores = {item.video_id: item.ctr_score for item in outcome.items}
    assert scores["v0"] == pytest.approx(0.0909)
    assert scores["v1"] == pytest.approx(0.5)


def test_reranker_with_calibration_chains_and_calibrates():
    # calibration이 있으면 main 확률을 원분포로 보정한 값을 반환한다.
    model = _FakeModel([0.0909, 0.5])
    cal = DownsamplingCalibrator(0.1)
    outcome = _reranker(model, calibration=cal).rerank_with_diagnostics(_candidates(2))
    scores = {item.video_id: item.ctr_score for item in outcome.items}
    assert scores["v0"] == pytest.approx(float(cal.calibrate(np.array([0.0909]))[0]))
    assert scores["v0"] == pytest.approx(0.0099, abs=1e-4)  # He 보정 원분포 복원
    # 보정은 monotonic이라 순위(v1 > v0)는 그대로.
    assert outcome.items[0].video_id == "v1"


def test_reranker_calibration_preserves_ranking_order():
    # monotonic → calibration 유무와 무관하게 정렬 순서 동일.
    positives = [0.2, 0.05, 0.8, 0.4]
    raw = _reranker(_FakeModel(positives)).rerank_with_diagnostics(_candidates(4))
    cal = _reranker(
        _FakeModel(positives), calibration=DownsamplingCalibrator(0.1)
    ).rerank_with_diagnostics(_candidates(4))
    assert [i.video_id for i in raw.items] == [i.video_id for i in cal.items]
