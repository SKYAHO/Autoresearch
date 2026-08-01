"""학습 snapshot/split provenance 계약의 순수 단위 테스트."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from src.pipeline.training_provenance import (
    RegistryProvenance,
    TrainingSeeds,
    build_snapshot_manifest,
    build_split_manifest,
    load_training_snapshot_manifest,
    membership_sha256,
    resolve_training_seeds,
    sha256_file,
    snapshot_manifest_path,
    write_manifest_atomic,
)
from src.pipeline.training_provenance import ProvenanceValidationError


def _registry() -> RegistryProvenance:
    return RegistryProvenance(
        uri="gs://bucket/registry.db",
        generation="7",
        sha256="a" * 64,
    )


def _dataset(path: Path) -> None:
    pd.DataFrame(
        {
            "views": pd.Series([1, 2], dtype="int64"),
            "clicked": pd.Series([0, 1], dtype="int64"),
        }
    ).to_csv(path, index=False)


def test_snapshot_manifest_round_trip_validates_csv(tmp_path: Path) -> None:
    dataset_path = tmp_path / "training_dataset.csv"
    _dataset(dataset_path)
    manifest = build_snapshot_manifest(
        dataset_path=dataset_path,
        events_start_date=date(2026, 7, 1),
        events_end_date=date(2026, 7, 30),
        feature_service="ctr_training_v1",
        registry=_registry(),
        code_archive_sha=None,
    )

    write_manifest_atomic(manifest, snapshot_manifest_path(dataset_path))

    loaded = load_training_snapshot_manifest(dataset_path)
    assert loaded == manifest
    assert loaded.row_count == 2
    assert [column.name for column in loaded.columns] == ["views", "clicked"]


def test_snapshot_manifest_rejects_tampered_csv(tmp_path: Path) -> None:
    dataset_path = tmp_path / "training_dataset.csv"
    _dataset(dataset_path)
    manifest = build_snapshot_manifest(
        dataset_path=dataset_path,
        events_start_date=date(2026, 7, 1),
        events_end_date=date(2026, 7, 30),
        feature_service="ctr_training_v1",
        registry=_registry(),
        code_archive_sha=None,
    )
    write_manifest_atomic(manifest, snapshot_manifest_path(dataset_path))

    dataset_path.write_text("views,clicked\n999,0\n", encoding="utf-8")

    with pytest.raises(ProvenanceValidationError, match="dataset_sha256"):
        load_training_snapshot_manifest(dataset_path)


def test_snapshot_manifest_rejects_schema_change(tmp_path: Path) -> None:
    dataset_path = tmp_path / "training_dataset.csv"
    _dataset(dataset_path)
    manifest = build_snapshot_manifest(
        dataset_path=dataset_path,
        events_start_date=date(2026, 7, 1),
        events_end_date=date(2026, 7, 30),
        feature_service="ctr_training_v1",
        registry=_registry(),
        code_archive_sha=None,
    )
    write_manifest_atomic(manifest, snapshot_manifest_path(dataset_path))

    pd.DataFrame(
        {
            "views": pd.Series([1.5, 2.5], dtype="float64"),
            "clicked": pd.Series([0, 1], dtype="int64"),
        }
    ).to_csv(dataset_path, index=False)
    write_manifest_atomic(
        manifest.model_copy(update={"dataset_sha256": sha256_file(dataset_path)}),
        snapshot_manifest_path(dataset_path),
    )

    with pytest.raises(ProvenanceValidationError, match="schema_sha256"):
        load_training_snapshot_manifest(dataset_path)


def test_snapshot_manifest_rejects_malformed_json(tmp_path: Path) -> None:
    dataset_path = tmp_path / "training_dataset.csv"
    _dataset(dataset_path)
    snapshot_manifest_path(dataset_path).write_text("{}", encoding="utf-8")

    with pytest.raises(ProvenanceValidationError, match="manifest"):
        load_training_snapshot_manifest(dataset_path)


def test_resolve_training_seeds_uses_config_for_legacy_default() -> None:
    assert resolve_training_seeds(
        random_state=None,
        split_seed=None,
        model_seed=None,
        sampler_seed=None,
        config_seed=42,
    ) == TrainingSeeds(split_seed=42, model_seed=42, sampler_seed=42)


def test_resolve_training_seeds_uses_random_state_for_legacy_override() -> None:
    assert resolve_training_seeds(
        random_state=17,
        split_seed=None,
        model_seed=None,
        sampler_seed=None,
        config_seed=42,
    ) == TrainingSeeds(split_seed=17, model_seed=17, sampler_seed=17)


def test_resolve_training_seeds_requires_complete_explicit_triplet() -> None:
    with pytest.raises(ValueError, match="모두 지정"):
        resolve_training_seeds(
            random_state=None,
            split_seed=1,
            model_seed=None,
            sampler_seed=3,
            config_seed=42,
        )


def test_resolve_training_seeds_rejects_ambiguous_random_state() -> None:
    with pytest.raises(ValueError, match="random_state"):
        resolve_training_seeds(
            random_state=42,
            split_seed=1,
            model_seed=2,
            sampler_seed=3,
            config_seed=99,
        )


def test_membership_hash_is_deterministic_and_position_specific() -> None:
    assert membership_sha256([3, 1, 2]) == membership_sha256([1, 2, 3])
    assert membership_sha256([1, 2, 3]) != membership_sha256([1, 2, 4])


def test_split_manifest_records_membership_and_feature_hashes(tmp_path: Path) -> None:
    dataset_path = tmp_path / "training_dataset.csv"
    _dataset(dataset_path)
    snapshot = build_snapshot_manifest(
        dataset_path=dataset_path,
        events_start_date=date(2026, 7, 1),
        events_end_date=date(2026, 7, 30),
        feature_service="ctr_training_v1",
        registry=_registry(),
        code_archive_sha=None,
    )
    split = build_split_manifest(
        run_id="run-1",
        snapshot=snapshot,
        snapshot_manifest_sha256="b" * 64,
        seeds=TrainingSeeds(split_seed=11, model_seed=12, sampler_seed=13),
        test_size=0.2,
        val_size=0.2,
        split_positions={"train": [0], "validation": [1], "test": [2]},
        feature_columns=["views"],
    )

    assert split.run_id == "run-1"
    assert split.splits["train"].membership_sha256 == membership_sha256([0])
    assert split.feature_columns == ["views"]
