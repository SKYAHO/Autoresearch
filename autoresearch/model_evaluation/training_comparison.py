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

from autoresearch.model_training.training_provenance import (
    TrainingComparisonManifest,
    TrainingSeeds,
    TrainingSnapshotManifest,
    TrainingSplitManifest,
    VerifiedComparisonPromotionEvidence,
    feature_columns_sha256,
    load_training_snapshot_manifest,
    sha256_file,
    write_manifest_atomic,
)
from autoresearch.model_evaluation.promotion_evidence import (
    HeldOutMetricEvidence,
    HeldOutMetricReceipt,
    PromotionEvidenceStore,
    PromotionEvidenceValidationError,
)


class ComparisonValidationError(ValueError):
    """공정 비교에 필요한 artifact 또는 equality 계약이 성립하지 않을 때 발생한다."""


_SNAPSHOT_DATASET_ARTIFACT = "reproducibility/snapshot/training_dataset.csv"
_SNAPSHOT_MANIFEST_ARTIFACT = "reproducibility/snapshot/snapshot_manifest.json"
_SPLIT_MANIFEST_ARTIFACT = "reproducibility/split/split_manifest.json"
_HELD_OUT_METRIC_RECEIPT_ARTIFACT = (
    "reproducibility/metrics/held_out_metric_receipt.json"
)
_EXPECTED_SPLIT_NAMES = {"train", "validation", "test"}


@dataclass(frozen=True)
class _VerifiedRun:
    """artifact 재검증을 통과한 하나의 MLflow run provenance."""

    run_id: str
    snapshot: TrainingSnapshotManifest
    split: TrainingSplitManifest
    snapshot_manifest_sha256: str
    split_manifest_sha256: str
    held_out_metric_receipt: HeldOutMetricReceipt | None


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


def _load_held_out_metric_receipt(
    *,
    run_id: str,
    client: MlflowClient,
    destination: Path,
) -> HeldOutMetricReceipt | None:
    """MLflow 복사본에서 receipt 좌표만 읽고, GCS trust 검증은 호출자에 남긴다."""
    try:
        artifacts = client.list_artifacts(run_id, "reproducibility/metrics")
    except Exception:
        raise ComparisonValidationError(
            f"metric receipt artifact를 run {run_id}에서 조회할 수 없습니다"
        ) from None
    if not any(artifact.path == _HELD_OUT_METRIC_RECEIPT_ARTIFACT for artifact in artifacts):
        return None
    receipt_path = _download_artifact(
        run_id=run_id,
        artifact_path=_HELD_OUT_METRIC_RECEIPT_ARTIFACT,
        destination=destination,
    )
    try:
        return HeldOutMetricReceipt.model_validate_json(
            receipt_path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError):
        raise ComparisonValidationError(
            f"held-out metric receipt가 run {run_id}에서 유효하지 않습니다"
        ) from None


