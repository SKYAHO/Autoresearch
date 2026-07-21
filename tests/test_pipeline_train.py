from __future__ import annotations

import numpy as np
import pandas as pd
import yaml

from src.pipeline import train
from src.pipeline.train import collect_categorical_categories

FEATURE_COLUMNS = [
    "age_group",
    "occupation",
    "historical_category_affinity",
    "recent_click_count_7d",
    "recent_watch_time_7d",
    "recent_like_count_7d",
    "category_id",
    "duration_sec",
    "view_count",
    "like_ratio",
    "comment_ratio",
    "days_since_upload",
    "historical_category_match",
    "preferred_category_match",
    "topic_similarity",
]
CATEGORICAL_COLUMNS = ["age_group", "occupation", "historical_category_affinity", "category_id"]


def _synthetic_ctr_dataset(n: int = 60, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "clicked": [i % 2 for i in range(n)],
            "age_group": rng.choice(["10s", "20s", "30s", "40s", "50s+"], size=n),
            "occupation": rng.choice(["Student", "Engineer", "Marketer"], size=n),
            "historical_category_affinity": rng.choice(["A", "B", "C"], size=n),
            "recent_click_count_7d": rng.integers(0, 20, size=n).astype(float),
            "recent_watch_time_7d": rng.random(size=n) * 100,
            "recent_like_count_7d": rng.integers(0, 10, size=n).astype(float),
            "category_id": rng.integers(1, 6, size=n),
            "duration_sec": rng.integers(60, 600, size=n).astype(float),
            "view_count": rng.integers(100, 100000, size=n).astype(float),
            "like_ratio": rng.random(size=n),
            "comment_ratio": rng.random(size=n),
            "days_since_upload": rng.integers(0, 30, size=n).astype(float),
            "historical_category_match": rng.integers(0, 2, size=n),
            "preferred_category_match": rng.integers(0, 2, size=n),
            "topic_similarity": rng.random(size=n),
        }
    )


def _write_train_config(config_path) -> None:
    config = {
        "data": {
            "path": "ignored.csv",
            "test_size": 0.2,
            "val_size": 0.2,
            "random_state": 42,
            "feature_columns": FEATURE_COLUMNS,
            "categorical_columns": CATEGORICAL_COLUMNS,
        },
        "model": {
            "n_estimators": 10,
            "learning_rate": 0.1,
            "num_leaves": 7,
            "scale_pos_weight": "auto",
            "random_state": 42,
        },
        "artifacts": {
            "model_path": "ignored/model.joblib",
            "feature_columns_path": "ignored/feature_columns.pkl",
            "categorical_columns_path": "ignored/categorical_columns.pkl",
            "test_set_path": "ignored/test_set.csv",
        },
    }
    with open(config_path, "w") as f:
        yaml.safe_dump(config, f)


def test_collect_categorical_categories_unions_train_and_val() -> None:
    X_train = pd.DataFrame({"category_id": [20, 10], "duration_sec": [1.0, 2.0]})
    X_val = pd.DataFrame({"category_id": [30], "duration_sec": [3.0]})

    result = collect_categorical_categories(X_train, X_val, ["category_id"])

    assert result == {"category_id": [10, 20, 30]}
    assert str(X_train["category_id"].dtype) == "category"
    assert list(X_train["category_id"].cat.categories) == [10, 20, 30]
    assert list(X_val["category_id"].cat.categories) == [10, 20, 30]
    # 비범주형 컬럼은 건드리지 않는다
    assert str(X_train["duration_sec"].dtype) == "float64"


def test_collect_categorical_categories_skips_missing_columns() -> None:
    X_train = pd.DataFrame({"duration_sec": [1.0]})
    X_val = pd.DataFrame({"duration_sec": [2.0]})

    result = collect_categorical_categories(X_train, X_val, ["category_id"])

    assert result == {}


def test_main_converts_and_logs_onnx_model(tmp_path, monkeypatch) -> None:
    """#178: 학습 완료 후 ONNX 변환·기록이 올바른 인자로 호출되는지 검증."""
    tracking_uri = (tmp_path / "mlruns").as_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)

    config_path = tmp_path / "config.yaml"
    _write_train_config(config_path)
    data_path = tmp_path / "training_dataset.csv"
    _synthetic_ctr_dataset().to_csv(data_path, index=False)

    sentinel_onnx_model = object()
    convert_calls = []
    log_calls = []

    def fake_convert(model, n_features):
        convert_calls.append({"model": model, "n_features": n_features})
        return sentinel_onnx_model

    def fake_log_onnx_model(onnx_model, artifact_path="model_onnx"):
        log_calls.append({"onnx_model": onnx_model, "artifact_path": artifact_path})

    monkeypatch.setattr(train, "convert_lgbm_to_onnx", fake_convert)
    monkeypatch.setattr(train, "log_onnx_model", fake_log_onnx_model)

    train.main(
        config_path=str(config_path),
        data_path=str(data_path),
        model_output=str(tmp_path / "model.joblib"),
        test_set_output=str(tmp_path / "test_set.csv"),
        feature_columns_output=str(tmp_path / "feature_columns.pkl"),
        categorical_columns_output=str(tmp_path / "categorical_columns.pkl"),
        test_size=0.2,
        val_size=0.2,
        random_state=42,
    )

    assert len(convert_calls) == 1
    assert convert_calls[0]["n_features"] == len(FEATURE_COLUMNS)
    assert convert_calls[0]["model"].model is not None  # 학습된 LGBMModel 인스턴스

    assert len(log_calls) == 1
    assert log_calls[0]["onnx_model"] is sentinel_onnx_model
    assert log_calls[0]["artifact_path"] == "model_onnx"
