"""CTR model deployment manifest contract tests."""

import json
import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.tracking.model_package import (
    ArtifactDigest,
    ModelPackageArtifacts,
    ModelPackageManifest,
    OnnxArtifactDigest,
    hash_directory,
    load_manifest,
    verify_model_package,
)

SHA = "a" * 64


def _manifest(*, sampling_rate: float = 1.0, calibration=None):
    return ModelPackageManifest(
        contract_version="ctr-model-package-v1",
        feature_service="ctr_training_v1",
        sampling_rate=sampling_rate,
        artifacts=ModelPackageArtifacts(
            model_onnx=OnnxArtifactDigest(
                path="model_onnx", entrypoint="model.onnx", sha256=SHA
            ),
            feature_columns=ArtifactDigest(
                path="features/feature_columns.json", sha256=SHA
            ),
            categorical_columns=ArtifactDigest(
                path="features/categorical_columns.json", sha256=SHA
            ),
            calibration=calibration,
        ),
    )


@pytest.mark.parametrize("value", [0.0, -0.1, 1.1, math.nan, math.inf])
def test_manifest_rejects_invalid_sampling_rate(value: float) -> None:
    with pytest.raises(ValidationError):
        _manifest(sampling_rate=value)


def test_manifest_uses_exact_sampling_rate_branch() -> None:
    calibration = ArtifactDigest(path="calibration/calibration.json", sha256=SHA)
    with pytest.raises(ValidationError):
        _manifest(sampling_rate=0.9999999999999999)
    with pytest.raises(ValidationError):
        _manifest(sampling_rate=1.0, calibration=calibration)
    assert _manifest(sampling_rate=0.5, calibration=calibration).sampling_rate == 0.5


@pytest.mark.parametrize("digest", ["A" * 64, "a" * 63, "g" * 64])
def test_manifest_rejects_noncanonical_sha256(digest: str) -> None:
    with pytest.raises(ValidationError):
        ArtifactDigest(path="features/feature_columns.json", sha256=digest)


@pytest.mark.parametrize(
    "path", ["../model.onnx", "/model.onnx", "C:/model.onnx", "a\\b", "https://x/y"]
)
def test_manifest_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        ArtifactDigest(path=path, sha256=SHA)


def test_manifest_forbids_unknown_fields(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    payload = _manifest().model_dump(mode="json")
    payload["unexpected"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError):
        load_manifest(path)


def test_directory_hash_includes_names_and_unlisted_files(tmp_path: Path) -> None:
    root = tmp_path / "model"
    root.mkdir()
    (root / "a").write_bytes(b"bc")
    first = hash_directory(root)
    (root / "a").rename(root / "ab")
    renamed = hash_directory(root)
    assert renamed != first
    (root / "extra").write_bytes(b"")
    assert hash_directory(root) != renamed


def test_verify_rejects_calibration_value_mismatch(tmp_path: Path) -> None:
    model_dir = tmp_path / "model_onnx"
    model_dir.mkdir()
    (model_dir / "model.onnx").write_bytes(b"onnx")
    features = tmp_path / "feature_columns.json"
    features.write_text("[]", encoding="utf-8")
    categories = tmp_path / "categorical_columns.json"
    categories.write_text("{}", encoding="utf-8")
    calibration = tmp_path / "calibration.json"
    calibration.write_text('{"sampling_rate":0.2}', encoding="utf-8")
    manifest = ModelPackageManifest.build(
        sampling_rate=0.1,
        model_onnx=model_dir,
        feature_columns=features,
        categorical_columns=categories,
        calibration=calibration,
    )
    with pytest.raises(ValueError, match="sampling_rate"):
        verify_model_package(
            manifest,
            model_onnx=model_dir,
            feature_columns=features,
            categorical_columns=categories,
            calibration=calibration,
        )
