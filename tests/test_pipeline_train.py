from __future__ import annotations

import pickle

import numpy as np
import pandas as pd
import pytest
import yaml
from mlflow.tracking import MlflowClient

from src.features.model_contract import CATEGORICAL_FEATURE_COLUMNS, MODEL_FEATURE_COLUMNS
from src.pipeline import train
from src.pipeline.train import collect_categorical_categories


def _synthetic_ctr_dataset(n: int = 60, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "clicked": [i % 2 for i in range(n)],
            "age_group": rng.choice(["10s", "20s", "30s", "40s", "50s+"], size=n),
            "occupation": rng.choice(["Student", "Engineer", "Marketer"], size=n),
            "watch_time_band": rng.choice(["morning", "evening", "night", "unknown"], size=n),
            "historical_category_affinity": rng.choice(["A", "B", "C"], size=n),
            "recent_click_count_7d": rng.integers(0, 20, size=n).astype(float),
            "recent_view_count_7d": rng.integers(0, 30, size=n),
            "recent_watch_time_7d": rng.random(size=n) * 100,
            "recent_like_count_7d": rng.integers(0, 10, size=n).astype(float),
            "total_event_count_7d": rng.integers(0, 100, size=n),
            "category_id": rng.integers(1, 6, size=n),
            "duration_sec": rng.integers(60, 600, size=n).astype(float),
            "view_count": rng.integers(100, 100000, size=n).astype(float),
            "like_ratio": rng.random(size=n),
            "comment_ratio": rng.random(size=n),
            "days_since_upload": rng.integers(0, 30, size=n).astype(float),
            "channel_subscriber_count": rng.integers(0, 1_000_000, size=n),
            "channel_view_count": rng.integers(0, 100_000_000, size=n),
            "channel_video_count": rng.integers(0, 10_000, size=n),
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

    with (tmp_path / "feature_columns_v1.pkl").open("rb") as stream:
        assert tuple(pickle.load(stream)) == MODEL_FEATURE_COLUMNS
    with (tmp_path / "categorical_columns_v1.pkl").open("rb") as stream:
        categories = pickle.load(stream)
    assert tuple(categories) == CATEGORICAL_FEATURE_COLUMNS
    assert "watch_time_band" in categories

    run_once("v2")

    client = MlflowClient(tracking_uri=tracking_uri)
    versions = client.search_model_versions("name='ctr-model'")
    assert {str(v.version) for v in versions} == {"1", "2"}
    for v in versions:
        assert v.run_id
        tags = client.get_model_version("ctr-model", str(v.version)).tags
        assert "val_roc_auc" in tags


def test_main_survives_registry_registration_failure(tmp_path, monkeypatch) -> None:
    """리뷰 반영: Registry 등록은 학습이 끝난 뒤의 best-effort 단계라, 등록이
    실패해도(registry 백엔드 미구성·네트워크 오류 등) run 전체를 실패로
    마킹해서는 안 된다 — 모델은 이미 저장·기록된 뒤다."""
    tracking_uri = (tmp_path / "mlruns").as_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)

    config_path = tmp_path / "config.yaml"
    _write_train_config(config_path)
    data_path = tmp_path / "training_dataset.csv"
    _synthetic_ctr_dataset().to_csv(data_path, index=False)

    def fake_register_model_raises(model_uri, model_name, tags=None):
        raise RuntimeError("registry 백엔드 없음(시뮬레이션)")

    monkeypatch.setattr(train, "register_model", fake_register_model_raises)

    model_output = tmp_path / "model.joblib"
    # 예외가 전파되지 않고 main()이 끝까지 정상 실행되어야 한다.
    train.main(
        config_path=str(config_path),
        data_path=str(data_path),
        model_output=str(model_output),
        test_set_output=str(tmp_path / "test_set.csv"),
        feature_columns_output=str(tmp_path / "feature_columns.pkl"),
        categorical_columns_output=str(tmp_path / "categorical_columns.pkl"),
        test_size=0.2,
        val_size=0.2,
        random_state=42,
    )

    # 모델 파일은 registry 등록 실패와 무관하게 이미 저장되어 있어야 한다.
    assert model_output.exists()