def _load_verified_run(
    run_id: str, workspace: Path, client: MlflowClient
) -> _VerifiedRun:
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
    held_out_metric_receipt = _load_held_out_metric_receipt(
        run_id=run_id,
        client=client,
        destination=run_workspace / "metrics",
    )
    return _VerifiedRun(
        run_id=run_id,
        snapshot=snapshot,
        split=split,
        snapshot_manifest_sha256=snapshot_manifest_sha256,
        split_manifest_sha256=split_manifest_sha256,
        held_out_metric_receipt=held_out_metric_receipt,
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
    promotion_evidence: VerifiedComparisonPromotionEvidence | None,
) -> str:
    """run pair·공통 snapshot·plan binding으로 deterministic ID를 만든다."""
    identity_parts = [
        baseline.run_id,
        challenger.run_id,
        baseline.snapshot.dataset_sha256,
        challenger.split_manifest_sha256,
    ]
    if promotion_evidence is not None:
        identity_parts.append(promotion_evidence.plan_receipt.object.sha256)
    elif experiment_plan_id is not None:
        identity_parts.append(experiment_plan_id)
    payload = "\0".join(identity_parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _validate_fairness(baseline: _VerifiedRun, challenger: _VerifiedRun) -> None:
    """기존 snapshot·split·seed 동등성을 모두 확인한다."""
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


def _mlflow_time(value: int | None, *, label: str, run_id: str) -> datetime:
    """MLflow millisecond timestamp를 UTC로 바꾸고 미완료 run을 거부한다."""
    if value is None:
        raise ComparisonValidationError(f"{label} timestamp가 없는 run입니다: {run_id}")
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def _assert_plan_precedes_run(
    receipt: object, *, run: object, run_id: str
) -> None:
    """GCS server plan 시각이 MLflow run 시작보다 늦지 않은지 확인한다."""
    plan_time = receipt.object.time_created
    run_start = _mlflow_time(run.info.start_time, label="run start", run_id=run_id)
    if plan_time > run_start:
        raise ComparisonValidationError(
            f"plan receipt가 run {run_id} 시작 뒤에 생성되었습니다"
        )


def _assert_metric_binding(
    *,
    metric_receipt: HeldOutMetricReceipt,
    expected_plan_receipt: object,
    verified_run: _VerifiedRun,
    run: object,
    artifact_workspace: Path,
) -> None:
    """GCS에서 읽은 metric을 MLflow run·split·model artifact와 다시 결합한다."""
    metric: HeldOutMetricEvidence = metric_receipt.evidence
    run_id = verified_run.run_id
    if metric.run_id != run_id:
        raise ComparisonValidationError(f"metric run_id mismatch for run {run_id}")
    if metric.plan_receipt != expected_plan_receipt:
        raise ComparisonValidationError(f"metric plan receipt mismatch for run {run_id}")
    if metric.split_manifest_sha256 != verified_run.split_manifest_sha256:
        raise ComparisonValidationError(
            f"metric split manifest sha256 mismatch for run {run_id}"
        )
    if metric.test_membership_sha256 != verified_run.split.splits["test"].membership_sha256:
        raise ComparisonValidationError(
            f"metric test membership mismatch for run {run_id}"
        )
    run_start = _mlflow_time(run.info.start_time, label="run start", run_id=run_id)
    run_end = _mlflow_time(run.info.end_time, label="run end", run_id=run_id)
    metric_time = metric_receipt.object.time_created
    if not run_start <= metric_time <= run_end:
        raise ComparisonValidationError(
            f"metric receipt 시간이 run {run_id} 범위 밖입니다"
        )
    artifact_path = _download_artifact(
        run_id=run_id,
        artifact_path=metric.model_artifact_path,
        destination=artifact_workspace / run_id,
    )
    if sha256_file(artifact_path) != metric.model_artifact_sha256:
        raise ComparisonValidationError(
            f"metric model artifact sha256 mismatch for run {run_id}"
        )


def _verify_promotion_evidence(
    *,
    baseline: _VerifiedRun,
    challenger: _VerifiedRun,
    client: MlflowClient,
    store: PromotionEvidenceStore | None,
    workspace: Path,
) -> VerifiedComparisonPromotionEvidence | None:
    """partial evidence는 거부하고, 완전한 receipt만 GCS에서 재검증한다."""
    baseline_plan = baseline.split.experiment_plan_receipt
    challenger_plan = challenger.split.experiment_plan_receipt
    baseline_metric = baseline.held_out_metric_receipt
    challenger_metric = challenger.held_out_metric_receipt
    if (
        baseline_plan is None
        and challenger_plan is None
        and baseline_metric is None
        and challenger_metric is None
    ):
        return None
    if (
        baseline_plan is None
        or challenger_plan is None
        or baseline_metric is None
        or challenger_metric is None
    ):
        raise ComparisonValidationError("partial promotion evidence가 있는 comparison입니다")
    if store is None:
        raise ComparisonValidationError(
            "promotion evidence receipt가 있는 comparison에는 evidence store가 필요합니다"
        )
    _assert_equal("plan receipt", baseline_plan, challenger_plan)
    try:
        store.verify_plan_receipt(baseline_plan)
        store.verify_held_out_metric_receipt(baseline_metric)
        store.verify_held_out_metric_receipt(challenger_metric)
    except PromotionEvidenceValidationError:
        raise ComparisonValidationError("promotion evidence GCS receipt 검증에 실패했습니다") from None
    try:
        baseline_run = client.get_run(baseline.run_id)
        challenger_run = client.get_run(challenger.run_id)
    except Exception:
        raise ComparisonValidationError("MLflow run 정보를 조회할 수 없습니다") from None
    _assert_plan_precedes_run(baseline_plan, run=baseline_run, run_id=baseline.run_id)
    _assert_plan_precedes_run(
        baseline_plan, run=challenger_run, run_id=challenger.run_id
    )
    artifact_workspace = workspace / "model-artifacts"
    _assert_metric_binding(
        metric_receipt=baseline_metric,
        expected_plan_receipt=baseline_plan,
        verified_run=baseline,
        run=baseline_run,
        artifact_workspace=artifact_workspace,
    )
    _assert_metric_binding(
        metric_receipt=challenger_metric,
        expected_plan_receipt=baseline_plan,
        verified_run=challenger,
        run=challenger_run,
        artifact_workspace=artifact_workspace,
    )
    return VerifiedComparisonPromotionEvidence(
        plan_receipt=baseline_plan,
        baseline_metric=baseline_metric,
        challenger_metric=challenger_metric,
    )


def _build_comparison_manifest(
    baseline: _VerifiedRun,
    challenger: _VerifiedRun,
    *,
    experiment_plan_id: str | None,
    promotion_evidence: VerifiedComparisonPromotionEvidence | None,
) -> TrainingComparisonManifest:
    """이미 검증된 두 run으로 immutable comparison manifest를 만든다."""
    return TrainingComparisonManifest(
        comparison_id=_comparison_id(
            baseline,
            challenger,
            experiment_plan_id=experiment_plan_id,
            promotion_evidence=promotion_evidence,
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
        # legacy caller의 plan_id는 receipt 없는 과거 comparison에서만 보존한다.
        experiment_plan_id=(None if promotion_evidence is not None else experiment_plan_id),
        promotion_evidence=promotion_evidence,
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
    promotion_evidence_store: PromotionEvidenceStore | None = None,
) -> TrainingComparisonManifest:
    """두 run의 provenance equality를 검증하고 성공 manifest를 게시한다.

    Args:
        baseline_run_id: 비교 기준 MLflow run ID.
        challenger_run_id: 비교 대상 MLflow run ID.
        output_path: 검증된 manifest를 원자 게시할 로컬 JSON 경로.
        experiment_plan_id: 학습 전에 고정한 experiment plan 식별자. 생략한 legacy
            manifest는 #466 자동 승격 평가에서 fail-closed `hold`가 된다.
        promotion_evidence_store: plan/metric receipt가 있는 run에서 GCS generation
            pinned 재검증에 사용할 store. 양쪽 run에 receipt가 없으면 필요 없다.
    """
    output_path = Path(output_path)
    try:
        client = MlflowClient()
    except Exception as error:
        raise ComparisonValidationError("MLflow client 초기화에 실패했습니다") from error
    with TemporaryDirectory(prefix="training_comparison_") as temporary_dir:
        workspace = Path(temporary_dir)
        baseline = _load_verified_run(baseline_run_id, workspace, client)
        challenger = _load_verified_run(challenger_run_id, workspace, client)
        _validate_fairness(baseline, challenger)
        promotion_evidence = _verify_promotion_evidence(
            baseline=baseline,
            challenger=challenger,
            client=client,
            store=promotion_evidence_store,
            workspace=workspace,
        )
        result = _build_comparison_manifest(
            baseline,
            challenger,
            experiment_plan_id=experiment_plan_id,
            promotion_evidence=promotion_evidence,
        )
        _publish_verified_comparison(
            result=result,
            challenger_run_id=challenger_run_id,
            output_path=output_path,
            client=client,
            workspace=workspace,
        )
        return result


def revalidate_training_comparison(
    comparison: TrainingComparisonManifest,
    *,
    promotion_evidence_store: PromotionEvidenceStore,
) -> TrainingComparisonManifest:
    """전달받은 comparison JSON을 신뢰하지 않고 MLflow·GCS에서 다시 구성한다.

    evaluator가 로컬 comparison 파일이나 API payload를 직접 받아도, 이 함수는
    run ID에서 snapshot/split/model artifact와 write-once receipt를 다시 읽는다.
    전달값의 `validated_at`·legacy `experiment_plan_id`는 trust source가 아니므로
    canonical 결과 대조에서 제외한다.
    """
    try:
        client = MlflowClient()
    except Exception:
        raise ComparisonValidationError("MLflow client 초기화에 실패했습니다") from None
    with TemporaryDirectory(prefix="training_comparison_revalidation_") as temporary_dir:
        workspace = Path(temporary_dir)
        baseline = _load_verified_run(comparison.baseline_run_id, workspace, client)
        challenger = _load_verified_run(comparison.challenger_run_id, workspace, client)
        _validate_fairness(baseline, challenger)
        promotion_evidence = _verify_promotion_evidence(
            baseline=baseline,
            challenger=challenger,
            client=client,
            store=promotion_evidence_store,
            workspace=workspace,
        )
        if promotion_evidence is None:
            raise ComparisonValidationError("promotion evidence가 없는 legacy comparison입니다")
        canonical = _build_comparison_manifest(
            baseline,
            challenger,
            experiment_plan_id=None,
            promotion_evidence=promotion_evidence,
        )
    excluded = {"validated_at", "experiment_plan_id"}
    if comparison.model_dump(exclude=excluded) != canonical.model_dump(exclude=excluded):
        raise ComparisonValidationError(
            "comparison transport 내용이 canonical verified result와 다릅니다"
        )
    return canonical
