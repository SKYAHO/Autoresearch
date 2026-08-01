"""검증된 MLflow 학습 run의 공정 비교를 수행한다.

[파이프라인] 두 MLflow run에서 canonical snapshot·split artifact를 내려받아
무결성을 재검증하고, snapshot·split membership·effective seed가 같은지 확인한 뒤
사전 선언한 experiment plan에 바인딩된 comparison manifest를 로컬 파일과 challenger
run에 게시한다.

[기능] MLflow artifact 다운로드, Task 1 provenance 계약 재검증, 공정 비교 equality
검사, deterministic comparison ID 생성, challenger artifact 원자 게시를 제공한다.

[비책임] 모델 학습·평가·통계적 유의성(#407)·champion 승격·Airflow 재시도와
인프라 저장소 수명주기는 이 모듈이 다루지 않는다. 이 모듈은 이미 종료된 run의
provenance artifact를 비교하는 application 경계만 소유한다.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import mlflow
import mlflow.artifacts
from mlflow.tracking import MlflowClient
from pydantic import ValidationError

from src.pipeline.training_provenance import (
    TrainingComparisonManifest,
    TrainingSeeds,
    TrainingSnapshotManifest,
    TrainingSplitManifest,
    feature_columns_sha256,
    load_training_snapshot_manifest,
    sha256_file,
    write_manifest_atomic,
)


class ComparisonValidationError(ValueError):
    """공정 비교에 필요한 artifact 또는 equality 계약이 성립하지 않을 때 발생한다."""


_SNAPSHOT_DATASET_ARTIFACT = "reproducibility/snapshot/training_dataset.csv"
_SNAPSHOT_MANIFEST_ARTIFACT = "reproducibility/snapshot/snapshot_manifest.json"
_SPLIT_MANIFEST_ARTIFACT = "reproducibility/split/split_manifest.json"
_EXPECTED_SPLIT_NAMES = {"train", "validation", "test"}


@dataclass(frozen=True)
class _VerifiedRun:
    """artifact 재검증을 통과한 하나의 MLflow run provenance."""

    run_id: str
    snapshot: TrainingSnapshotManifest
    split: TrainingSplitManifest
    snapshot_manifest_sha256: str
    split_manifest_sha256: str


def _download_artifact(
    *,
    run_id: str,
    artifact_path: str,
    destination: Path,
) -> Path:
    """canonical run artifact를 임시 디렉터리에 내려받고 파일인지 확인한다."""
    destination.mkdir(parents=True, exist_ok=True)
    try:
        downloaded_path = Path(
            mlflow.artifacts.download_artifacts(
                artifact_uri=f"runs:/{run_id}/{artifact_path}",
                dst_path=str(destination),
            )
        )
    except Exception as error:
        # MLflow backend 예외에는 credential·signed URL이 포함될 수 있으므로 원문을
        # comparison 오류에 복사하지 않는다.
        raise ComparisonValidationError(
            f"{artifact_path} artifact를 run {run_id}에서 내려받을 수 없습니다"
        ) from error

    if downloaded_path.is_dir():
        candidate = downloaded_path / Path(artifact_path).name
        if candidate.is_file():
            downloaded_path = candidate
    if not downloaded_path.is_file():
        raise ComparisonValidationError(
            f"{artifact_path} artifact가 run {run_id}에 없습니다"
        )
    return downloaded_path


def _safe_snapshot_error(run_id: str, error: Exception) -> ComparisonValidationError:
    """snapshot 검증 오류에서 안전한 field label만 보존한다."""
    message = str(error)
    labels = (
        "dataset_sha256",
        "schema_sha256",
        "row_count",
        "snapshot manifest",
        "snapshot dataset",
    )
    label = next((candidate for candidate in labels if candidate in message), "snapshot")
    return ComparisonValidationError(f"{label} validation failed for run {run_id}")


def _load_split_manifest(
    *,
    run_id: str,
    split_path: Path,
    snapshot: TrainingSnapshotManifest,
    snapshot_manifest_sha256: str,
) -> TrainingSplitManifest:
    """split manifest JSON과 snapshot/run 연결 및 내부 hash를 검증한다."""
    try:
        split = TrainingSplitManifest.model_validate_json(
            split_path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError) as error:
        raise ComparisonValidationError(
            f"split manifest가 run {run_id}에서 유효하지 않습니다"
        ) from error

    if split.run_id != run_id:
        raise ComparisonValidationError(f"split run_id mismatch for run {run_id}")
    if split.snapshot_sha256 != snapshot.dataset_sha256:
        raise ComparisonValidationError(f"split snapshot_sha256 mismatch for run {run_id}")
    if split.snapshot_manifest_sha256 != snapshot_manifest_sha256:
        raise ComparisonValidationError(
            f"snapshot_manifest_sha256 mismatch for run {run_id}"
        )
    if set(split.splits) != _EXPECTED_SPLIT_NAMES:
        raise ComparisonValidationError(f"split names mismatch for run {run_id}")
    if sum(member.row_count for member in split.splits.values()) != snapshot.row_count:
        raise ComparisonValidationError(f"split row_count mismatch for run {run_id}")
    if feature_columns_sha256(split.feature_columns) != split.feature_columns_sha256:
        raise ComparisonValidationError(
            f"feature_columns_sha256 mismatch for run {run_id}"
        )
    return split


def _load_verified_run(run_id: str, workspace: Path) -> _VerifiedRun:
    """하나의 run에서 세 canonical artifact를 내려받아 검증한다."""
    run_workspace = workspace / run_id
    snapshot_workspace = run_workspace / "snapshot"
    split_workspace = run_workspace / "split"
    dataset_artifact = _download_artifact(
        run_id=run_id,
        artifact_path=_SNAPSHOT_DATASET_ARTIFACT,
        destination=snapshot_workspace,
    )
    snapshot_artifact = _download_artifact(
        run_id=run_id,
        artifact_path=_SNAPSHOT_MANIFEST_ARTIFACT,
        destination=snapshot_workspace,
    )
    split_artifact = _download_artifact(
        run_id=run_id,
        artifact_path=_SPLIT_MANIFEST_ARTIFACT,
        destination=split_workspace,
    )

    staged_dataset = run_workspace / "training_dataset.csv"
    staged_snapshot = Path(f"{staged_dataset}.snapshot.json")
    try:
        shutil.copyfile(dataset_artifact, staged_dataset)
        shutil.copyfile(snapshot_artifact, staged_snapshot)
    except OSError as error:
        raise ComparisonValidationError(
            f"snapshot artifact staging failed for run {run_id}"
        ) from error
    snapshot_manifest_sha256 = sha256_file(snapshot_artifact)
    split_manifest_sha256 = sha256_file(split_artifact)

    try:
        snapshot = load_training_snapshot_manifest(staged_dataset)
    except Exception as error:
        if isinstance(error, ComparisonValidationError):
            raise
        raise _safe_snapshot_error(run_id, error) from error

    split = _load_split_manifest(
        run_id=run_id,
        split_path=split_artifact,
        snapshot=snapshot,
        snapshot_manifest_sha256=snapshot_manifest_sha256,
    )
    return _VerifiedRun(
        run_id=run_id,
        snapshot=snapshot,
        split=split,
        snapshot_manifest_sha256=snapshot_manifest_sha256,
        split_manifest_sha256=split_manifest_sha256,
    )


def _assert_equal(label: str, baseline: object, challenger: object) -> None:
    """공정 비교 equality field가 다르면 안전한 오류를 발생시킨다."""
    if baseline != challenger:
        raise ComparisonValidationError(
            f"{label} differs between baseline and challenger"
        )


def _comparison_id(
    baseline: _VerifiedRun,
    challenger: _VerifiedRun,
    *,
    experiment_plan_id: str | None,
) -> str:
    """run pair·공통 snapshot·plan binding으로 deterministic ID를 만든다."""
    identity_parts = [
        baseline.run_id,
        challenger.run_id,
        baseline.snapshot.dataset_sha256,
        challenger.split_manifest_sha256,
    ]
    if experiment_plan_id is not None:
        identity_parts.append(experiment_plan_id)
    payload = "\0".join(identity_parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _build_comparison_manifest(
    baseline: _VerifiedRun,
    challenger: _VerifiedRun,
    *,
    experiment_plan_id: str | None,
) -> TrainingComparisonManifest:
    """equality 검증을 모두 통과한 두 run의 comparison manifest를 만든다."""
    _assert_equal(
        "snapshot_sha256",
        baseline.snapshot.dataset_sha256,
        challenger.snapshot.dataset_sha256,
    )
    _assert_equal(
        "snapshot_manifest_sha256",
        baseline.snapshot_manifest_sha256,
        challenger.snapshot_manifest_sha256,
    )
    for split_name in ("train", "validation", "test"):
        _assert_equal(
            f"{split_name} row_count",
            baseline.split.splits[split_name].row_count,
            challenger.split.splits[split_name].row_count,
        )
        _assert_equal(
            f"{split_name} membership",
            baseline.split.splits[split_name].membership_sha256,
            challenger.split.splits[split_name].membership_sha256,
        )
    _assert_equal("split_seed", baseline.split.split_seed, challenger.split.split_seed)
    _assert_equal("model_seed", baseline.split.model_seed, challenger.split.model_seed)
    _assert_equal("sampler_seed", baseline.split.sampler_seed, challenger.split.sampler_seed)

    return TrainingComparisonManifest(
        comparison_id=_comparison_id(
            baseline,
            challenger,
            experiment_plan_id=experiment_plan_id,
        ),
        baseline_run_id=baseline.run_id,
        challenger_run_id=challenger.run_id,
        baseline_snapshot_sha256=baseline.snapshot.dataset_sha256,
        challenger_snapshot_sha256=challenger.snapshot.dataset_sha256,
        baseline_snapshot_manifest_sha256=baseline.snapshot_manifest_sha256,
        challenger_snapshot_manifest_sha256=challenger.snapshot_manifest_sha256,
        baseline_split_manifest_sha256=baseline.split_manifest_sha256,
        challenger_split_manifest_sha256=challenger.split_manifest_sha256,
        baseline_feature_columns_sha256=baseline.split.feature_columns_sha256,
        challenger_feature_columns_sha256=challenger.split.feature_columns_sha256,
        baseline_feature_columns=baseline.split.feature_columns,
        challenger_feature_columns=challenger.split.feature_columns,
        effective_seeds=TrainingSeeds(
            split_seed=baseline.split.split_seed,
            model_seed=baseline.split.model_seed,
            sampler_seed=baseline.split.sampler_seed,
        ),
        experiment_plan_id=experiment_plan_id,
        validated_at=datetime.now(timezone.utc),
    )


def _publish_verified_comparison(
    *,
    result: TrainingComparisonManifest,
    challenger_run_id: str,
    output_path: Path,
    client: MlflowClient,
    workspace: Path,
) -> None:
    """검증된 manifest를 challenger artifact에 먼저 업로드하고 로컬에 원자 게시한다."""
    temporary_manifest = workspace / f"{result.comparison_id}.json"
    write_manifest_atomic(result, temporary_manifest)
    try:
        client.log_artifact(
            challenger_run_id,
            str(temporary_manifest),
            artifact_path="reproducibility/comparisons",
        )
    except Exception as error:
        raise ComparisonValidationError(
            "challenger comparison artifact 업로드에 실패했습니다"
        ) from error

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary_manifest, output_path)
    except OSError as error:
        raise ComparisonValidationError(
            "comparison output 게시에 실패했습니다"
        ) from error


def verify_training_comparison(
    baseline_run_id: str,
    challenger_run_id: str,
    output_path: Path,
    *,
    experiment_plan_id: str | None = None,
) -> TrainingComparisonManifest:
    """두 run의 provenance equality를 검증하고 성공 manifest를 게시한다.

    Args:
        baseline_run_id: 비교 기준 MLflow run ID.
        challenger_run_id: 비교 대상 MLflow run ID.
        output_path: 검증된 manifest를 원자 게시할 로컬 JSON 경로.
        experiment_plan_id: 학습 전에 고정한 experiment plan 식별자. 생략한 legacy
            manifest는 #466 자동 승격 평가에서 fail-closed `hold`가 된다.
    """
    output_path = Path(output_path)
    try:
        client = MlflowClient()
    except Exception as error:
        raise ComparisonValidationError("MLflow client 초기화에 실패했습니다") from error
    with TemporaryDirectory(prefix="training_comparison_") as temporary_dir:
        workspace = Path(temporary_dir)
        baseline = _load_verified_run(baseline_run_id, workspace)
        challenger = _load_verified_run(challenger_run_id, workspace)
        result = _build_comparison_manifest(
            baseline,
            challenger,
            experiment_plan_id=experiment_plan_id,
        )
        _publish_verified_comparison(
            result=result,
            challenger_run_id=challenger_run_id,
            output_path=output_path,
            client=client,
            workspace=workspace,
        )
        return result
