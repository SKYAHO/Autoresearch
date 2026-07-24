from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.models.lgbm_model import LGBMModel
from src.utils.model_utils import (
    convert_lgbm_to_onnx,
    load_categorical_columns,
    save_categorical_columns,
)


def test_categorical_columns_roundtrip(tmp_path: Path) -> None:
    categories_by_column = {
        "category_id": [10, 20, 30],
        "age_group": ["10s", "20s", "30s"],
    }
    path = tmp_path / "categorical_columns.pkl"

    save_categorical_columns(categories_by_column, str(path))
    loaded = load_categorical_columns(str(path))

    assert loaded == categories_by_column


def _fit_lgbm(n: int = 200, seed: int = 3) -> tuple[LGBMModel, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    features = pd.DataFrame(
        {
            "num1": rng.random(n),
            "num2": rng.normal(size=n),
            "num3": rng.integers(0, 100, size=n).astype(float),
        }
    )
    labels = pd.Series((rng.random(n) < 0.3).astype(int))
    model = LGBMModel(scale_pos_weight=1, n_estimators=30, num_leaves=15)
    model.fit(features, labels, categorical_features=[])
    return model, features


def test_convert_lgbm_to_onnx_matches_lightgbm_within_tolerance() -> None:
    # zipmap=False라 확률 출력이 dict 시퀀스가 아니라 (n, 2) 텐서다 — 서빙이 그대로
    # 슬라이싱한다(#179 기본값 zipmap=True의 dict 파싱과 다른 지점). ONNX 예측은 원본
    # LightGBM과 허용오차(1e-4) 내로 동일해야 한다("완전 동일/diff 0.0"이 아니라 수치 허용오차).
    import onnxruntime as ort

    model, features = _fit_lgbm()
    onnx_model = convert_lgbm_to_onnx(model, n_features=features.shape[1])
    session = ort.InferenceSession(onnx_model.SerializeToString())
    outputs = session.run(None, {"input": features.to_numpy(dtype=np.float32)})

    # zipmap=False 계약 고정: probabilities 출력이 (n, 2) 2D 텐서.
    probabilities = next(out for out in outputs if getattr(out, "ndim", 0) == 2)
    assert probabilities.shape == (len(features), 2)

    onnx_positive = probabilities[:, 1]
    lgbm_positive = model.predict_proba(features)[:, 1]
    np.testing.assert_allclose(onnx_positive, lgbm_positive, atol=1e-4)


def test_convert_lgbm_to_onnx_rejects_untrained_model() -> None:
    with pytest.raises(ValueError, match="학습"):
        convert_lgbm_to_onnx(LGBMModel(scale_pos_weight=1), n_features=3)
