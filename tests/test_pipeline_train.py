from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
import yaml
from mlflow.tracking import MlflowClient

from src.features.model_contract import (
    CATEGORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
    FeatureContractError,
)
from src.pipeline import train
from src.pipeline.train import collect_categorical_categories
from src.pipeline.promotion_evidence import (
    ExperimentPlanReceipt,
    PromotionEvidenceStore,
    PromotionEvidenceValidationError,
    create_experiment_plan,
)
from src.pipeline.training_provenance import (
    ProvenanceValidationError,
    RegistryProvenance,
    TrainingSplitManifest,
    build_snapshot_manifest,
    sha256_file,
    snapshot_manifest_path,
    split_manifest_path,
    write_manifest_atomic,
)


@dataclass
class _EvidenceStoredObject:
    """training evidence test용 immutable fake GCS object."""

    payload: bytes
    generation: int
    metageneration: int
    time_created: datetime


class _EvidenceBlob:
    """PromotionEvidenceStore가 쓰는 최소 Blob API fake."""

    def __init__(
        self,
        bucket: "_EvidenceBucket",
        name: str,
        generation: int | None,
    ) -> None:
        self._bucket = bucket
        self.name = name
        self._requested_generation = generation
        self.generation: int | None = generation
        self.metageneration: int | None = None
        self.time_created: datetime | None = None

    def upload_from_string(
        self, payload: bytes, *, content_type: str, if_generation_match: int
    ) -> None:
        assert content_type == "application/json"
        self._bucket.create(self.name, payload, if_generation_match=if_generation_match)

    def reload(self) -> None:
        stored = self._bucket.get(self.name, self._requested_generation)
        self.generation = stored.generation
        self.metageneration = stored.metageneration
        self.time_created = stored.time_created

    def download_as_bytes(self) -> bytes:
        return self._bucket.get(self.name, self._requested_generation).payload


class _EvidenceBucket:
    """generation-pinned read와 create-only write를 제공하는 fake bucket."""

    def __init__(self) -> None:
        self._objects: dict[tuple[str, int], _EvidenceStoredObject] = {}

    def blob(self, name: str, generation: int | None = None) -> _EvidenceBlob:
        return _EvidenceBlob(self, name, generation)

    def create(self, name: str, payload: bytes, *, if_generation_match: int) -> None:
        if if_generation_match != 0 or any(key[0] == name for key in self._objects):
            raise RuntimeError("create-only precondition failed")
        self._objects[(name, 1)] = _EvidenceStoredObject(
            payload=payload,
            generation=1,
            metageneration=1,
            time_created=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )

    def get(self, name: str, generation: int | None) -> _EvidenceStoredObject:
        requested = 1 if generation is None else generation
        try:
            return self._objects[(name, requested)]
        except KeyError:
            raise RuntimeError("object generation not found") from None


class _EvidenceStorageClient:
    """단일 evidence bucket을 제공하는 fake storage client."""

    def __init__(self, bucket: _EvidenceBucket) -> None:
        self._bucket = bucket

    def bucket(self, name: str) -> _EvidenceBucket:
        assert name == "evidence"
        return self._bucket


class _MetricPublishFailureStore:
    """metric publish 실패가 학습 성공으로 위장되지 않는지 검증하는 adapter."""

    def __init__(self, delegate: PromotionEvidenceStore) -> None:
        self._delegate = delegate

    def verify_plan_receipt(self, receipt: ExperimentPlanReceipt):
        return self._delegate.verify_plan_receipt(receipt)

    def publish_held_out_metric(self, evidence: object) -> object:
        raise PromotionEvidenceValidationError("promotion evidence publish에 실패했습니다")


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
            "feature_columns_path": "ignored/feature_columns.json",
            "categorical_columns_path": "ignored/categorical_columns.json",
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


def test_require_binary_labels_rejects_all_negative_with_counts() -> None:
    """#421: 양성 0건이면 학습 전에 행 수·양성/음성 개수가 드러나는 에러로 막는다."""
    with pytest.raises(ValueError, match="단일 클래스") as exc_info:
        train.require_binary_labels(pd.Series([0] * 10), stage="학습 데이터셋")

    message = str(exc_info.value)
    assert "학습 데이터셋" in message
    assert "rows=10" in message
    assert "positive(clicked=1)=0" in message
    assert "negative(clicked=0)=10" in message


def test_require_binary_labels_rejects_all_positive() -> None:
    # 음성 0건도 같은 이유로 지표(ROC-AUC/LogLoss)가 정의되지 않는다.
    with pytest.raises(ValueError, match="단일 클래스"):
        train.require_binary_labels(pd.Series([1] * 5), stage="Train split")


def test_require_binary_labels_returns_counts_when_both_present() -> None:
    assert train.require_binary_labels(pd.Series([0, 0, 1]), stage="Train split") == (1, 2)


def test_compute_auto_scale_pos_weight_returns_negative_over_positive() -> None:
    assert train.compute_auto_scale_pos_weight(pd.Series([0, 0, 0, 1])) == 3.0


