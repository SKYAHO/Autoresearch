"""CTR 모델 배포 패키지 manifest와 무결성 검증.

[파이프라인] 학습이 만든 ONNX·피처 JSON·calibration을 MLflow run에 기록하기 직전과,
서빙이 같은 run의 아티팩트를 로드하는 직후 사이의 패키지 계약을 담당한다.

[기능] 엄격한 pydantic manifest 스키마, 플랫폼 독립적인 canonical SHA-256, 패키지
내용 및 calibration 정합성 검증을 제공한다.

[비책임] ONNX 변환·학습은 src.pipeline.train, ONNX 세션 생성·추론은
src.serving.model_loader와 src.serving.onnx_model이 담당한다.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import struct
from pathlib import Path, PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SHA256_PATTERN = r"^[0-9a-f]{64}$"


def _reject_reparse_point(path: Path) -> None:
    """재귀 진입·파일 open 전에 링크와 Windows reparse point를 거부한다."""
    info = path.stat(follow_symlinks=False)
    attributes = getattr(info, "st_file_attributes", 0)
    if path.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise ValueError(f"링크 또는 reparse point는 허용하지 않습니다: {path}")


def _validate_relative_path(value: str) -> str:
    if not value or "\\" in value or "://" in value:
        raise ValueError("POSIX 상대 경로여야 합니다")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError("정규화된 상대 경로여야 합니다")
    if path.parts and path.parts[0].endswith(":"):
        raise ValueError("drive 경로는 허용하지 않습니다")
    return value


class ArtifactDigest(BaseModel):
    """고정 경로 파일 아티팩트의 digest."""

    model_config = ConfigDict(extra="forbid")
    path: str
    sha256: str = Field(pattern=SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        _validate_relative_path(self.path)
        return self


class OnnxArtifactDigest(ArtifactDigest):
    """MLflow ONNX 디렉터리와 고정 엔트리 파일 digest."""

    entrypoint: str

    @model_validator(mode="after")
    def validate_entrypoint(self) -> Self:
        _validate_relative_path(self.entrypoint)
        return self


class ModelPackageArtifacts(BaseModel):
    """허용된 CTR 모델 패키지 아티팩트 집합."""

    model_config = ConfigDict(extra="forbid")
    model_onnx: OnnxArtifactDigest
    feature_columns: ArtifactDigest
    categorical_columns: ArtifactDigest
    calibration: ArtifactDigest | None


class ModelPackageManifest(BaseModel):
    """ctr-model-package-v1 배포 계약."""

    model_config = ConfigDict(extra="forbid")
    contract_version: Literal["ctr-model-package-v1"]
    feature_service: Literal["ctr_training_v1"]
    sampling_rate: float
    artifacts: ModelPackageArtifacts

    @model_validator(mode="after")
    def validate_contract(self) -> Self:
        rate = self.sampling_rate
        if not math.isfinite(rate) or not (0.0 < rate <= 1.0):
            raise ValueError("sampling_rate는 유한한 (0, 1] 값이어야 합니다")
        expected = {
            "model_onnx": "model_onnx",
            "feature_columns": "features/feature_columns.json",
            "categorical_columns": "features/categorical_columns.json",
        }
        if self.artifacts.model_onnx.path != expected["model_onnx"]:
            raise ValueError("model_onnx path가 고정 계약과 다릅니다")
        if self.artifacts.model_onnx.entrypoint != "model.onnx":
            raise ValueError("ONNX entrypoint가 model.onnx가 아닙니다")
        if self.artifacts.feature_columns.path != expected["feature_columns"]:
            raise ValueError("feature_columns path가 고정 계약과 다릅니다")
        if self.artifacts.categorical_columns.path != expected["categorical_columns"]:
            raise ValueError("categorical_columns path가 고정 계약과 다릅니다")
        if rate < 1.0:
            calibration = self.artifacts.calibration
            if calibration is None or calibration.path != "calibration/calibration.json":
                raise ValueError("downsampling 패키지에는 calibration이 필요합니다")
        else:
            if self.artifacts.calibration is not None:
                raise ValueError("sampling_rate == 1.0이면 calibration은 null이어야 합니다")
        return self

    @classmethod
    def build(
        cls,
        *,
        sampling_rate: float,
        model_onnx: Path,
        feature_columns: Path,
        categorical_columns: Path,
        calibration: Path | None,
    ) -> "ModelPackageManifest":
        """로컬 staging 아티팩트로 manifest를 생성한다."""
        calibration_digest = (
            ArtifactDigest(
                path="calibration/calibration.json", sha256=hash_file(calibration)
            )
            if calibration is not None
            else None
        )
        return cls(
            contract_version="ctr-model-package-v1",
            feature_service="ctr_training_v1",
            sampling_rate=sampling_rate,
            artifacts=ModelPackageArtifacts(
                model_onnx=OnnxArtifactDigest(
                    path="model_onnx",
                    entrypoint="model.onnx",
                    sha256=hash_directory(model_onnx),
                ),
                feature_columns=ArtifactDigest(
                    path="features/feature_columns.json",
                    sha256=hash_file(feature_columns),
                ),
                categorical_columns=ArtifactDigest(
                    path="features/categorical_columns.json",
                    sha256=hash_file(categorical_columns),
                ),
                calibration=calibration_digest,
            ),
        )


def hash_file(path: Path) -> str:
    """일반 파일 바이트의 SHA-256을 반환한다."""
    _reject_reparse_point(path)
    if not path.is_file():
        raise ValueError(f"일반 파일이 아닙니다: {path}")
    digest = hashlib.sha256()
    _reject_reparse_point(path)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_directory(root: Path) -> str:
    """길이-prefix canonical 알고리즘으로 디렉터리 SHA-256을 반환한다."""
    _reject_reparse_point(root)
    if not root.is_dir():
        raise ValueError(f"디렉터리가 아닙니다: {root}")
    files: list[tuple[bytes, Path]] = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        _reject_reparse_point(current_path)
        for name in directories:
            _reject_reparse_point(current_path / name)
        for name in filenames:
            path = current_path / name
            _reject_reparse_point(path)
            if not path.is_file():
                raise ValueError(f"일반 파일이 아닙니다: {path}")
            relative = path.relative_to(root).as_posix().encode("utf-8")
            files.append((relative, path))
    if not files:
        raise ValueError("빈 디렉터리는 해시할 수 없습니다")
    digest = hashlib.sha256()
    for relative, path in sorted(files, key=lambda item: item[0]):
        _reject_reparse_point(path)
        size = path.stat().st_size
        digest.update(struct.pack(">Q", len(relative)))
        digest.update(relative)
        digest.update(struct.pack(">Q", size))
        _reject_reparse_point(path)
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def save_manifest(manifest: ModelPackageManifest, path: Path) -> None:
    """manifest를 UTF-8 JSON으로 저장한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")


