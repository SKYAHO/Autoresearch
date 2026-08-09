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
        self.predict_calls = 0

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        self.received = frame.copy()
        self.predict_calls += 1
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


# --- held-out 다중 지표 (#493 D3) ---


def _held_out_dataset() -> pd.DataFrame:
    dataset = pd.DataFrame(
        {column: np.arange(4, dtype=float) for column in MODEL_FEATURE_COLUMNS}
    )
    dataset["age_group"] = ["10s", "20s", "30s", "40s"]
    dataset["occupation"] = ["Student", "Engineer", "Marketer", "Student"]
    dataset["watch_time_band"] = ["morning", "evening", "night", "unknown"]
    dataset["historical_category_affinity"] = ["A", "B", "C", "A"]
    dataset["category_id"] = [1, 2, 1, 2]
    dataset["clicked"] = [0, 0, 1, 1]
    return dataset


def test_held_out_metric_names_match_the_evidence_contract_allowlist() -> None:
    """산출 지표 집합이 증거 계약 allowlist와 갈라지면 게시가 거부된다."""
    from src.pipeline.promotion_evidence import SUPPORTED_HELD_OUT_METRIC_NAMES

    assert set(evaluate.HELD_OUT_METRIC_NAMES) == set(SUPPORTED_HELD_OUT_METRIC_NAMES)


def test_evaluate_held_out_metrics_computes_every_metric_from_one_prediction() -> None:
    model = _FakeModel()
    dataset = _held_out_dataset()

    metrics = evaluate.evaluate_held_out_metrics(
        model, dataset, MODEL_FEATURE_COLUMNS
    )

    assert model.predict_calls == 1
    assert set(metrics) == set(evaluate.HELD_OUT_METRIC_NAMES)
    assert all(isinstance(value, float) for value in metrics.values())
    assert metrics["roc_auc"] == evaluate.evaluate_held_out_roc_auc(
        model, dataset, MODEL_FEATURE_COLUMNS
    )
    assert model.received is not None
    for column in CATEGORICAL_FEATURE_COLUMNS:
        assert str(model.received[column].dtype) == "category"


def test_evaluate_held_out_metrics_calibrates_log_loss_only() -> None:
    """순위 기반 지표는 보정에 불변이고 Log Loss만 원분포로 이동한다(#300 결정 5)."""
    dataset = _held_out_dataset()
    raw = evaluate.evaluate_held_out_metrics(
        _FakeModel(), dataset, MODEL_FEATURE_COLUMNS
    )

    calibrated = evaluate.evaluate_held_out_metrics(
        _FakeModel(), dataset, MODEL_FEATURE_COLUMNS, sampling_rate=0.1
    )

    assert calibrated["roc_auc"] == raw["roc_auc"]
    assert calibrated["pr_auc"] == raw["pr_auc"]
    assert calibrated["log_loss"] != raw["log_loss"]


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


def test_grouped_roc_auc_reports_null_group_keys() -> None:
    """그룹 키가 null인 행은 따로 세어 보고한다(#506 리뷰).

    pandas ``groupby``의 기본값 ``dropna=True``는 이 행들을 그룹으로 세지도, 집계에
    넣지도 않는다. 커버리지 숫자만 보면 멀쩡해 보이지만 실제로는 행이 조용히
    사라져, 이 dataclass가 약속하는 "값을 얼마나 믿을 수 있는지의 근거"가 거짓이 된다.
    """
    labels = [1, 0, 1, 0]
    scores = [0.9, 0.1, 0.8, 0.2]
    groups = ["uA", "uA", None, None]

    result = evaluate.grouped_roc_auc(labels, scores, groups)

    assert result.value == pytest.approx(1.0)  # uA만 반영
    # null 키는 어느 유저에도 귀속되지 않으므로 그룹으로 세지 않는다.
    assert result.total_groups == 1
    assert result.scored_groups == 1
    assert result.skipped_groups == 0
    # 그러나 사라지지도 않는다 — 버려진 행 수가 드러나야 한다.
    assert result.null_key_rows == 2


