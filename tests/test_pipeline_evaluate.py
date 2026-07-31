from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import yaml

from src.features.model_contract import (
    CATEGORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
    FeatureContractError,
)
from src.pipeline import evaluate


class _FakeModel:
    def __init__(self) -> None:
        self.received: pd.DataFrame | None = None

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        self.received = frame.copy()
        positive = np.linspace(0.2, 0.8, len(frame))
        return np.column_stack([1 - positive, positive])


def test_main_uses_canonical_feature_contract_without_config_columns(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.yaml"
    with config_path.open("w") as stream:
        yaml.safe_dump(
            {
                "data": {"path": "ignored.csv"},
                "artifacts": {
                    "model_path": str(tmp_path / "model.joblib"),
                    "feature_columns_path": str(tmp_path / "feature_columns.json"),
                },
            },
            stream,
        )

    dataset = pd.DataFrame(
        {column: np.arange(4, dtype=float) for column in MODEL_FEATURE_COLUMNS}
    )
    dataset["age_group"] = ["10s", "20s", "30s", "40s"]
    dataset["occupation"] = ["Student", "Engineer", "Marketer", "Student"]
    dataset["watch_time_band"] = ["morning", "evening", "night", "unknown"]
    dataset["historical_category_affinity"] = ["A", "B", "C", "A"]
    dataset["category_id"] = [1, 2, 1, 2]
    dataset["clicked"] = [0, 1, 0, 1]
    data_path = tmp_path / "test_set.csv"
    dataset.to_csv(data_path, index=False)

    fake_model = _FakeModel()
    monkeypatch.setattr(evaluate, "load_model", lambda _: fake_model)
    monkeypatch.setattr(evaluate, "load_feature_columns", lambda _: list(MODEL_FEATURE_COLUMNS))

    evaluate.main(config_path=str(config_path), data_path=str(data_path))

    assert fake_model.received is not None
    assert tuple(fake_model.received.columns) == MODEL_FEATURE_COLUMNS
    for column in CATEGORICAL_FEATURE_COLUMNS:
        assert str(fake_model.received[column].dtype) == "category"


def _eval_config_and_data(tmp_path):
    config_path = tmp_path / "config.yaml"
    with config_path.open("w") as stream:
        yaml.safe_dump(
            {
                "data": {"path": "ignored.csv"},
                "artifacts": {
                    "model_path": str(tmp_path / "model.joblib"),
                    "feature_columns_path": str(tmp_path / "feature_columns.json"),
                },
            },
            stream,
        )
    dataset = pd.DataFrame(
        {column: np.arange(4, dtype=float) for column in MODEL_FEATURE_COLUMNS}
    )
    dataset["age_group"] = ["10s", "20s", "30s", "40s"]
    dataset["occupation"] = ["Student", "Engineer", "Marketer", "Student"]
    dataset["watch_time_band"] = ["morning", "evening", "night", "unknown"]
    dataset["historical_category_affinity"] = ["A", "B", "C", "A"]
    dataset["category_id"] = [1, 2, 1, 2]
    dataset["clicked"] = [0, 1, 0, 1]
    data_path = tmp_path / "test_set.csv"
    dataset.to_csv(data_path, index=False)
    return config_path, data_path


def test_main_applies_calibration_with_given_sampling_rate(tmp_path, monkeypatch) -> None:
    # #300 결정 4: evaluate가 sampling_rate로 보정을 적용한다.
    config_path, data_path = _eval_config_and_data(tmp_path)
    monkeypatch.setattr(evaluate, "load_model", lambda _: _FakeModel())
    monkeypatch.setattr(evaluate, "load_feature_columns", lambda _: list(MODEL_FEATURE_COLUMNS))
    seen = {}

    def spy(q, sampling_rate):
        seen["rate"] = sampling_rate
        return q

    monkeypatch.setattr(evaluate, "apply_downsampling_calibration", spy)
    evaluate.main(config_path=str(config_path), data_path=str(data_path), sampling_rate=0.1)
    assert seen["rate"] == 0.1


def test_main_defaults_to_no_calibration(tmp_path, monkeypatch) -> None:
    # 하위호환(#300 결정 7): sampling_rate 미지정이면 1.0(보정 없음)으로 동작.
    config_path, data_path = _eval_config_and_data(tmp_path)
    monkeypatch.setattr(evaluate, "load_model", lambda _: _FakeModel())
    monkeypatch.setattr(evaluate, "load_feature_columns", lambda _: list(MODEL_FEATURE_COLUMNS))
    seen = {}

    def spy(q, sampling_rate):
        seen["rate"] = sampling_rate
        return q

    monkeypatch.setattr(evaluate, "apply_downsampling_calibration", spy)
    evaluate.main(config_path=str(config_path), data_path=str(data_path))
    assert seen["rate"] == 1.0


# --- 실험용 피처 오버라이드 (#405) ---


def _eval_config_and_data_with_extra(tmp_path, extra_column):
    config_path, data_path = _eval_config_and_data(tmp_path)
    dataset = pd.read_csv(data_path)
    dataset[extra_column] = np.arange(len(dataset), dtype=float)
    dataset.to_csv(data_path, index=False)
    return config_path, data_path


def test_main_accepts_experiment_columns_when_declared(tmp_path, monkeypatch) -> None:
    """실험 경로에서는 계약 검증이 학습을 막지 않는다(#405 완료조건 2)."""
    config_path, data_path = _eval_config_and_data_with_extra(tmp_path, "views_per_day")
    columns = list(MODEL_FEATURE_COLUMNS) + ["views_per_day"]
    monkeypatch.setattr(evaluate, "load_model", lambda _: _FakeModel())
    monkeypatch.setattr(evaluate, "load_feature_columns", lambda _: columns)

    evaluate.main(
        config_path=str(config_path),
        data_path=str(data_path),
        extra_features=["views_per_day"],
    )


def test_main_rejects_experiment_columns_that_differ_from_declaration(
    tmp_path, monkeypatch
) -> None:
    """아티팩트가 선언과 다른 실험 피처를 담고 있으면 채점 대상이 어긋난다."""
    config_path, data_path = _eval_config_and_data_with_extra(tmp_path, "views_per_day")
    columns = list(MODEL_FEATURE_COLUMNS) + ["views_per_day"]
    monkeypatch.setattr(evaluate, "load_model", lambda _: _FakeModel())
    monkeypatch.setattr(evaluate, "load_feature_columns", lambda _: columns)

    with pytest.raises(FeatureContractError):
        evaluate.main(
            config_path=str(config_path),
            data_path=str(data_path),
            extra_features=["other_feature"],
        )


def test_main_keeps_strict_contract_when_no_extra_features(tmp_path, monkeypatch) -> None:
    """미지정이면 prod 경로의 엄격한 동등 검사가 그대로 유지된다(#405 회귀 방지)."""
    config_path, data_path = _eval_config_and_data_with_extra(tmp_path, "views_per_day")
    columns = list(MODEL_FEATURE_COLUMNS) + ["views_per_day"]
    monkeypatch.setattr(evaluate, "load_model", lambda _: _FakeModel())
    monkeypatch.setattr(evaluate, "load_feature_columns", lambda _: columns)

    with pytest.raises(FeatureContractError):
        evaluate.main(config_path=str(config_path), data_path=str(data_path))