def test_compute_auto_scale_pos_weight_rejects_zero_positive() -> None:
    """#421: neg/pos 0 나눗셈(RuntimeWarning divide by zero → inf) 경로를 fail-closed로 막는다."""
    with pytest.raises(ValueError, match="양성"):
        train.compute_auto_scale_pos_weight(pd.Series([0, 0, 0]))


def test_main_rejects_single_class_dataset_before_training(tmp_path, monkeypatch) -> None:
    """#421: 라벨이 전부 0인 데이터셋은 학습·저장·등록 전에 중단되어야 한다."""
    tracking_uri = (tmp_path / "mlruns").as_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    config_path = tmp_path / "config.yaml"
    _write_train_config(config_path)
    dataset = _synthetic_ctr_dataset(n=240)
    dataset["clicked"] = 0
    dataset.to_csv(tmp_path / "training_dataset.csv", index=False)

    with pytest.raises(ValueError, match="단일 클래스") as exc_info:
        _run_train(tmp_path, config_path)

    message = str(exc_info.value)
    assert "rows=240" in message
    assert "positive(clicked=1)=0" in message

    # 모델 파일도, registry 버전도 생기지 않아야 한다(쓰레기 버전 방지).
    assert not (tmp_path / "model.joblib").exists()
    client = MlflowClient(tracking_uri=tracking_uri)
    assert client.search_model_versions("name='ctr-model'") == []


def test_main_rejects_single_class_split(tmp_path, monkeypatch) -> None:
    """#421: 데이터셋 전체엔 양성이 있어도 분할 결과가 단일 클래스면 막는다.

    양성이 2건뿐이면 stratified split이 train에만 양성을 몰아주고 val/test는
    전부 음성이 된다 — 이 상태로 진행하면 평가 단계 log_loss에서 터진다.
    """
    tracking_uri = (tmp_path / "mlruns").as_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    config_path = tmp_path / "config.yaml"
    _write_train_config(config_path)
    dataset = _synthetic_ctr_dataset(n=200)
    dataset["clicked"] = 0
    dataset.loc[[0, 1], "clicked"] = 1
    dataset.to_csv(tmp_path / "training_dataset.csv", index=False)

    with pytest.raises(ValueError, match="단일 클래스") as exc_info:
        _run_train(tmp_path, config_path)

    assert "split" in str(exc_info.value)
    assert not (tmp_path / "model.joblib").exists()
    client = MlflowClient(tracking_uri=tracking_uri)
    assert client.search_model_versions("name='ctr-model'") == []


def test_main_defer_registration_returns_pending_without_registering(tmp_path, monkeypatch) -> None:
    """#421: defer_registration=True면 run 로깅·아티팩트는 그대로 남기되
    registered model 버전은 만들지 않고, 호출자가 평가 통과 뒤 직접 등록한다."""
    tracking_uri = (tmp_path / "mlruns").as_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    config_path = tmp_path / "config.yaml"
    _write_train_config(config_path)
    _synthetic_ctr_dataset(n=200).to_csv(tmp_path / "training_dataset.csv", index=False)

    outcome = train.main(
        config_path=str(config_path),
        data_path=str(tmp_path / "training_dataset.csv"),
        model_output=str(tmp_path / "model.joblib"),
        test_set_output=str(tmp_path / "test_set.csv"),
        feature_columns_output=str(tmp_path / "feature_columns.json"),
        categorical_columns_output=str(tmp_path / "categorical_columns.json"),
        test_size=0.2,
        val_size=0.2,
        random_state=42,
        defer_registration=True,
    )

    client = MlflowClient(tracking_uri=tracking_uri)
    # 등록은 아직 없다. 반면 run 로깅(메트릭)과 모델 아티팩트는 이미 남아 있다.
    assert client.search_model_versions("name='ctr-model'") == []
    assert outcome.registered_version is None
    assert (tmp_path / "model.joblib").exists()
    assert "val_roc_auc" in client.get_run(outcome.run_id).data.metrics

    pending = outcome.pending_registration
    assert pending is not None
    assert pending.model_name == "ctr-model"
    assert pending.model_uri == f"runs:/{outcome.run_id}/model"

    # 평가 통과 후 호출하면 그때 버전이 생기고 태그도 함께 붙는다.
    version = train.register_pending_model(pending)
    [registered] = client.search_model_versions("name='ctr-model'")
    assert registered.version == version
    assert "val_roc_auc" in client.get_model_version("ctr-model", version).tags


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
            feature_columns_output=str(tmp_path / f"feature_columns_{suffix}.json"),
            categorical_columns_output=str(tmp_path / f"categorical_columns_{suffix}.json"),
            test_size=0.2,
            val_size=0.2,
            random_state=42,
        )

    run_once("v1")

    with (tmp_path / "feature_columns_v1.json").open(encoding="utf-8") as stream:
        assert tuple(json.load(stream)) == MODEL_FEATURE_COLUMNS
    with (tmp_path / "categorical_columns_v1.json").open(encoding="utf-8") as stream:
        categories = json.load(stream)
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
        feature_columns_output=str(tmp_path / "feature_columns.json"),
        categorical_columns_output=str(tmp_path / "categorical_columns.json"),
        test_size=0.2,
        val_size=0.2,
        random_state=42,
    )

    # 모델 파일은 registry 등록 실패와 무관하게 이미 저장되어 있어야 한다.
    assert model_output.exists()