def test_main_registers_lineage_tags_from_extra_params(tmp_path, monkeypatch) -> None:
    """extra_params(데이터 계보)로 넘긴 값이 실제로 등록된 버전의 태그에
    반영되는지 검증(run params 기록뿐 아니라 registry 태그까지 전파)."""
    tracking_uri = (tmp_path / "mlruns").as_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)

    config_path = tmp_path / "config.yaml"
    _write_train_config(config_path)
    data_path = tmp_path / "training_dataset.csv"
    _synthetic_ctr_dataset().to_csv(data_path, index=False)

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
        extra_params={"videos_source": "bigquery", "events_source": "bigquery"},
    )

    client = MlflowClient(tracking_uri=tracking_uri)
    [version] = client.search_model_versions("name='ctr-model'")
    tags = client.get_model_version("ctr-model", str(version.version)).tags
    assert tags["videos_source"] == "bigquery"
    assert tags["events_source"] == "bigquery"


def _write_train_config_with(config_path, *, sampling_rate=None, scale_pos_weight="auto") -> None:
    """downsampling 관련 옵션을 넣은 train config (#300)."""
    config_path_str = str(config_path)
    _write_train_config(config_path)
    with open(config_path_str) as f:
        config = yaml.safe_load(f)
    config["model"]["scale_pos_weight"] = scale_pos_weight
    if sampling_rate is not None:
        config["model"]["sampling_rate"] = sampling_rate
    with open(config_path_str, "w") as f:
        yaml.safe_dump(config, f)


def _run_train(tmp_path, config_path):
    return train.main(
        config_path=str(config_path),
        data_path=str(tmp_path / "training_dataset.csv"),
        model_output=str(tmp_path / "model.joblib"),
        test_set_output=str(tmp_path / "test_set.csv"),
        feature_columns_output=str(tmp_path / "feature_columns.pkl"),
        categorical_columns_output=str(tmp_path / "categorical_columns.pkl"),
        test_size=0.2,
        val_size=0.2,
        random_state=42,
    )


def test_main_downsampling_records_sampling_rate_and_preserves_test_set(tmp_path, monkeypatch) -> None:
    # #300: downsampling 켜면 run param + 모델 버전 tag에 실현 sampling_rate가
    # 기록되고, held-out test set은 원분포(50/50)를 유지해야 한다(train만 줄임).
    tracking_uri = (tmp_path / "mlruns").as_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    config_path = tmp_path / "config.yaml"
    _write_train_config_with(config_path, sampling_rate=0.5)
    _synthetic_ctr_dataset(n=200).to_csv(tmp_path / "training_dataset.csv", index=False)

    realized = _run_train(tmp_path, config_path)
    assert 0.0 < realized < 1.0

    client = MlflowClient(tracking_uri=tracking_uri)
    [version] = client.search_model_versions("name='ctr-model'")
    tags = client.get_model_version("ctr-model", str(version.version)).tags
    assert float(tags["sampling_rate"]) < 1.0
    run = client.get_run(version.run_id)
    assert float(run.data.params["sampling_rate"]) < 1.0

    # held-out test set은 원분포(합성 50/50)를 유지 — downsampling이 새지 않음.
    test_df = pd.read_csv(tmp_path / "test_set.csv")
    assert test_df["clicked"].mean() == pytest.approx(0.5, abs=0.1)


def test_main_downsampling_forces_scale_pos_weight_to_one(tmp_path, monkeypatch) -> None:
    # #300 결정 6: downsampling 켜지면 scale_pos_weight(auto)가 1로 강제된다(이중 보정 방지).
    tracking_uri = (tmp_path / "mlruns").as_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    config_path = tmp_path / "config.yaml"
    _write_train_config_with(config_path, sampling_rate=0.5, scale_pos_weight="auto")
    _synthetic_ctr_dataset(n=200).to_csv(tmp_path / "training_dataset.csv", index=False)

    _run_train(tmp_path, config_path)

    client = MlflowClient(tracking_uri=tracking_uri)
    [version] = client.search_model_versions("name='ctr-model'")
    run = client.get_run(version.run_id)
    assert float(run.data.params["scale_pos_weight"]) == 1.0


def test_main_downsampling_with_explicit_scale_pos_weight_fails_closed(tmp_path, monkeypatch) -> None:
    # #300 결정 6 가드: downsampling + 명시적 scale_pos_weight(≠1) 동시 세팅은 fail-closed.
    tracking_uri = (tmp_path / "mlruns").as_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    config_path = tmp_path / "config.yaml"
    _write_train_config_with(config_path, sampling_rate=0.5, scale_pos_weight=5)
    _synthetic_ctr_dataset(n=200).to_csv(tmp_path / "training_dataset.csv", index=False)

    with pytest.raises(ValueError, match="이중 보정"):
        _run_train(tmp_path, config_path)
