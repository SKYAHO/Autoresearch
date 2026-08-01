from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import mlflow
import pandas as pd
import pytest
from mlflow.tracking import MlflowClient

from src.pipeline.training_comparison import (
    ComparisonValidationError,
    verify_training_comparison,
)
from src.pipeline.training_provenance import (
    RegistryProvenance,
    TrainingSeeds,
    build_snapshot_manifest,
    build_split_manifest,
    sha256_file,
    write_manifest_atomic,
)


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