def test_register_pending_model_propagates_registry_failure(monkeypatch) -> None:
    """#421 리뷰(중간): 미룬 등록의 실패는 삼키면 안 된다.

    run이 닫힌 뒤 호출되므로 "끝난 run을 FAILED로 만들지 않는다"는 best-effort
    근거가 성립하지 않는다. 삼키면 run-pipeline이 exit 0으로 끝나고, 후속
    promote-model이 어제 버전(=이미 champion)을 집어 ALREADY_CHAMPION으로
    조용히 지나가 "평가까지 통과했는데 신규 후보가 없는 날"이 드러나지 않는다.
    """

    def fake_register_model_raises(model_uri, model_name, tags=None):
        raise RuntimeError("registry 백엔드 없음(시뮬레이션)")

    monkeypatch.setattr(train, "register_model", fake_register_model_raises)
    pending = train.PendingRegistration(
        model_uri="runs:/abc123/model", model_name="ctr-model", tags={}
    )

    with pytest.raises(RuntimeError, match="registry 백엔드 없음"):
        train.register_pending_model(pending)


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
        feature_columns_output=str(tmp_path / "feature_columns.json"),
        categorical_columns_output=str(tmp_path / "categorical_columns.json"),
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


def test_main_logs_training_dataset_lineage(tmp_path, monkeypatch) -> None:
    """run의 Datasets(input)에 학습 데이터셋이 provenance 태그·행 수와 함께 기록되는지
    검증(#359). params와 별개인 dataset lineage."""
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
        feature_columns_output=str(tmp_path / "feature_columns.json"),
        categorical_columns_output=str(tmp_path / "categorical_columns.json"),
        test_size=0.2,
        val_size=0.2,
        random_state=42,
        extra_params={"assembly_source": "feast", "feature_service": "ctr_training_v1"},
    )

    client = MlflowClient(tracking_uri=tracking_uri)
    experiment = client.get_experiment_by_name("ctr-model-training")
    [run] = client.search_runs([experiment.experiment_id])
    [dataset_input] = run.inputs.dataset_inputs
    assert dataset_input.dataset.name == "training_dataset"
    input_tags = {tag.key: tag.value for tag in dataset_input.tags}
    # 용도(context)·provenance·행 수가 dataset input 태그로 남는다.
    assert input_tags["mlflow.data.context"] == "training"
    assert input_tags["assembly_source"] == "feast"
    assert input_tags["feature_service"] == "ctr_training_v1"
    assert int(input_tags["rows"]) == len(_synthetic_ctr_dataset())


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
        feature_columns_output=str(tmp_path / "feature_columns.json"),
        categorical_columns_output=str(tmp_path / "categorical_columns.json"),
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

    outcome = _run_train(tmp_path, config_path)
    assert 0.0 < outcome.sampling_rate < 1.0

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


def test_main_downsampling_logs_calibration_artifact_in_main_run(tmp_path, monkeypatch) -> None:
    # #390: downsampling 학습은 calibration을 별도 등록하지 않고 main과 같은 run의
    # 아티팩트(calibration/calibration.json)로 로깅한다(run_id 종속). 서빙은 main run_id로
    # 이 아티팩트를 읽어 체이닝한다.
    tracking_uri = (tmp_path / "mlruns").as_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    config_path = tmp_path / "config.yaml"
    _write_train_config_with(config_path, sampling_rate=0.5)
    _synthetic_ctr_dataset(n=200).to_csv(tmp_path / "training_dataset.csv", index=False)

    _run_train(tmp_path, config_path)

    client = MlflowClient(tracking_uri=tracking_uri)
    [main_version] = client.search_model_versions("name='ctr-model'")
    # calibration은 별도 등록 모델이 아니다.
    assert client.search_model_versions("name='ctr-calibration-model'") == []
    # 대신 main과 같은 run에 calibration 아티팩트가 있어야 한다.
    artifacts = client.list_artifacts(main_version.run_id, "calibration")
    assert any(entry.path.endswith("calibration.json") for entry in artifacts)


def test_main_no_downsampling_logs_no_calibration_artifact(tmp_path, monkeypatch) -> None:
    # 하위호환: downsampling 미사용(w=1.0)이면 calibration 아티팩트를 로깅하지 않는다.
    tracking_uri = (tmp_path / "mlruns").as_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    config_path = tmp_path / "config.yaml"
    _write_train_config_with(config_path, sampling_rate=1.0)
    _synthetic_ctr_dataset(n=200).to_csv(tmp_path / "training_dataset.csv", index=False)

    _run_train(tmp_path, config_path)

    client = MlflowClient(tracking_uri=tracking_uri)
    [main_version] = client.search_model_versions("name='ctr-model'")
    assert client.list_artifacts(main_version.run_id, "calibration") == []


def test_downsampling_main_without_calibration_artifact_fails_closed(tmp_path, monkeypatch) -> None:
    # #390 fail-closed(PR #395 리뷰 5): downsampling main(sampling_rate<1.0 tag)인데 그 run에
    # calibration 아티팩트가 없으면, 서빙 로드가 ModelArtifactError로 기동을 거부해야 한다
    # (보정 안 된 편향 확률 서빙 방지). 정상 경로는 아티팩트가 항상 있지만, 이 마지막 보루를
    # 회귀 테스트로 고정한다 — sampling_rate=1.0으로 학습해 calibration 아티팩트 없는 run을
    # 만든 뒤, main 버전 tag를 0.5로 덮어써 "downsampling인데 아티팩트 없음" 상황을 재현한다.
    from mlflow.tracking import MlflowClient as _Client

    from src.serving.model_loader import (
        ModelArtifactError,
        RegistryModelSettings,
        load_reranker_with_lineage,
    )

    tracking_uri = (tmp_path / "mlruns").as_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    config_path = tmp_path / "config.yaml"
    _write_train_config_with(config_path, sampling_rate=1.0)
    _synthetic_ctr_dataset(n=200).to_csv(tmp_path / "training_dataset.csv", index=False)

    _run_train(tmp_path, config_path)

    client = _Client(tracking_uri=tracking_uri)
    [main_version] = client.search_model_versions("name='ctr-model'")
    # calibration 아티팩트가 없는 run인데 downsampling인 것처럼 tag를 덮어쓴다.
    assert client.list_artifacts(main_version.run_id, "calibration") == []
    client.set_model_version_tag("ctr-model", main_version.version, "sampling_rate", "0.5")
    client.set_registered_model_alias("ctr-model", "champion", main_version.version)

    with pytest.raises(ModelArtifactError, match="calibration"):
        load_reranker_with_lineage(
            RegistryModelSettings(
                tracking_uri=tracking_uri, model_name="ctr-model", alias="champion"
            )
        )


def test_main_logs_onnx_artifact_and_serving_loads_it(tmp_path, monkeypatch) -> None:
    # #302/#179: 학습이 model_onnx/ 아티팩트를 로깅하고, 서빙 로더가 그 run에서 ONNX로
    # (joblib 아님) Reranker를 로드하며 joblib 예측과 허용오차 내로 동일해야 한다.
    from src.serving.model_loader import MlflowModelSettings, load_mlflow_model
    from src.serving.onnx_model import OnnxProbabilityModel
    from src.serving.schemas import CandidateVideo

    tracking_uri = (tmp_path / "mlruns").as_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    config_path = tmp_path / "config.yaml"
    _write_train_config(config_path)
    _synthetic_ctr_dataset(n=200).to_csv(tmp_path / "training_dataset.csv", index=False)

    _run_train(tmp_path, config_path)

    client = MlflowClient(tracking_uri=tracking_uri)
    [version] = client.search_model_versions("name='ctr-model'")
    artifact_paths = {artifact.path for artifact in client.list_artifacts(version.run_id)}
    assert "model_onnx" in artifact_paths

    reranker = load_mlflow_model(
        MlflowModelSettings(tracking_uri=tracking_uri, run_id=version.run_id)
    )
    assert isinstance(reranker.model, OnnxProbabilityModel)

    serve_frame = _synthetic_ctr_dataset(n=15, seed=11).drop(columns=["clicked"])
    candidates = [
        CandidateVideo(video_id=f"v{i}", features=record)
        for i, record in enumerate(serve_frame.to_dict(orient="records"))
    ]
    items = reranker.rerank(candidates)
    assert len(items) == len(candidates)

    # ONNX 서빙 점수가 원본 joblib LightGBM과 허용오차 내로 동일한지 직접 대조.
    import joblib

    joblib_model = joblib.load(tmp_path / "model.joblib")
    with (tmp_path / "categorical_columns.json").open(encoding="utf-8") as stream:
        categories = json.load(stream)
    cast = serve_frame.copy()
    for col, cats in categories.items():
        cast[col] = pd.Categorical(cast[col], categories=cats)
    lgbm_positive = joblib_model.predict_proba(cast[list(MODEL_FEATURE_COLUMNS)])[:, 1]
    # rerank 결과는 점수 내림차순 정렬이므로 video_id 인덱스로 원위치를 복원해 비교한다.
    onnx_by_id = {int(item.video_id[1:]): item.ctr_score for item in items}
    onnx_positive = np.array([onnx_by_id[i] for i in range(len(candidates))])
    np.testing.assert_allclose(onnx_positive, lgbm_positive, atol=1e-4)


# --- 실험용 피처 오버라이드 (#405) ---
# 계약 정본: docs/specs/2026-07-31-experiment-feature-override.md


