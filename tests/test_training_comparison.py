from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mlflow
import pandas as pd
import pytest
from mlflow.tracking import MlflowClient

from src.pipeline.training_comparison import (
    ComparisonValidationError,
    verify_training_comparison,
)
from src.pipeline.promotion_evidence import (
    ExperimentPlanReceipt,
    HeldOutMetricEvidence,
    HeldOutMetricReceipt,
    PromotionEvidenceStore,
    create_experiment_plan,
)
from src.pipeline.training_provenance import (
    RegistryProvenance,
    TrainingSeeds,
    build_snapshot_manifest,
    build_split_manifest,
    sha256_file,
    write_manifest_atomic,
)


@dataclass
class _EvidenceStoredObject:
    """comparison evidence test용 generation-pinned object."""

    payload: bytes
    generation: int
    metageneration: int
    time_created: datetime


class _EvidenceBlob:
    """PromotionEvidenceStore에 필요한 Blob API만 제공하는 fake."""

    def __init__(
        self, bucket: "_EvidenceBucket", name: str, generation: int | None
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
    """plan은 run start보다 앞서고 metric은 active run 안에 생기게 하는 fake."""

    def __init__(self) -> None:
        self._objects: dict[tuple[str, int], _EvidenceStoredObject] = {}
        self.next_plan_time_created: datetime | None = None
        self.next_metric_time_created: datetime | None = None

    def blob(self, name: str, generation: int | None = None) -> _EvidenceBlob:
        return _EvidenceBlob(self, name, generation)

    def create(self, name: str, payload: bytes, *, if_generation_match: int) -> None:
        if if_generation_match != 0 or any(key[0] == name for key in self._objects):
            raise RuntimeError("create-only precondition failed")
        now = datetime.now(timezone.utc)
        if "/plans/" in name and self.next_plan_time_created is not None:
            time_created = self.next_plan_time_created
            self.next_plan_time_created = None
        elif "/metrics/" in name and self.next_metric_time_created is not None:
            time_created = self.next_metric_time_created
            self.next_metric_time_created = None
        else:
            time_created = (now - timedelta(seconds=1)) if "/plans/" in name else now
        self._objects[(name, 1)] = _EvidenceStoredObject(
            payload=payload,
            generation=1,
            metageneration=1,
            time_created=time_created,
        )

    def get(self, name: str, generation: int | None) -> _EvidenceStoredObject:
        requested = 1 if generation is None else generation
        try:
            return self._objects[(name, requested)]
        except KeyError:
            raise RuntimeError("object generation not found") from None

    def replace_payload(self, name: str, payload: bytes) -> None:
        stored = self.get(name, 1)
        self._objects[(name, 1)] = _EvidenceStoredObject(
            payload=payload,
            generation=stored.generation,
            metageneration=stored.metageneration,
            time_created=stored.time_created,
        )

    def replace_time_created(self, name: str, time_created: datetime) -> None:
        stored = self.get(name, 1)
        self._objects[(name, 1)] = _EvidenceStoredObject(
            payload=stored.payload,
            generation=stored.generation,
            metageneration=stored.metageneration,
            time_created=time_created,
        )


class _EvidenceStorageClient:
    """단일 promotion evidence bucket fake."""

    def __init__(self, bucket: _EvidenceBucket) -> None:
        self._bucket = bucket

    def bucket(self, name: str) -> _EvidenceBucket:
        assert name == "evidence"
        return self._bucket


def _evidence_store() -> tuple[PromotionEvidenceStore, _EvidenceBucket]:
    bucket = _EvidenceBucket()
    return PromotionEvidenceStore(
        "gs://evidence/promotion-evidence",
        client=_EvidenceStorageClient(bucket),
    ), bucket


def _write_source_dataset(tmp_path: Path) -> tuple[Path, Path]:
    dataset_path = tmp_path / "source.csv"
    pd.DataFrame({"views": [1, 2, 3, 4], "clicked": [0, 1, 0, 1]}).to_csv(
        dataset_path, index=False
    )
    snapshot = build_snapshot_manifest(
        dataset_path=dataset_path,
        events_start_date="2026-07-01",
        events_end_date="2026-07-30",
        feature_service="ctr_training_v1",
        registry=RegistryProvenance(
            uri="gs://bucket/registry.db",
            generation="7",
            sha256="a" * 64,
        ),
        code_archive_sha=None,
        created_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )
    snapshot_path = tmp_path / "snapshot_manifest.json"
    write_manifest_atomic(snapshot, snapshot_path)
    return dataset_path, snapshot_path


def _log_verified_run(
    tmp_path: Path,
    *,
    experiment_id: str,
    dataset_path: Path,
    snapshot_path: Path,
    split_seed: int = 42,
    model_seed: int = 42,
    sampler_seed: int = 42,
    feature_columns: list[str] | None = None,
    split_positions: dict[str, list[int]] | None = None,
    include_snapshot: bool = True,
    include_split: bool = True,
    tamper_dataset: bool = False,
    tamper_snapshot: bool = False,
) -> str:
    client = MlflowClient()
    with mlflow.start_run(experiment_id=experiment_id) as run:
        run_id = run.info.run_id

    run_root = tmp_path / run_id
    run_root.mkdir()
    logged_dataset = run_root / "training_dataset.csv"
    shutil.copyfile(dataset_path, logged_dataset)
    if tamper_dataset:
        logged_dataset.write_text(
            logged_dataset.read_text(encoding="utf-8").replace("1,0", "999,0"),
            encoding="utf-8",
        )

    logged_snapshot = run_root / "snapshot_manifest.json"
    shutil.copyfile(snapshot_path, logged_snapshot)
    original_snapshot_sha256 = sha256_file(logged_snapshot)
    if tamper_snapshot:
        logged_snapshot.write_text(
            logged_snapshot.read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )

    if include_snapshot:
        client.log_artifact(
            run_id,
            str(logged_dataset),
            artifact_path="reproducibility/snapshot",
        )
        client.log_artifact(
            run_id,
            str(logged_snapshot),
            artifact_path="reproducibility/snapshot",
        )

    if include_split:
        split = build_split_manifest(
            run_id=run_id,
            snapshot=build_snapshot_manifest(
                dataset_path=dataset_path,
                events_start_date="2026-07-01",
                events_end_date="2026-07-30",
                feature_service="ctr_training_v1",
                registry=RegistryProvenance(
                    uri="gs://bucket/registry.db",
                    generation="7",
                    sha256="a" * 64,
                ),
                code_archive_sha=None,
                created_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
            ),
            snapshot_manifest_sha256=original_snapshot_sha256,
            seeds=TrainingSeeds(
                split_seed=split_seed,
                model_seed=model_seed,
                sampler_seed=sampler_seed,
            ),
            test_size=0.25,
            val_size=0.25,
            split_positions=split_positions
            or {"train": [0, 1], "validation": [2], "test": [3]},
            feature_columns=feature_columns or ["views"],
        )
        split_path = run_root / "split_manifest.json"
        write_manifest_atomic(split, split_path)
        client.log_artifact(
            run_id,
            str(split_path),
            artifact_path="reproducibility/split",
        )
    return run_id


def _log_verified_run_with_promotion_evidence(
    tmp_path: Path,
    *,
    experiment_id: str,
    dataset_path: Path,
    snapshot_path: Path,
    plan_receipt: ExperimentPlanReceipt,
    store: PromotionEvidenceStore,
    feature_columns: list[str] | None = None,
    metric_overrides: dict[str, object] | None = None,
) -> tuple[str, HeldOutMetricReceipt]:
    """active MLflow run 안에서 receipt·model artifact를 함께 남기는 fixture."""
    client = MlflowClient()
    with mlflow.start_run(experiment_id=experiment_id) as run:
        run_id = run.info.run_id
        run_root = tmp_path / run_id
        run_root.mkdir()
        logged_dataset = run_root / "training_dataset.csv"
        logged_snapshot = run_root / "snapshot_manifest.json"
        shutil.copyfile(dataset_path, logged_dataset)
        shutil.copyfile(snapshot_path, logged_snapshot)
        snapshot_manifest_sha256 = sha256_file(logged_snapshot)
        snapshot = build_snapshot_manifest(
            dataset_path=dataset_path,
            events_start_date="2026-07-01",
            events_end_date="2026-07-30",
            feature_service="ctr_training_v1",
            registry=RegistryProvenance(
                uri="gs://bucket/registry.db",
                generation="7",
                sha256="a" * 64,
            ),
            code_archive_sha=None,
            created_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
        )
        split = build_split_manifest(
            run_id=run_id,
            snapshot=snapshot,
            snapshot_manifest_sha256=snapshot_manifest_sha256,
            seeds=TrainingSeeds(split_seed=42, model_seed=42, sampler_seed=42),
            test_size=0.25,
            val_size=0.25,
            split_positions={"train": [0, 1], "validation": [2], "test": [3]},
            feature_columns=feature_columns or ["views"],
            experiment_plan_receipt=plan_receipt,
        )
        split_path = run_root / "split_manifest.json"
        write_manifest_atomic(split, split_path)
        model_path = run_root / "model.joblib"
        model_path.write_bytes(f"model-{run_id}".encode("utf-8"))

        client.log_artifact(
            run_id, str(logged_dataset), artifact_path="reproducibility/snapshot"
        )
        client.log_artifact(
            run_id, str(logged_snapshot), artifact_path="reproducibility/snapshot"
        )
        client.log_artifact(
            run_id, str(split_path), artifact_path="reproducibility/split"
        )
        client.log_artifact(run_id, str(model_path), artifact_path="model")

        metric = HeldOutMetricEvidence(
            run_id=run_id,
            plan_receipt=plan_receipt,
            value=0.8,
            split_manifest_sha256=sha256_file(split_path),
            test_membership_sha256=split.splits["test"].membership_sha256,
            model_artifact_path="model/model.joblib",
            model_artifact_sha256=sha256_file(model_path),
        )
        if metric_overrides:
            metric = metric.model_copy(update=metric_overrides)
        metric_receipt = store.publish_held_out_metric(metric)
        metric_receipt_path = run_root / "held_out_metric_receipt.json"
        write_manifest_atomic(metric_receipt, metric_receipt_path)
        client.log_artifact(
            run_id,
            str(metric_receipt_path),
            artifact_path="reproducibility/metrics",
        )
    return run_id, metric_receipt


def _comparison_fixture(tmp_path: Path, monkeypatch) -> tuple[Path, str, str]:
    tracking_uri = (tmp_path / "mlruns").as_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    mlflow.set_tracking_uri(tracking_uri)
    experiment_id = mlflow.create_experiment("comparison")
    dataset_path, snapshot_path = _write_source_dataset(tmp_path)
    return dataset_path, snapshot_path, experiment_id


def test_verify_training_comparison_writes_output_and_challenger_artifact(
    tmp_path, monkeypatch
) -> None:
    dataset_path, snapshot_path, experiment_id = _comparison_fixture(tmp_path, monkeypatch)
    baseline_run = _log_verified_run(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        feature_columns=["views"],
    )
    challenger_run = _log_verified_run(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        feature_columns=["views", "new_feature"],
    )
    output = tmp_path / "comparison.json"

    result = verify_training_comparison(
        baseline_run,
        challenger_run,
        output,
        experiment_plan_id="experiment-plan-predeclared",
    )

    assert result.validation_status == "verified"
    assert result.effective_seeds == TrainingSeeds(
        split_seed=42,
        model_seed=42,
        sampler_seed=42,
    )
    assert result.experiment_plan_id == "experiment-plan-predeclared"
    assert output.is_file()
    client = MlflowClient(tracking_uri=(tmp_path / "mlruns").as_uri())
    assert any(
        entry.path == f"reproducibility/comparisons/{result.comparison_id}.json"
        for entry in client.list_artifacts(challenger_run, "reproducibility/comparisons")
    )
    output_payload = json.loads(output.read_text(encoding="utf-8"))
    assert output_payload["comparison_id"] == result.comparison_id
    assert output_payload["effective_seeds"] == {
        "split_seed": 42,
        "model_seed": 42,
        "sampler_seed": 42,
    }
    assert output_payload["experiment_plan_id"] == "experiment-plan-predeclared"


def test_verify_comparison_rechecks_receipts_and_records_verified_metrics(
    tmp_path, monkeypatch
) -> None:
    dataset_path, snapshot_path, experiment_id = _comparison_fixture(tmp_path, monkeypatch)
    store, _ = _evidence_store()
    plan_receipt = store.publish_plan(
        create_experiment_plan(
            hypothesis_id="issue-466-h1",
            control_id="control-revision",
            candidate_ids=("candidate-revision",),
            created_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
        )
    )
    baseline_run, _ = _log_verified_run_with_promotion_evidence(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        plan_receipt=plan_receipt,
        store=store,
    )
    challenger_run, _ = _log_verified_run_with_promotion_evidence(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        plan_receipt=plan_receipt,
        store=store,
        feature_columns=["views", "new_feature"],
    )

    result = verify_training_comparison(
        baseline_run,
        challenger_run,
        tmp_path / "comparison.json",
        promotion_evidence_store=store,
    )

    assert result.promotion_evidence is not None
    assert result.promotion_evidence.plan_receipt == plan_receipt
    assert result.promotion_evidence.baseline_metric.evidence.run_id == baseline_run
    assert result.promotion_evidence.challenger_metric.evidence.run_id == challenger_run


def _plan_receipt(store: PromotionEvidenceStore, *, candidate_id: str) -> ExperimentPlanReceipt:
    return store.publish_plan(
        create_experiment_plan(
            hypothesis_id="issue-466-h1",
            control_id="control-revision",
            candidate_ids=(candidate_id,),
            created_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
        )
    )


def _assert_no_comparison_publication(
    output: Path, *, challenger_run: str
) -> None:
    assert not output.exists()
    assert list(
        MlflowClient().list_artifacts(challenger_run, "reproducibility/comparisons")
    ) == []


def _overwrite_metric_receipt_artifact(
    tmp_path: Path, *, run_id: str, receipt: HeldOutMetricReceipt
) -> None:
    receipt_path = tmp_path / "held_out_metric_receipt.json"
    write_manifest_atomic(receipt, receipt_path)
    MlflowClient().log_artifact(
        run_id,
        str(receipt_path),
        artifact_path="reproducibility/metrics",
    )


def test_comparison_rejects_gcs_metric_body_changed_after_receipt_creation(
    tmp_path, monkeypatch
) -> None:
    dataset_path, snapshot_path, experiment_id = _comparison_fixture(tmp_path, monkeypatch)
    store, bucket = _evidence_store()
    plan_receipt = _plan_receipt(store, candidate_id="candidate-revision")
    baseline_run, baseline_metric = _log_verified_run_with_promotion_evidence(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        plan_receipt=plan_receipt,
        store=store,
    )
    challenger_run, _ = _log_verified_run_with_promotion_evidence(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        plan_receipt=plan_receipt,
        store=store,
    )
    bucket.replace_payload(
        baseline_metric.object.uri.removeprefix("gs://evidence/"), b'{"tampered":true}'
    )
    output = tmp_path / "comparison.json"

    with pytest.raises(ComparisonValidationError, match="GCS receipt"):
        verify_training_comparison(
            baseline_run, challenger_run, output, promotion_evidence_store=store
        )

    _assert_no_comparison_publication(output, challenger_run=challenger_run)


def test_comparison_rejects_metric_receipt_generation_change(
    tmp_path, monkeypatch
) -> None:
    dataset_path, snapshot_path, experiment_id = _comparison_fixture(tmp_path, monkeypatch)
    store, _ = _evidence_store()
    plan_receipt = _plan_receipt(store, candidate_id="candidate-revision")
    baseline_run, baseline_metric = _log_verified_run_with_promotion_evidence(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        plan_receipt=plan_receipt,
        store=store,
    )
    challenger_run, _ = _log_verified_run_with_promotion_evidence(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        plan_receipt=plan_receipt,
        store=store,
    )
    _overwrite_metric_receipt_artifact(
        tmp_path,
        run_id=baseline_run,
        receipt=baseline_metric.model_copy(
            update={"object": baseline_metric.object.model_copy(update={"generation": "2"})}
        ),
    )
    output = tmp_path / "comparison.json"

    with pytest.raises(ComparisonValidationError, match="GCS receipt"):
        verify_training_comparison(
            baseline_run, challenger_run, output, promotion_evidence_store=store
        )

    _assert_no_comparison_publication(output, challenger_run=challenger_run)


def test_comparison_rejects_mismatched_plan_receipts(tmp_path, monkeypatch) -> None:
    dataset_path, snapshot_path, experiment_id = _comparison_fixture(tmp_path, monkeypatch)
    store, _ = _evidence_store()
    baseline_plan = _plan_receipt(store, candidate_id="candidate-baseline")
    challenger_plan = _plan_receipt(store, candidate_id="candidate-challenger")
    baseline_run, _ = _log_verified_run_with_promotion_evidence(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        plan_receipt=baseline_plan,
        store=store,
    )
    challenger_run, _ = _log_verified_run_with_promotion_evidence(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        plan_receipt=challenger_plan,
        store=store,
    )
    output = tmp_path / "comparison.json"

    with pytest.raises(ComparisonValidationError, match="plan receipt"):
        verify_training_comparison(
            baseline_run, challenger_run, output, promotion_evidence_store=store
        )

    _assert_no_comparison_publication(output, challenger_run=challenger_run)


def test_comparison_rejects_partial_promotion_evidence(tmp_path, monkeypatch) -> None:
    dataset_path, snapshot_path, experiment_id = _comparison_fixture(tmp_path, monkeypatch)
    store, _ = _evidence_store()
    plan_receipt = _plan_receipt(store, candidate_id="candidate-revision")
    baseline_run, _ = _log_verified_run_with_promotion_evidence(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        plan_receipt=plan_receipt,
        store=store,
    )
    challenger_run = _log_verified_run(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
    )
    output = tmp_path / "comparison.json"

    with pytest.raises(ComparisonValidationError, match="partial promotion evidence"):
        verify_training_comparison(
            baseline_run, challenger_run, output, promotion_evidence_store=store
        )

    _assert_no_comparison_publication(output, challenger_run=challenger_run)


def test_comparison_rejects_plan_created_after_mlflow_run_start(tmp_path, monkeypatch) -> None:
    dataset_path, snapshot_path, experiment_id = _comparison_fixture(tmp_path, monkeypatch)
    store, bucket = _evidence_store()
    bucket.next_plan_time_created = datetime.now(timezone.utc) + timedelta(days=1)
    plan_receipt = _plan_receipt(store, candidate_id="candidate-revision")
    baseline_run, _ = _log_verified_run_with_promotion_evidence(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        plan_receipt=plan_receipt,
        store=store,
    )
    challenger_run, _ = _log_verified_run_with_promotion_evidence(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        plan_receipt=plan_receipt,
        store=store,
    )
    output = tmp_path / "comparison.json"

    with pytest.raises(ComparisonValidationError, match="시작 뒤"):
        verify_training_comparison(
            baseline_run, challenger_run, output, promotion_evidence_store=store
        )

    _assert_no_comparison_publication(output, challenger_run=challenger_run)


def test_comparison_rejects_metric_created_outside_its_run_time_range(
    tmp_path, monkeypatch
) -> None:
    dataset_path, snapshot_path, experiment_id = _comparison_fixture(tmp_path, monkeypatch)
    store, bucket = _evidence_store()
    plan_receipt = _plan_receipt(store, candidate_id="candidate-revision")
    bucket.next_metric_time_created = datetime.now(timezone.utc) + timedelta(days=1)
    baseline_run, _ = _log_verified_run_with_promotion_evidence(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        plan_receipt=plan_receipt,
        store=store,
    )
    challenger_run, _ = _log_verified_run_with_promotion_evidence(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        plan_receipt=plan_receipt,
        store=store,
    )
    output = tmp_path / "comparison.json"

    with pytest.raises(ComparisonValidationError, match="범위 밖"):
        verify_training_comparison(
            baseline_run, challenger_run, output, promotion_evidence_store=store
        )

    _assert_no_comparison_publication(output, challenger_run=challenger_run)


@pytest.mark.parametrize(
    ("metric_overrides", "error_fragment"),
    [
        ({"run_id": "another-run"}, "metric run_id"),
        ({"split_manifest_sha256": "b" * 64}, "metric split manifest"),
        ({"test_membership_sha256": "b" * 64}, "metric test membership"),
        ({"model_artifact_sha256": "c" * 64}, "metric model artifact"),
    ],
)
def test_comparison_rejects_metric_binding_mismatch(
    tmp_path, monkeypatch, metric_overrides: dict[str, object], error_fragment: str
) -> None:
    dataset_path, snapshot_path, experiment_id = _comparison_fixture(tmp_path, monkeypatch)
    store, _ = _evidence_store()
    plan_receipt = _plan_receipt(store, candidate_id="candidate-revision")
    baseline_run, _ = _log_verified_run_with_promotion_evidence(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        plan_receipt=plan_receipt,
        store=store,
        metric_overrides=metric_overrides,
    )
    challenger_run, _ = _log_verified_run_with_promotion_evidence(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        plan_receipt=plan_receipt,
        store=store,
    )
    output = tmp_path / "comparison.json"

    with pytest.raises(ComparisonValidationError, match=error_fragment):
        verify_training_comparison(
            baseline_run, challenger_run, output, promotion_evidence_store=store
        )

    _assert_no_comparison_publication(output, challenger_run=challenger_run)


def test_seed_mismatch_has_no_output_or_challenger_upload(tmp_path, monkeypatch) -> None:
    dataset_path, snapshot_path, experiment_id = _comparison_fixture(tmp_path, monkeypatch)
    baseline_run = _log_verified_run(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        split_seed=42,
    )
    challenger_run = _log_verified_run(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        split_seed=43,
    )
    output = tmp_path / "comparison.json"

    with pytest.raises(ComparisonValidationError, match="split_seed"):
        verify_training_comparison(baseline_run, challenger_run, output)

    assert not output.exists()
    client = MlflowClient(tracking_uri=(tmp_path / "mlruns").as_uri())
    assert list(client.list_artifacts(challenger_run, "reproducibility/comparisons")) == []


def test_missing_artifact_has_no_output(tmp_path, monkeypatch) -> None:
    dataset_path, snapshot_path, experiment_id = _comparison_fixture(tmp_path, monkeypatch)
    baseline_run = _log_verified_run(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        include_split=False,
    )
    challenger_run = _log_verified_run(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
    )
    output = tmp_path / "comparison.json"

    with pytest.raises(ComparisonValidationError, match="split"):
        verify_training_comparison(baseline_run, challenger_run, output)

    assert not output.exists()


def test_tampered_csv_has_no_output(tmp_path, monkeypatch) -> None:
    dataset_path, snapshot_path, experiment_id = _comparison_fixture(tmp_path, monkeypatch)
    baseline_run = _log_verified_run(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        tamper_dataset=True,
    )
    challenger_run = _log_verified_run(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
    )
    output = tmp_path / "comparison.json"

    with pytest.raises(ComparisonValidationError, match="dataset_sha256"):
        verify_training_comparison(baseline_run, challenger_run, output)

    assert not output.exists()


def test_split_membership_mismatch_has_no_output(tmp_path, monkeypatch) -> None:
    dataset_path, snapshot_path, experiment_id = _comparison_fixture(tmp_path, monkeypatch)
    baseline_run = _log_verified_run(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
    )
    challenger_run = _log_verified_run(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        split_positions={"train": [0, 2], "validation": [1], "test": [3]},
    )
    output = tmp_path / "comparison.json"

    with pytest.raises(ComparisonValidationError, match="train membership"):
        verify_training_comparison(baseline_run, challenger_run, output)

    assert not output.exists()


@pytest.mark.parametrize(
    ("seed_name", "seed_kwargs"),
    [
        ("model_seed", {"model_seed": 43}),
        ("sampler_seed", {"sampler_seed": 43}),
    ],
)
def test_model_or_sampler_seed_mismatch_has_no_output(
    tmp_path, monkeypatch, seed_name, seed_kwargs
) -> None:
    dataset_path, snapshot_path, experiment_id = _comparison_fixture(tmp_path, monkeypatch)
    baseline_run = _log_verified_run(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
    )
    challenger_run = _log_verified_run(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        **seed_kwargs,
    )
    output = tmp_path / "comparison.json"

    with pytest.raises(ComparisonValidationError, match=seed_name):
        verify_training_comparison(baseline_run, challenger_run, output)

    assert not output.exists()


def test_manifest_byte_hash_mismatch_has_no_output(tmp_path, monkeypatch) -> None:
    dataset_path, snapshot_path, experiment_id = _comparison_fixture(tmp_path, monkeypatch)
    baseline_run = _log_verified_run(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
        tamper_snapshot=True,
    )
    challenger_run = _log_verified_run(
        tmp_path,
        experiment_id=experiment_id,
        dataset_path=dataset_path,
        snapshot_path=snapshot_path,
    )
    output = tmp_path / "comparison.json"

    with pytest.raises(ComparisonValidationError, match="snapshot_manifest_sha256"):
        verify_training_comparison(baseline_run, challenger_run, output)

    assert not output.exists()