def load_manifest(path: Path) -> ModelPackageManifest:
    """manifest JSON을 엄격히 검증해 읽는다."""
    return ModelPackageManifest.model_validate_json(path.read_text(encoding="utf-8"))


def verify_model_package(
    manifest: ModelPackageManifest,
    *,
    model_onnx: Path,
    feature_columns: Path,
    categorical_columns: Path,
    calibration: Path | None,
) -> None:
    """manifest와 로컬 아티팩트의 hash·calibration 계약을 검증한다."""
    expected = manifest.artifacts
    if hash_directory(model_onnx) != expected.model_onnx.sha256:
        raise ValueError("model_onnx SHA-256 불일치")
    if hash_file(feature_columns) != expected.feature_columns.sha256:
        raise ValueError("feature_columns SHA-256 불일치")
    if hash_file(categorical_columns) != expected.categorical_columns.sha256:
        raise ValueError("categorical_columns SHA-256 불일치")
    rate = manifest.sampling_rate
    if rate < 1.0:
        if calibration is None or expected.calibration is None:
            raise ValueError("calibration 아티팩트가 없습니다")
        if hash_file(calibration) != expected.calibration.sha256:
            raise ValueError("calibration SHA-256 불일치")
        payload = json.loads(calibration.read_text(encoding="utf-8"))
        if set(payload) != {"sampling_rate"} or payload["sampling_rate"] != rate:
            raise ValueError("calibration sampling_rate가 manifest와 다릅니다")
    else:
        if calibration is not None or expected.calibration is not None:
            raise ValueError("sampling_rate == 1.0 패키지에는 calibration이 없어야 합니다")