def _train_once(tmp_path, config_path, data_path, suffix, **kwargs):
    return train.main(
        config_path=str(config_path),
        data_path=str(data_path),
        model_output=str(tmp_path / f"model_{suffix}.joblib"),
        test_set_output=str(tmp_path / f"test_set_{suffix}.csv"),
        feature_columns_output=str(tmp_path / f"feature_columns_{suffix}.json"),
        categorical_columns_output=str(tmp_path / f"categorical_{suffix}.json"),
        test_size=0.2,
        val_size=0.2,
        random_state=42,
        **kwargs,
    )


def _prepared_dataset(tmp_path, monkeypatch, *, extra_column: str | None = None):
    tracking_uri = (tmp_path / "mlruns").as_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    config_path = tmp_path / "config.yaml"
    _write_train_config(config_path)
    dataset = _synthetic_ctr_dataset()
    if extra_column is not None:
        # 실험 피처는 데이터셋에 **이미 있는** 컬럼만 승격시킨다. 여기서는 조립
        # 단계가 이미 넣어준 상황을 흉내낸다.
        dataset[extra_column] = dataset["view_count"] / 7.0
    data_path = tmp_path / "training_dataset.csv"
    dataset.to_csv(data_path, index=False)
    return config_path, data_path, tracking_uri


def _prepared_verified_dataset(tmp_path, monkeypatch):
    config_path, data_path, tracking_uri = _prepared_dataset(tmp_path, monkeypatch)
    manifest = build_snapshot_manifest(
        dataset_path=Path(data_path),
        events_start_date="2026-07-01",
        events_end_date="2026-07-30",
        feature_service="ctr_training_v1",
        registry=RegistryProvenance(
            uri="gs://bucket/registry.db",
            generation="7",
            sha256="a" * 64,
        ),
        code_archive_sha=None,
    )
    write_manifest_atomic(manifest, snapshot_manifest_path(Path(data_path)))
    return config_path, data_path, tracking_uri


def _evidence_store() -> PromotionEvidenceStore:
    return PromotionEvidenceStore(
        "gs://evidence/promotion-evidence",
        client=_EvidenceStorageClient(_EvidenceBucket()),
    )


def _write_plan_receipt(
    tmp_path: Path, store: PromotionEvidenceStore
) -> tuple[Path, ExperimentPlanReceipt]:
    plan = create_experiment_plan(
        hypothesis_id="issue-466-h1",
        control_id="control-revision",
        candidate_ids=("candidate-revision",),
        created_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )
    receipt = store.publish_plan(plan)
    path = tmp_path / "experiment_plan_receipt.json"
    write_manifest_atomic(receipt, path)
    return path, receipt


def _promotion_train_kwargs(tmp_path: Path, receipt_path: Path) -> dict[str, object]:
    return {
        "model_output": str(tmp_path / "model.joblib"),
        "test_set_output": str(tmp_path / "test_set.csv"),
        "feature_columns_output": str(tmp_path / "features.json"),
        "categorical_columns_output": str(tmp_path / "categories.json"),
        "require_snapshot": True,
        "defer_registration": True,
        "experiment_plan_receipt_path": str(receipt_path),
        "promotion_evidence_root": "gs://evidence/promotion-evidence",
    }


def _artifact_paths(client: MlflowClient, run_id: str, artifact_path: str) -> set[str]:
    return {entry.path for entry in client.list_artifacts(run_id, artifact_path)}


def test_main_binds_verified_plan_and_publishes_held_out_metric_inside_run(
    tmp_path, monkeypatch
) -> None:
    config_path, data_path, tracking_uri = _prepared_verified_dataset(tmp_path, monkeypatch)
    store = _evidence_store()
    receipt_path, receipt = _write_plan_receipt(tmp_path, store)

    outcome = train.main(
        config_path=str(config_path),
        data_path=str(data_path),
        promotion_evidence_store=store,
        **_promotion_train_kwargs(tmp_path, receipt_path),
    )

    split = TrainingSplitManifest.model_validate_json(
        split_manifest_path(tmp_path / "test_set.csv").read_text(encoding="utf-8")
    )
    assert split.experiment_plan_receipt == receipt
    assert outcome.held_out_metric_receipt is not None
    metric = store.verify_held_out_metric_receipt(outcome.held_out_metric_receipt)
    assert metric.run_id == outcome.run_id
    assert metric.dataset_split == "test"
    assert metric.model_artifact_sha256 == sha256_file(tmp_path / "model.joblib")
    assert _artifact_paths(
        MlflowClient(tracking_uri=tracking_uri),
        outcome.run_id,
        "reproducibility/metrics",
    ) == {"reproducibility/metrics/held_out_metric_receipt.json"}