def _held_out_metrics_kwargs(**overrides) -> dict:
    """`write_held_out_metrics`의 필수 인자를 채운 기본 묶음."""
    kwargs = {
        "roc_auc": 0.7834,
        "pr_auc": 0.1122,
        "logloss": 0.0875,
        "brier": 0.0132,
        "predicted_mean": 0.0147,
        "actual_positive_rate": 0.0147,
        "row_count": 98894,
        "positive_count": 1455,
        "sampling_rate": 1.0,
    }
    kwargs.update(overrides)
    return kwargs


def test_write_held_out_metrics_records_stdout_equivalent_values(tmp_path) -> None:
    """stdout에 찍는 지표와 같은 값을 기계가 읽을 JSON으로 남긴다.

    executor가 조건·seed별로 이 파일을 모아 비교하므로, 출력 형식이 바뀌어도 깨지지
    않는 경로가 필요하다.
    """
    import json

    destination = tmp_path / "nested" / "metrics.json"

    returned = evaluate.write_held_out_metrics(
        str(destination), **_held_out_metrics_kwargs()
    )

    written = json.loads(destination.read_text(encoding="utf-8"))
    # 돌려준 payload와 파일 내용이 갈라지면 호출자가 둘 중 무엇을 믿을지 알 수 없다.
    assert written == returned
    assert written["contract_version"] == evaluate.HELD_OUT_METRICS_CONTRACT_VERSION
    assert written["roc_auc"] == pytest.approx(0.7834)
    assert written["log_loss"] == pytest.approx(0.0875)
    assert written["brier"] == pytest.approx(0.0132)
    assert written["row_count"] == 98894


def test_write_held_out_metrics_keeps_grouped_key_when_not_computed(tmp_path) -> None:
    """패스스루 컬럼이 없어 유저 단위 지표를 못 낸 경우에도 키를 남긴다.

    키를 생략하면 읽는 쪽이 "계산하지 않았다"와 "0이었다"를 구분할 수 없다 —
    ``GroupedRocAuc``가 약속하는 커버리지 정직성이 파일 경계에서 깨진다.
    """
    import json

    destination = tmp_path / "metrics.json"

    evaluate.write_held_out_metrics(str(destination), **_held_out_metrics_kwargs())

    written = json.loads(destination.read_text(encoding="utf-8"))
    assert "grouped_roc_auc" in written
    assert written["grouped_roc_auc"] is None


def test_write_held_out_metrics_carries_grouped_coverage(tmp_path) -> None:
    """유저 단위 지표는 값만이 아니라 커버리지 근거까지 함께 실린다(#505)."""
    import json

    destination = tmp_path / "metrics.json"
    grouped = evaluate.GroupedRocAuc(
        value=0.81,
        total_groups=120,
        scored_groups=95,
        skipped_groups=25,
        null_key_rows=7,
    )

    evaluate.write_held_out_metrics(
        str(destination), **_held_out_metrics_kwargs(grouped=grouped)
    )

    written = json.loads(destination.read_text(encoding="utf-8"))["grouped_roc_auc"]
    assert written["value"] == pytest.approx(0.81)
    assert written["scored_groups"] == 95
    assert written["skipped_groups"] == 25
    assert written["null_key_rows"] == 7


def test_write_held_out_metrics_leaves_no_partial_file_on_failure(
    tmp_path, monkeypatch
) -> None:
    """쓰는 도중 실패하면 부분 파일도 임시 파일도 남기지 않는다.

    읽는 쪽이 "파일이 있으면 완결됐다"를 가정할 수 있어야 한다. 반쯤 쓰인 JSON이
    남으면 다음 단계가 파싱 오류로 죽거나, 더 나쁘게는 잘린 값을 지표로 읽는다.
    """
    destination = tmp_path / "metrics.json"

    def _explode(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(evaluate.json, "dump", _explode)

    with pytest.raises(RuntimeError):
        evaluate.write_held_out_metrics(
            str(destination), **_held_out_metrics_kwargs()
        )

    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []
