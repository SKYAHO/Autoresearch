"""CTRModel 인터페이스를 구현한 모델들의 저장 계약 테스트.

새 모델 구현체가 save()를 override하든(LGBMModel) 안 하든(기본 구현) 저장→로드
후 predict_proba가 서빙이 기대하는 shape으로 동작하는지 검증한다.
"""

import numpy as np
import pandas as pd
import pytest

from src.models.base import CTRModel


def _tiny_dataset() -> tuple[pd.DataFrame, pd.Series]:
    X = pd.DataFrame(
        {
            "num_feature": [0.1, 0.5, 0.9, 0.3, 0.7, 0.2, 0.8, 0.4],
            "cat_feature": ["a", "b", "a", "b", "a", "b", "a", "b"],
        }
    )
    y = pd.Series([0, 1, 0, 1, 0, 1, 0, 1])
    return X, y


def test_ctr_model_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        CTRModel()


class _DummyMeanModel(CTRModel):
    """save()/load()를 override하지 않고 CTRModel 기본 구현만 쓰는 테스트 전용 더미."""

    def __init__(self) -> None:
        self._mean = 0.5

    def fit(self, X_train, y_train, categorical_features=None) -> None:
        self._mean = float(y_train.mean())

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        p1 = np.full(len(X), self._mean)
        return np.column_stack([1 - p1, p1])


def test_ctr_model_default_save_load_round_trip_preserves_predict_proba(tmp_path) -> None:
    X, y = _tiny_dataset()
    model = _DummyMeanModel()
    model.fit(X, y)

    model_path = tmp_path / "dummy_model.joblib"
    model.save(str(model_path))

    loaded = CTRModel.load(str(model_path))

    assert isinstance(loaded, _DummyMeanModel)
    proba = loaded.predict_proba(X)
    assert proba.shape == (len(X), 2)
    np.testing.assert_allclose(proba[:, 1], y.mean())


def test_ctr_model_load_missing_file_raises_file_not_found_error(tmp_path) -> None:
    missing_path = tmp_path / "does_not_exist.joblib"
    with pytest.raises(FileNotFoundError):
        CTRModel.load(str(missing_path))