@pytest.mark.parametrize(
    "promotion_options",
    [
        {"experiment_plan_receipt_path": "plan.json"},
        {"promotion_evidence_root": "gs://evidence/promotion-evidence"},
    ],
)
def test_main_rejects_incomplete_promotion_options_before_model_fit(
    tmp_path, monkeypatch, promotion_options: dict[str, str]
) -> None:
    config_path, data_path, _ = _prepared_verified_dataset(tmp_path, monkeypatch)
    fit = MagicMock()
    monkeypatch.setattr(train.LGBMModel, "fit", fit)

    with pytest.raises(ValueError, match="함께"):
        train.main(
            config_path=str(config_path),
            data_path=str(data_path),
            require_snapshot=True,
            defer_registration=True,
            **promotion_options,
        )

    fit.assert_not_called()


def test_main_rejects_promotion_evidence_without_verified_snapshot_before_model_fit(
    tmp_path, monkeypatch
) -> None:
    config_path, data_path, _ = _prepared_dataset(tmp_path, monkeypatch)
    store = _evidence_store()
    receipt_path, _ = _write_plan_receipt(tmp_path, store)
    fit = MagicMock()
    monkeypatch.setattr(train.LGBMModel, "fit", fit)
    promotion_kwargs = _promotion_train_kwargs(tmp_path, receipt_path)
    promotion_kwargs["require_snapshot"] = False

    with pytest.raises(ValueError, match="require_snapshot"):
        train.main(
            config_path=str(config_path),
            data_path=str(data_path),
            promotion_evidence_store=store,
            **promotion_kwargs,
        )

    fit.assert_not_called()


def test_main_rejects_tampered_local_plan_receipt_before_model_fit(
    tmp_path, monkeypatch
) -> None:
    config_path, data_path, _ = _prepared_verified_dataset(tmp_path, monkeypatch)
    store = _evidence_store()
    receipt_path, receipt = _write_plan_receipt(tmp_path, store)
    tampered = receipt.model_copy(
        update={"object": receipt.object.model_copy(update={"sha256": "f" * 64})}
    )
    write_manifest_atomic(tampered, receipt_path)
    fit = MagicMock()
    monkeypatch.setattr(train.LGBMModel, "fit", fit)

    with pytest.raises(PromotionEvidenceValidationError, match="sha256"):
        train.main(
            config_path=str(config_path),
            data_path=str(data_path),
            promotion_evidence_store=store,
            **_promotion_train_kwargs(tmp_path, receipt_path),
        )

    fit.assert_not_called()


def test_main_fails_when_held_out_metric_publish_fails_without_metric_receipt_artifact(
    tmp_path, monkeypatch
) -> None:
    config_path, data_path, tracking_uri = _prepared_verified_dataset(tmp_path, monkeypatch)
    store = _evidence_store()
    receipt_path, _ = _write_plan_receipt(tmp_path, store)

    with pytest.raises(PromotionEvidenceValidationError, match="publish"):
        train.main(
            config_path=str(config_path),
            data_path=str(data_path),
            promotion_evidence_store=_MetricPublishFailureStore(store),
            **_promotion_train_kwargs(tmp_path, receipt_path),
        )

    client = MlflowClient(tracking_uri=tracking_uri)
    experiment = client.get_experiment_by_name("ctr-model-training")
    assert experiment is not None
    [failed_run] = client.search_runs([experiment.experiment_id])
    assert _artifact_paths(client, failed_run.info.run_id, "reproducibility/metrics") == set()


def test_main_logs_verified_snapshot_and_split_artifacts(tmp_path, monkeypatch) -> None:
    config_path, data_path, tracking_uri = _prepared_verified_dataset(tmp_path, monkeypatch)

    outcome = train.main(
        config_path=str(config_path),
        data_path=str(data_path),
        model_output=str(tmp_path / "model.joblib"),
        test_set_output=str(tmp_path / "test_set.csv"),
        feature_columns_output=str(tmp_path / "features.json"),
        categorical_columns_output=str(tmp_path / "categories.json"),
        split_seed=11,
        model_seed=12,
        sampler_seed=13,
        require_snapshot=True,
        defer_registration=True,
    )

    client = MlflowClient(tracking_uri=tracking_uri)
    assert _artifact_paths(client, outcome.run_id, "reproducibility/snapshot") == {
        "reproducibility/snapshot/training_dataset.csv",
        "reproducibility/snapshot/snapshot_manifest.json",
    }
    assert _artifact_paths(client, outcome.run_id, "reproducibility/split") == {
        "reproducibility/split/split_manifest.json",
    }
    split = TrainingSplitManifest.model_validate_json(
        split_manifest_path(tmp_path / "test_set.csv").read_text(encoding="utf-8")
    )
    assert (split.split_seed, split.model_seed, split.sampler_seed) == (11, 12, 13)
    assert split.snapshot_sha256
    assert split.feature_columns == list(MODEL_FEATURE_COLUMNS)


