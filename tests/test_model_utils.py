from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.models.lgbm_model import LGBMModel  # noqa: E402
from src.utils import model_utils  # noqa: E402
from src.utils.model_utils import load_categorical_columns, save_categorical_columns  # noqa: E402


CATEGORICAL_FEATURES = ["age_group", "occupation"]


def test_categorical_columns_roundtrip(tmp_path: Path) -> None:
    categories_by_column = {
        "category_id": [10, 20, 30],
        "age_group": ["10s", "20s", "30s"],
    }
    path = tmp_path / "categorical_columns.pkl"

    save_categorical_columns(categories_by_column, str(path))
    loaded = load_categorical_columns(str(path))

    assert loaded == categories_by_column


def _synthetic_training_data(n=200, seed=42):
    rng = np.random.default_rng(seed)
    X = pd.DataFrame(
        {
            "age_group": pd.Categorical(
                rng.choice(["10s", "20s", "30s", "40s", "50s+"], size=n)
            ),
            "occupation": pd.Categorical(
                rng.choice(["Student", "Engineer", "Marketer"], size=n)
            ),
            "recent_click_count_7d": rng.integers(0, 20, size=n).astype(float),
            "topic_similarity": rng.random(size=n),
        }
    )
    y = pd.Series(rng.integers(0, 2, size=n))
    return X, y


def test_extract_category_maps_returns_training_category_order():
    X, _ = _synthetic_training_data()

    maps = model_utils.extract_category_maps(X, CATEGORICAL_FEATURES)

    assert set(maps.keys()) == set(CATEGORICAL_FEATURES)
    assert maps["age_group"] == list(X["age_group"].cat.categories)
    assert maps["occupation"] == list(X["occupation"].cat.categories)


def test_convert_lgbm_to_onnx_matches_original_predictions():
    X, y = _synthetic_training_data()

    model = LGBMModel(scale_pos_weight=1.0, n_estimators=20)
    model.fit(X, y, categorical_features=CATEGORICAL_FEATURES)
    original_proba = model.predict_proba(X)[:, 1]

    onnx_model = model_utils.convert_lgbm_to_onnx(model, n_features=X.shape[1])

    import onnxruntime as ort

    # 재학습 없이 원본 모델을 그대로 변환했으므로, 추론 시에는 카테고리
    # 문자열이 아니라 학습 시점의 카테고리 순서(cat.codes)를 넣어야 한다.
    X_codes = X.copy()
    for col in CATEGORICAL_FEATURES:
        X_codes[col] = X_codes[col].cat.codes
    X_matrix = X_codes.astype(np.float32).to_numpy()

    sess = ort.InferenceSession(onnx_model.SerializeToString())
    outputs = sess.run(None, {"input": X_matrix})
    onnx_proba = np.array([row[1] for row in outputs[1]])

    assert np.abs(original_proba - onnx_proba).max() < 1e-4


def test_convert_lgbm_to_onnx_requires_fitted_model():
    model = LGBMModel(scale_pos_weight=1.0)

    with pytest.raises(ValueError, match="학습되지"):
        model_utils.convert_lgbm_to_onnx(model, n_features=4)
