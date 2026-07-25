"""서빙 ONNX 전환(#302/#179) 단위·통합 테스트.

핵심 계약: 표현(joblib→ONNX)만 바뀌고 예측·랭킹·calibration 체이닝은 안 바뀐다 —
ONNX 어댑터가 joblib LightGBM과 수치 허용오차 내로 동일해야 하고, model_onnx/가 없는
기존 champion은 joblib으로 폴백해야 한다(하위호환).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.features.model_contract import (
    CATEGORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
)
from src.models.calibration import DownsamplingCalibrator
from src.models.lgbm_model import LGBMModel
from src.serving.model_loader import LocalModelSettings, load_local_model
from src.serving.onnx_model import OnnxProbabilityModel
from src.serving.schemas import CandidateVideo
from src.serving.service import Reranker
from src.utils.model_utils import (
    convert_lgbm_to_onnx,
    save_categorical_columns,
    save_feature_columns,
    save_model,
)


def _synthetic_contract_frame(n: int, seed: int) -> pd.DataFrame:
    """model_contract의 21개 컬럼을 갖춘 합성 프레임(clicked 제외)."""
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "age_group": rng.choice(["10s", "20s", "30s", "40s", "50s+"], size=n),
            "occupation": rng.choice(["Student", "Engineer", "Marketer"], size=n),
            "watch_time_band": rng.choice(["morning", "evening", "night", "unknown"], size=n),
            "recent_click_count_7d": rng.integers(0, 20, size=n).astype(float),
            "recent_view_count_7d": rng.integers(0, 30, size=n).astype(float),
            "recent_watch_time_7d": rng.random(size=n) * 100,
            "recent_like_count_7d": rng.integers(0, 10, size=n).astype(float),
            "historical_category_affinity": rng.choice(["A", "B", "C"], size=n),
            "total_event_count_7d": rng.integers(0, 100, size=n).astype(float),
            "category_id": rng.integers(1, 6, size=n),
            "duration_sec": rng.integers(60, 600, size=n).astype(float),
            "view_count": rng.integers(100, 100000, size=n).astype(float),
            "like_ratio": rng.random(size=n),
            "comment_ratio": rng.random(size=n),
            "days_since_upload": rng.integers(0, 30, size=n).astype(float),
            "channel_subscriber_count": rng.integers(0, 1_000_000, size=n).astype(float),
            "channel_view_count": rng.integers(0, 100_000_000, size=n).astype(float),
            "channel_video_count": rng.integers(0, 10_000, size=n).astype(float),
            "topic_similarity": rng.random(size=n),
            "preferred_category_match": rng.integers(0, 2, size=n).astype(float),
            "historical_category_match": rng.integers(0, 2, size=n).astype(float),
        }
    )[list(MODEL_FEATURE_COLUMNS)]


def _categorical_categories(frame: pd.DataFrame) -> dict:
    return {col: sorted(frame[col].unique().tolist()) for col in CATEGORICAL_FEATURE_COLUMNS}


def _cast_categoricals(frame: pd.DataFrame, categories: dict) -> pd.DataFrame:
    out = frame.copy()
    for col, cats in categories.items():
        out[col] = pd.Categorical(out[col], categories=cats)
    return out


def _fit_contract_model(n: int = 300, seed: int = 5):
    frame = _synthetic_contract_frame(n, seed)
    categories = _categorical_categories(frame)
    train_frame = _cast_categoricals(frame, categories)
    rng = np.random.default_rng(seed + 1)
    labels = pd.Series((rng.random(n) < 0.3).astype(int))
    model = LGBMModel(scale_pos_weight=1, n_estimators=40, num_leaves=15)
    model.fit(train_frame, labels, categorical_features=list(CATEGORICAL_FEATURE_COLUMNS))
    return model, categories


# ── OnnxProbabilityModel 어댑터 수치 동등 ─────────────────────────


def test_onnx_adapter_matches_lgbm_on_categorical_input() -> None:
    # 서빙 경로 재현: category dtype 입력(미학습값 포함 → code -1)에서 ONNX 어댑터가
    # joblib LGBMClassifier와 허용오차 내로 동일. category→정수코드 인코딩 경로 검증.
    import onnxruntime as ort

    model, categories = _fit_contract_model()
    onnx_model = convert_lgbm_to_onnx(model, n_features=len(MODEL_FEATURE_COLUMNS))
    session = ort.InferenceSession(onnx_model.SerializeToString())
    adapter = OnnxProbabilityModel(session, MODEL_FEATURE_COLUMNS)

    serve_frame = _synthetic_contract_frame(50, seed=99)
    # 미학습 카테고리 주입 → NaN → code -1 (조용한 강등 경로).
    serve_frame.loc[serve_frame.index[:5], "occupation"] = "UNSEEN_JOB"
    serve_cast = _cast_categoricals(serve_frame, categories)

    onnx_proba = adapter.predict_proba(serve_cast)
    lgbm_proba = model.predict_proba(serve_cast)

    assert onnx_proba.shape == (len(serve_frame), 2)
    np.testing.assert_allclose(onnx_proba, lgbm_proba, atol=1e-4)


# ── 로더: model_onnx/(로컬 .onnx) 있으면 ONNX, 없으면 joblib 폴백 ──


def _save_contract_artifacts(tmp_path: Path, model, categories, *, with_onnx: bool):
    model_path = tmp_path / "model.joblib"
    feature_path = tmp_path / "feature_columns.pkl"
    categorical_path = tmp_path / "categorical_columns.pkl"
    save_model(model.model, str(model_path))
    save_feature_columns(list(MODEL_FEATURE_COLUMNS), str(feature_path))
    save_categorical_columns(categories, str(categorical_path))
    onnx_path = None
    if with_onnx:
        onnx_path = tmp_path / "model.onnx"
        onnx_model = convert_lgbm_to_onnx(model, n_features=len(MODEL_FEATURE_COLUMNS))
        onnx_path.write_bytes(onnx_model.SerializeToString())
    return LocalModelSettings(
        model_path=model_path,
        feature_columns_path=feature_path,
        categorical_columns_path=categorical_path,
        onnx_model_path=onnx_path,
    )


def _candidates_from_frame(frame: pd.DataFrame) -> list[CandidateVideo]:
    records = frame.to_dict(orient="records")
    return [
        CandidateVideo(video_id=f"v{i}", features=record)
        for i, record in enumerate(records)
    ]


def test_local_loader_onnx_and_joblib_produce_equivalent_ranking(tmp_path: Path) -> None:
    # 하위호환 핵심: 같은 학습에서 나온 ONNX 로드와 joblib 폴백 로드가 동일 순위·
    # 허용오차 내 동일 점수를 낸다. (model_onnx/ 없는 기존 champion은 joblib으로 폴백.)
    model, categories = _fit_contract_model()

    onnx_settings = _save_contract_artifacts(tmp_path / "onnx", model, categories, with_onnx=True)
    joblib_settings = _save_contract_artifacts(
        tmp_path / "joblib", model, categories, with_onnx=False
    )

    onnx_reranker = load_local_model(onnx_settings)
    joblib_reranker = load_local_model(joblib_settings)

    assert isinstance(onnx_reranker.model, OnnxProbabilityModel)
    assert not isinstance(joblib_reranker.model, OnnxProbabilityModel)

    candidates = _candidates_from_frame(_synthetic_contract_frame(40, seed=123))
    onnx_items = onnx_reranker.rerank(candidates)
    joblib_items = joblib_reranker.rerank(candidates)

    assert [i.video_id for i in onnx_items] == [i.video_id for i in joblib_items]
    onnx_scores = {i.video_id: i.ctr_score for i in onnx_items}
    joblib_scores = {i.video_id: i.ctr_score for i in joblib_items}
    for video_id, score in joblib_scores.items():
        assert onnx_scores[video_id] == pytest.approx(score, abs=1e-4)


def test_onnx_reranker_preserves_calibration_chaining(tmp_path: Path) -> None:
    # ONNX 어댑터로 로드해도 main→calibration 체이닝은 그대로: calibration 적용 점수가
    # raw ONNX 점수를 He 보정한 값과 일치하고, monotonic이라 순위는 불변.
    model, categories = _fit_contract_model()
    settings = _save_contract_artifacts(tmp_path, model, categories, with_onnx=True)
    reranker = load_local_model(settings)
    calibration = DownsamplingCalibrator(0.1)
    calibrated = Reranker(
        model=reranker.model,
        feature_columns=reranker.feature_columns,
        categorical_categories=reranker.categorical_categories,
        calibration=calibration,
    )

    candidates = _candidates_from_frame(_synthetic_contract_frame(30, seed=321))
    raw_items = reranker.rerank(candidates)
    cal_items = calibrated.rerank(candidates)

    # 순위 불변(monotonic).
    assert [i.video_id for i in raw_items] == [i.video_id for i in cal_items]
    # 점수는 raw를 He 보정한 값.
    raw_scores = {i.video_id: i.ctr_score for i in raw_items}
    for item in cal_items:
        expected = float(calibration.calibrate(np.array([raw_scores[item.video_id]]))[0])
        assert item.ctr_score == pytest.approx(expected, abs=1e-9)