def test_main_rejects_stale_snapshot_before_model_fit(tmp_path, monkeypatch) -> None:
    config_path, data_path, _ = _prepared_verified_dataset(tmp_path, monkeypatch)
    data_path.write_text("changed", encoding="utf-8")
    fit = MagicMock()
    monkeypatch.setattr(train.LGBMModel, "fit", fit)

    with pytest.raises(ProvenanceValidationError, match="dataset_sha256"):
        train.main(
            config_path=str(config_path),
            data_path=str(data_path),
            model_output=str(tmp_path / "model.joblib"),
            test_set_output=str(tmp_path / "test_set.csv"),
            feature_columns_output=str(tmp_path / "features.json"),
            categorical_columns_output=str(tmp_path / "categories.json"),
            require_snapshot=True,
            defer_registration=True,
        )

    fit.assert_not_called()
    assert not (tmp_path / "model.joblib").exists()
    assert not (tmp_path / "test_set.csv").exists()


def test_main_rejects_incomplete_explicit_seed_triplet_before_model_fit(
    tmp_path, monkeypatch
) -> None:
    config_path, data_path, _ = _prepared_verified_dataset(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="모두 지정"):
        train.main(
            config_path=str(config_path),
            data_path=str(data_path),
            model_output=str(tmp_path / "model.joblib"),
            test_set_output=str(tmp_path / "test_set.csv"),
            feature_columns_output=str(tmp_path / "features.json"),
            categorical_columns_output=str(tmp_path / "categories.json"),
            split_seed=11,
            model_seed=12,
            require_snapshot=True,
            defer_registration=True,
        )

    assert not (tmp_path / "model.joblib").exists()
    assert not (tmp_path / "test_set.csv").exists()


def test_main_legacy_random_state_records_three_effective_seed_params(tmp_path, monkeypatch) -> None:
    config_path, data_path, tracking_uri = _prepared_dataset(tmp_path, monkeypatch)

    outcome = _train_once(tmp_path, config_path, data_path, "legacy", defer_registration=True)

    client = MlflowClient(tracking_uri=tracking_uri)
    params = client.get_run(outcome.run_id).data.params
    assert params["split_seed"] == "42"
    assert params["model_seed"] == "42"
    assert params["sampler_seed"] == "42"
    assert _artifact_paths(client, outcome.run_id, "reproducibility") == set()


def test_main_verified_model_version_tags_provenance_hashes(tmp_path, monkeypatch) -> None:
    config_path, data_path, tracking_uri = _prepared_verified_dataset(tmp_path, monkeypatch)

    outcome = train.main(
        config_path=str(config_path),
        data_path=str(data_path),
        model_output=str(tmp_path / "model.joblib"),
        test_set_output=str(tmp_path / "test_set.csv"),
        feature_columns_output=str(tmp_path / "features.json"),
        categorical_columns_output=str(tmp_path / "categories.json"),
        require_snapshot=True,
    )

    client = MlflowClient(tracking_uri=tracking_uri)
    [version] = client.search_model_versions("name='ctr-model'")
    tags = client.get_model_version("ctr-model", str(version.version)).tags
    assert tags["snapshot_sha256"]
    assert tags["split_manifest_sha256"]
    assert version.run_id == outcome.run_id


def test_main_without_extra_features_keeps_prod_contract(tmp_path, monkeypatch) -> None:
    """기본값(None)이면 지금까지와 완전히 동일한 입력 순서다(#405 완료조건 3)."""
    config_path, data_path, _ = _prepared_dataset(tmp_path, monkeypatch)

    _train_once(tmp_path, config_path, data_path, "base")

    with (tmp_path / "feature_columns_base.json").open(encoding="utf-8") as stream:
        assert tuple(json.load(stream)) == MODEL_FEATURE_COLUMNS


def test_main_extra_features_appends_after_prod_contract(tmp_path, monkeypatch) -> None:
    config_path, data_path, _ = _prepared_dataset(
        tmp_path, monkeypatch, extra_column="views_per_day"
    )

    _train_once(
        tmp_path, config_path, data_path, "exp", extra_features=["views_per_day"]
    )

    with (tmp_path / "feature_columns_exp.json").open(encoding="utf-8") as stream:
        columns = tuple(json.load(stream))
    # prod 접두부가 그대로여야 ONNX 입력(이름 없는 순서 배열) 해석이 안 깨진다.
    assert columns[: len(MODEL_FEATURE_COLUMNS)] == MODEL_FEATURE_COLUMNS
    assert columns[len(MODEL_FEATURE_COLUMNS) :] == ("views_per_day",)
    # 실험 피처가 prod 계약으로 새어들어가지 않는다(#405 완료조건 1).
    assert "views_per_day" not in MODEL_FEATURE_COLUMNS


def test_main_extra_features_missing_column_routes_to_feast_path(
    tmp_path, monkeypatch
) -> None:
    """데이터셋에 없는 컬럼이면 정규 경로(#399) 안내와 함께 중단한다."""
    config_path, data_path, _ = _prepared_dataset(tmp_path, monkeypatch)

    with pytest.raises(ValueError) as excinfo:
        _train_once(
            tmp_path, config_path, data_path, "missing", extra_features=["views_per_day"]
        )

    message = str(excinfo.value)
    assert "views_per_day" in message
    # 오타인지 부재인지 구분할 수 있게 데이터셋 컬럼 수를 알려준다.
    assert "데이터셋" in message
    # 팀원이 헷갈릴 때 바로 정규 경로로 라우팅되도록 한다.
    assert "#399" in message
    assert "feast apply" in message
    # 학습을 시작하기 전에 막았으므로 모델 파일이 없어야 한다.
    assert not (tmp_path / "model_missing.joblib").exists()


