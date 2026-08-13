"""CTRModel 인터페이스를 구현한 모델들의 저장 계약 테스트.

새 모델 구현체가 save()를 override하든(LGBMModel) 안 하든(기본 구현) 저장→로드
후 predict_proba가 서빙이 기대하는 shape으로 동작하는지 검증한다.
"""

import numpy as np
import pandas as pd
import pytest

from autoresearch.model_training.base import CTRModel


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


def test_lgbm_model_save_load_round_trip_matches_serving_contract(tmp_path) -> None:
    import joblib
    import lightgbm as lgb

    from autoresearch.model_training.lgbm_model import LGBMModel

    X, y = _tiny_dataset()
    # LightGBM 4.x는 categorical_feature로 지정한 컬럼이 pandas "category" dtype이어야
    # 한다(object dtype은 ValueError) — 프로덕션 경로(autoresearch/model_training/train.py의
    # collect_categorical_categories)와 동일한 캐스팅을 테스트 픽스처에도 적용한다.
    X = X.assign(cat_feature=X["cat_feature"].astype("category"))
    model = LGBMModel(scale_pos_weight=1.0, n_estimators=5, num_leaves=3, random_state=42)
    model.fit(X, y, categorical_features=["cat_feature"])

    model_path = tmp_path / "model.joblib"
    model.save(str(model_path))

    # 서빙(src/serving/model_loader.py)과 동일한 로드 경로 — joblib.load를 직접 호출한다.
    loaded = joblib.load(model_path)

    assert isinstance(loaded, lgb.LGBMClassifier)
    proba = loaded.predict_proba(X)
    assert proba.shape == (len(X), 2)


def test_lgbm_model_load_classmethod_round_trip(tmp_path) -> None:
    from autoresearch.model_training.lgbm_model import LGBMModel

    X, y = _tiny_dataset()
    X = X.assign(cat_feature=X["cat_feature"].astype("category"))
    model = LGBMModel(scale_pos_weight=1.0, n_estimators=5, num_leaves=3, random_state=42)
    model.fit(X, y, categorical_features=["cat_feature"])

    model_path = tmp_path / "model.joblib"
    model.save(str(model_path))

    loaded = LGBMModel.load(str(model_path))

    assert isinstance(loaded, LGBMModel)
    proba = loaded.predict_proba(X)
    assert proba.shape == (len(X), 2)
    np.testing.assert_allclose(proba, model.predict_proba(X))


def test_lgbm_model_save_before_fit_raises_value_error(tmp_path) -> None:
    from autoresearch.model_training.lgbm_model import LGBMModel

    model = LGBMModel(scale_pos_weight=1.0)
    with pytest.raises(ValueError):
        model.save(str(tmp_path / "unfitted.joblib"))
