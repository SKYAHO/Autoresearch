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
from sklearn.metrics import roc_auc_score

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


def test_grouped_roc_auc_differs_from_global_roc_auc() -> None:
    """유저 단위 AUC는 전역 AUC와 다른 값을 잰다(#505의 존재 이유).

    두 유저 각각의 목록 안에서는 순서가 완벽하지만(유저별 1.0), 유저를 섞어 전역으로
    재면 uB의 양성(0.2)이 uA의 음성(0.8)보다 낮아 0.75가 된다. 리랭킹 품질은 유저
    목록 **안**의 순서이므로 전역 AUC는 이 차이를 감춘다.
    """
    labels = [1, 0, 1, 0]
    scores = [0.9, 0.8, 0.2, 0.1]
    groups = ["uA", "uA", "uB", "uB"]

    result = evaluate.grouped_roc_auc(labels, scores, groups)

    assert result.value == pytest.approx(1.0)
    assert result.total_groups == 2
    assert result.scored_groups == 2
    assert result.skipped_groups == 0
    # 같은 데이터의 전역 AUC는 0.75 — 두 지표가 갈린다.
    assert roc_auc_score(labels, scores) == pytest.approx(0.75)


def test_grouped_roc_auc_macro_averages_over_users() -> None:
    """유저별 후보 수가 달라도 큰 유저가 지표를 지배하지 않는다(유저 동등 가중)."""
    # uA: 완벽 정렬(1.0). uB: 완전 역순(0.0). 행 수는 uA가 2배.
    labels = [1, 1, 0, 0, 1, 0]
    scores = [0.9, 0.8, 0.2, 0.1, 0.1, 0.9]
    groups = ["uA", "uA", "uA", "uA", "uB", "uB"]

    result = evaluate.grouped_roc_auc(labels, scores, groups)

    assert result.value == pytest.approx(0.5)
    assert result.scored_groups == 2


def test_grouped_roc_auc_skips_single_class_users_and_reports_counts() -> None:
    """한 클래스만 가진 유저는 AUC가 정의되지 않으므로 제외하고, 제외 수를 보고한다."""
    labels = [1, 0, 1, 1]
    scores = [0.9, 0.1, 0.5, 0.4]
    groups = ["uA", "uA", "uB", "uB"]  # uB는 양성만

    result = evaluate.grouped_roc_auc(labels, scores, groups)

    assert result.value == pytest.approx(1.0)  # uA만 반영
    assert result.total_groups == 2
    assert result.scored_groups == 1
    assert result.skipped_groups == 1


def test_grouped_roc_auc_returns_none_when_no_user_is_scorable() -> None:
    """대상 유저가 0명이면 실패시키지 않고 None을 보고한다(관측 지표, 판정 지표 아님)."""
    result = evaluate.grouped_roc_auc([1, 1], [0.9, 0.8], ["uA", "uB"])

    assert result.value is None
    assert result.total_groups == 2
    assert result.scored_groups == 0
    assert result.skipped_groups == 2


def test_main_reports_grouped_roc_auc_when_passthrough_present(
    tmp_path, monkeypatch, capsys
) -> None:
    """패스스루 컬럼이 있으면 전역 지표와 **나란히** grouped 지표를 보고한다(#505)."""
    config_path, data_path = _eval_config_and_data(tmp_path)
    frame = pd.read_csv(data_path)
    frame["user_id"] = ["uA", "uA", "uB", "uB"]  # 조립이 보존하는 형태
    frame.to_csv(data_path, index=False)
    monkeypatch.setattr(evaluate, "load_model", lambda _: _FakeModel())
    monkeypatch.setattr(
        evaluate, "load_feature_columns", lambda _: list(MODEL_FEATURE_COLUMNS)
    )

    evaluate.main(config_path=str(config_path), data_path=str(data_path))

    out = capsys.readouterr().out
    assert "Grouped ROC-AUC" in out
    # 값만 내보내지 않는다 — 집계 대상 수가 없으면 신뢰도를 알 수 없다.
    assert "유저" in out


def test_main_skips_grouped_roc_auc_without_passthrough(
    tmp_path, monkeypatch, capsys
) -> None:
    """패스스루 이전에 만들어진 스냅샷도 그대로 평가된다(#505).

    조립은 fail-closed지만 평가는 관대해야 한다 — 과거 데이터셋으로 재현 평가를
    돌리는 경로를 끊으면 비교 가능성이 사라진다.
    """
    config_path, data_path = _eval_config_and_data(tmp_path)  # user_id 없음
    monkeypatch.setattr(evaluate, "load_model", lambda _: _FakeModel())
    monkeypatch.setattr(
        evaluate, "load_feature_columns", lambda _: list(MODEL_FEATURE_COLUMNS)
    )

    evaluate.main(config_path=str(config_path), data_path=str(data_path))

    out = capsys.readouterr().out
    assert "ROC-AUC" in out  # 전역 지표는 그대로 나온다
    assert "Grouped ROC-AUC" not in out


def test_group_key_column_is_a_passthrough_column() -> None:
    """그룹 키는 반드시 패스스루 컬럼이어야 한다(#505 드리프트 방지).

    모델 입력 컬럼을 그룹 키로 쓰면 "모델이 본 것"으로 그룹을 나누게 되고, 패스스루가
    아닌 컬럼은 조립이 CSV에 싣지 않아 평가가 조용히 건너뛴다.
    """
    from src.features.model_contract import PASSTHROUGH_COLUMNS

    assert evaluate.GROUP_KEY_COLUMN in PASSTHROUGH_COLUMNS