def test_main_extra_features_tags_registered_version(tmp_path, monkeypatch) -> None:
    """실험 모델은 registry tag로 구분된다(#405 완료조건 4).

    #406 리뷰 반영 후로는 `--extra-features`만 줘도 실험 네임스페이스를 쓰므로
    prod 이름에는 아무것도 등록되지 않는다 — tag는 그 안에서 한 번 더 구분한다.
    """
    config_path, data_path, tracking_uri = _prepared_dataset(
        tmp_path, monkeypatch, extra_column="views_per_day"
    )

    _train_once(
        tmp_path, config_path, data_path, "tag", extra_features=["views_per_day"]
    )

    client = MlflowClient(tracking_uri=tracking_uri)
    assert client.search_model_versions("name='ctr-model'") == []
    experiment_model = "ctr-model-exp-views-per-day"
    [registered] = client.search_model_versions(f"name='{experiment_model}'")
    tags = client.get_model_version(experiment_model, registered.version).tags
    assert tags["experiment_features"] == "views_per_day"


def test_main_without_extra_features_has_no_experiment_tag(tmp_path, monkeypatch) -> None:
    config_path, data_path, tracking_uri = _prepared_dataset(tmp_path, monkeypatch)

    _train_once(tmp_path, config_path, data_path, "notag")

    client = MlflowClient(tracking_uri=tracking_uri)
    [registered] = client.search_model_versions("name='ctr-model'")
    tags = client.get_model_version("ctr-model", registered.version).tags
    assert "experiment_features" not in tags


def test_main_experiment_registers_under_separate_registry_name(tmp_path, monkeypatch) -> None:
    """실험 학습은 prod와 다른 registry 이름으로 등록된다(#406 완료조건 1)."""
    config_path, data_path, tracking_uri = _prepared_dataset(tmp_path, monkeypatch)

    _train_once(tmp_path, config_path, data_path, "ns", experiment="views_per_day")

    client = MlflowClient(tracking_uri=tracking_uri)
    # prod 이름으로는 아무것도 등록되지 않는다 — 승격 게이트가 보는 대상이 오염되지 않는다.
    assert client.search_model_versions("name='ctr-model'") == []
    [registered] = client.search_model_versions("name='ctr-model-exp-views-per-day'")
    assert str(registered.version) == "1"


def test_main_prod_path_still_uses_prod_registry_name(tmp_path, monkeypatch) -> None:
    """experiment 미지정이면 기존과 동일하게 prod 이름으로 등록된다(#406 회귀 방지)."""
    config_path, data_path, tracking_uri = _prepared_dataset(tmp_path, monkeypatch)

    _train_once(tmp_path, config_path, data_path, "prodns")

    client = MlflowClient(tracking_uri=tracking_uri)
    assert len(client.search_model_versions("name='ctr-model'")) == 1
def test_main_duplicate_extra_features_stops_before_writing_test_set(
    tmp_path, monkeypatch
) -> None:
    """계약 거부가 부수효과보다 먼저다(#405 리뷰 2).

    중복 지정은 계약 위반인데, 예전에는 Step 3에서야 걸려 그 전에 held-out
    test set 파일이 이미 덮어써지고 FAILED run만 남았다.
    """
    config_path, data_path, _ = _prepared_dataset(
        tmp_path, monkeypatch, extra_column="views_per_day"
    )
    test_set_path = tmp_path / "test_set_dup.csv"
    test_set_path.write_text("sentinel", encoding="utf-8")

    with pytest.raises(FeatureContractError):
        _train_once(
            tmp_path,
            config_path,
            data_path,
            "dup",
            extra_features=["views_per_day", "views_per_day"],
        )

    # 공유 test set 파일이 그대로 남아 있어야 한다.
    assert test_set_path.read_text(encoding="utf-8") == "sentinel"
    assert not (tmp_path / "model_dup.joblib").exists()


def test_main_rejects_non_numeric_extra_feature(tmp_path, monkeypatch) -> None:
    """범주형 실험 피처는 문서상 비범위이고 코드에서도 막힌다(#405 리뷰 5).

    막지 않으면 LightGBM fit()에서야 터지는데, 그때는 test set 저장과 run 생성이
    이미 끝난 뒤다.
    """
    config_path, data_path, _ = _prepared_dataset(tmp_path, monkeypatch)
    dataset = pd.read_csv(data_path)
    dataset["region"] = ["seoul", "busan"] * (len(dataset) // 2)
    dataset.to_csv(data_path, index=False)

    with pytest.raises(ValueError) as excinfo:
        _train_once(tmp_path, config_path, data_path, "cat", extra_features=["region"])

    message = str(excinfo.value)
    assert "region" in message
    assert "수치형" in message
    assert not (tmp_path / "model_cat.joblib").exists()
