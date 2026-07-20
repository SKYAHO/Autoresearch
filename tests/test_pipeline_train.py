from __future__ import annotations

import numpy as np
import pandas as pd
import yaml
from mlflow.tracking import MlflowClient

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
        "registry": {"model_name": "ctr-model"},
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


def test_main_registers_model_and_auto_increments_version(tmp_path, monkeypatch) -> None:
    """#96: 학습 완료 후 ctr-model이 Model Registry에 등록되고 버전이 자동 증가하는지 검증."""
    tracking_uri = (tmp_path / "mlruns").as_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)

    config_path = tmp_path / "config.yaml"
    _write_train_config(config_path)
    data_path = tmp_path / "training_dataset.csv"
    _synthetic_ctr_dataset().to_csv(data_path, index=False)

    def run_once(suffix: str) -> None:
        train.main(
            config_path=str(config_path),
            data_path=str(data_path),
            model_output=str(tmp_path / f"model_{suffix}.joblib"),
            test_set_output=str(tmp_path / f"test_set_{suffix}.csv"),
            feature_columns_output=str(tmp_path / f"feature_columns_{suffix}.pkl"),
            categorical_columns_output=str(tmp_path / f"categorical_columns_{suffix}.pkl"),
            test_size=0.2,
            val_size=0.2,
            random_state=42,
        )

    run_once("v1")
    run_once("v2")

    client = MlflowClient(tracking_uri=tracking_uri)
    versions = client.search_model_versions("name='ctr-model'")
    assert {str(v.version) for v in versions} == {"1", "2"}
    for v in versions:
        assert v.run_id
        tags = client.get_model_version("ctr-model", str(v.version)).tags
        assert "val_roc_auc" in tags
