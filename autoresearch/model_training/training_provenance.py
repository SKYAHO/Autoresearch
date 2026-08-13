"""학습 데이터 snapshot과 실험 비교 provenance 계약.

[파이프라인] dataset assembly가 만든 CSV와 training이 확정한 split을 immutable한
JSON manifest로 식별하고, MLflow run 사이의 동일성 검증에 사용할 hash·seed·피처
계약을 제공한다.

[기능] Pydantic v2 manifest 모델, canonical JSON SHA-256 계산, sidecar 원자 게시,
snapshot 무결성 재검증, train/model/sampler seed 해석을 제공한다.

[비책임] Feast 조회·GCS object 다운로드·모델 fit·MLflow artifact 전송·비교 CLI의
실행은 각각 dataset assembly, training, comparison, CLI 모듈이 담당한다. 이 모듈은
그 경계에서 교환되는 값의 형식과 순수 검증만 담당한다.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, ValidationError

from autoresearch.model_evaluation.promotion_evidence import ExperimentPlanReceipt, HeldOutMetricReceipt

SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ProvenanceValidationError(ValueError):
    """검증되지 않은 snapshot 또는 provenance artifact를 거부하는 오류."""


class _ImmutableModel(BaseModel):
    """모든 provenance 계약에 적용하는 엄격한 Pydantic 기본 설정."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
    )


class DatasetColumn(_ImmutableModel):
    """CSV schema의 순서 보존 column 설명."""

    name: str
    dtype: str


class RegistryProvenance(_ImmutableModel):
    """Feast가 실제로 읽은 registry object의 identity."""

    uri: str
    generation: str = Field(min_length=1)
    sha256: str = Field(pattern=SHA256_PATTERN)


class TrainingSeeds(_ImmutableModel):
    """split, model, sampler 각 난수 소비 지점에 적용할 effective seed."""

    split_seed: int
    model_seed: int
    sampler_seed: int


def resolve_training_seeds(
    *,
    random_state: int | None,
    split_seed: int | None,
    model_seed: int | None,
    sampler_seed: int | None,
    config_seed: int,
) -> TrainingSeeds:
    """legacy random_state 또는 explicit seed triplet을 effective seed로 해석한다."""
    explicit = (split_seed, model_seed, sampler_seed)
    if any(seed is not None for seed in explicit):
        if random_state is not None or any(seed is None for seed in explicit):
            raise ValueError(
                "split_seed, model_seed, sampler_seed를 모두 지정해야 합니다; "
                "random_state와 함께 지정할 수 없습니다"
            )
        return TrainingSeeds(
            split_seed=split_seed,
            model_seed=model_seed,
            sampler_seed=sampler_seed,
        )
    seed = config_seed if random_state is None else random_state
    return TrainingSeeds(split_seed=seed, model_seed=seed, sampler_seed=seed)


class TrainingSnapshotManifest(_ImmutableModel):
    """최종 training CSV와 Feast assembly 조건의 content identity."""

    manifest_version: Literal["training_snapshot_manifest_v1"] = (
        "training_snapshot_manifest_v1"
    )
    dataset_sha256: str = Field(pattern=SHA256_PATTERN)
    schema_sha256: str = Field(pattern=SHA256_PATTERN)
    row_count: NonNegativeInt
    columns: list[DatasetColumn]
    created_at: datetime
    events_start_date: date
    events_end_date: date
    timezone: str = "Asia/Seoul"
    assembly_source: Literal["feast"] = "feast"
    feature_service: str
    registry_uri: str
    registry_generation: str = Field(min_length=1)
    registry_sha256: str = Field(pattern=SHA256_PATTERN)
    code_archive_sha: str | None = None
    # 기존 v1 snapshot manifest JSON은 이 필드가 없으므로 None으로 읽는다. 재사용
    # 학습(--dataset-uri)은 None이면 커버리지 게이트(#464)를 검증할 수 없어 거부한다.
    # 스키마를 필수로 만들지 않는 이유는 TrainingSplitManifest.experiment_plan_receipt와
    # 같다 — 이 필드와 무관한 기존 소비자가 스키마 변경에 얽히지 않게 한다.
    spine_usable_days: NonNegativeInt | None = None


MAX_POINTER_HISTORY = 10


class SnapshotPointerEntry(_ImmutableModel):
    """by-date 포인터가 이전에 가리켰던 스냅샷 한 건."""

    dataset_sha256: str = Field(pattern=SHA256_PATTERN)
    published_at: datetime


class TrainingSnapshotPointer(_ImmutableModel):
    """(events_end_date, feature_service) 좌표의 최신 스냅샷 포인터.

    by-hash object는 불변이고 재현은 항상 sha 주소로 하므로, 이 포인터가 최신으로
    이동해도 과거 학습은 깨지지 않는다. ``previous``를 캡 없이 두면 매 갱신이 전체를
    읽고 쓰는 비용이 계속 커지므로 최근 ``MAX_POINTER_HISTORY``개만 보존한다.
    """

    pointer_version: Literal["training_snapshot_pointer_v1"] = (
        "training_snapshot_pointer_v1"
    )
    dataset_sha256: str = Field(pattern=SHA256_PATTERN)
    uri: str = Field(min_length=1)
    events_start_date: date
    events_end_date: date
    feature_service: str = Field(min_length=1)
    registry_generation: str = Field(min_length=1)
    published_at: datetime
    previous: list[SnapshotPointerEntry] = Field(
        default_factory=list, max_length=MAX_POINTER_HISTORY
    )


class SplitMembership(_ImmutableModel):
    """하나의 train/validation/test split membership 요약."""

    row_count: NonNegativeInt
    membership_sha256: str = Field(pattern=SHA256_PATTERN)


class TrainingSplitManifest(_ImmutableModel):
    """하나의 MLflow run이 사용한 split과 feature provenance."""

    manifest_version: Literal["training_split_manifest_v1"] = "training_split_manifest_v1"
    run_id: str = Field(min_length=1)
    snapshot_sha256: str = Field(pattern=SHA256_PATTERN)
    snapshot_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    split_seed: int
    model_seed: int
    sampler_seed: int
    test_size: float = Field(ge=0, lt=1)
    val_size: float = Field(ge=0, lt=1)
    splits: dict[str, SplitMembership]
    feature_columns_sha256: str = Field(pattern=SHA256_PATTERN)
    feature_columns: list[str]
    # 기존 v1 split manifest JSON은 이 필드가 없으므로 None으로 읽는다. receipt가
    # 있는 새 run만 자동 승격 evidence 경로에서 사용할 수 있다.
    experiment_plan_receipt: ExperimentPlanReceipt | None = None


class VerifiedComparisonPromotionEvidence(_ImmutableModel):
    """fair comparison이 GCS에서 재검증한 plan·두 held-out metric receipt."""

    plan_receipt: ExperimentPlanReceipt
    baseline_metric: HeldOutMetricReceipt
    challenger_metric: HeldOutMetricReceipt


class TrainingComparisonManifest(_ImmutableModel):
    """두 MLflow run의 verified comparison 결과."""

    manifest_version: Literal["training_comparison_manifest_v1"] = (
        "training_comparison_manifest_v1"
    )
    comparison_id: str = Field(min_length=1)
    baseline_run_id: str = Field(min_length=1)
    challenger_run_id: str = Field(min_length=1)
    baseline_snapshot_sha256: str = Field(pattern=SHA256_PATTERN)
    challenger_snapshot_sha256: str = Field(pattern=SHA256_PATTERN)
    baseline_snapshot_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    challenger_snapshot_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    baseline_split_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    challenger_split_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    baseline_feature_columns_sha256: str = Field(pattern=SHA256_PATTERN)
    challenger_feature_columns_sha256: str = Field(pattern=SHA256_PATTERN)
    baseline_feature_columns: list[str]
    challenger_feature_columns: list[str]
    # 이전에 기록된 comparison artifact도 읽어야 한다. 다만 None이면 자동 승격
    # 정책이 요구하는 effective seed·사전 선언 plan 근거가 없으므로 이후 evaluator가
    # fail-closed 한다.
    effective_seeds: TrainingSeeds | None = None
    experiment_plan_id: str | None = Field(default=None, min_length=1)
    promotion_evidence: VerifiedComparisonPromotionEvidence | None = None
    validated_at: datetime
    validation_status: Literal["verified"] = "verified"


def snapshot_manifest_path(dataset_path: Path) -> Path:
    """CSV 경로에 대응하는 snapshot sidecar 경로를 반환한다."""
    return Path(f"{dataset_path}.snapshot.json")


def split_manifest_path(test_set_path: Path) -> Path:
    """held-out test CSV에 대응하는 split sidecar 경로를 반환한다."""
    return Path(f"{test_set_path}.split.json")


def sha256_file(path: Path) -> str:
    """파일 전체 byte의 SHA-256을 계산한다."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def schema_sha256(frame: pd.DataFrame) -> str:
    """DataFrame의 ordered column name/dtype schema hash를 반환한다."""
    columns = [{"name": str(name), "dtype": str(dtype)} for name, dtype in frame.dtypes.items()]
    return _sha256_bytes(_canonical_json(columns))


def feature_columns_sha256(columns: Sequence[str]) -> str:
    """최종 모델 입력 column 순서의 canonical hash를 반환한다."""
    return _sha256_bytes(_canonical_json(list(columns)))


def membership_sha256(positions: Sequence[int]) -> str:
    """source CSV 0-based row position 집합의 canonical hash를 반환한다."""
    return _sha256_bytes(_canonical_json(sorted(int(position) for position in positions)))


def _as_date(value: date | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def build_snapshot_manifest(
    *,
    dataset_path: Path,
    events_start_date: date | str,
    events_end_date: date | str,
    feature_service: str,
    registry: RegistryProvenance,
    code_archive_sha: str | None,
    spine_usable_days: int | None = None,
    created_at: datetime | None = None,
) -> TrainingSnapshotManifest:
    """현재 CSV byte/schema를 읽어 snapshot manifest를 만든다."""
    frame = pd.read_csv(dataset_path)
    return TrainingSnapshotManifest(
        dataset_sha256=sha256_file(dataset_path),
        schema_sha256=schema_sha256(frame),
        row_count=len(frame),
        columns=[
            DatasetColumn(name=str(name), dtype=str(dtype))
            for name, dtype in frame.dtypes.items()
        ],
        created_at=created_at or datetime.now(timezone.utc),
        events_start_date=_as_date(events_start_date),
        events_end_date=_as_date(events_end_date),
        feature_service=feature_service,
        registry_uri=registry.uri,
        registry_generation=registry.generation,
        registry_sha256=registry.sha256,
        code_archive_sha=code_archive_sha,
        spine_usable_days=spine_usable_days,
    )


def write_manifest_atomic(model: BaseModel, path: Path) -> None:
    """manifest JSON을 temp file fsync 후 target에 원자적으로 게시한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(model.model_dump_json(indent=2))
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _raise_manifest_error(message: str) -> ProvenanceValidationError:
    return ProvenanceValidationError(message)


def load_training_snapshot_manifest(dataset_path: Path) -> TrainingSnapshotManifest:
    """sidecar와 현재 CSV의 byte/schema/row count 일치를 검증한다."""
    manifest_path = snapshot_manifest_path(dataset_path)
    if not manifest_path.is_file():
        raise _raise_manifest_error(f"snapshot manifest missing: {manifest_path}")
    try:
        manifest = TrainingSnapshotManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError) as error:
        raise _raise_manifest_error(f"snapshot manifest invalid: {manifest_path}") from error

    try:
        frame = pd.read_csv(dataset_path)
        actual_dataset_sha256 = sha256_file(dataset_path)
        actual_schema_sha256 = schema_sha256(frame)
        actual_row_count = len(frame)
    except (OSError, ValueError, pd.errors.ParserError) as error:
        raise _raise_manifest_error(f"snapshot dataset unreadable: {dataset_path}") from error

    if actual_dataset_sha256 != manifest.dataset_sha256:
        raise _raise_manifest_error(
            f"dataset_sha256 mismatch for {dataset_path}: "
            f"expected={manifest.dataset_sha256} actual={actual_dataset_sha256}"
        )
    if actual_schema_sha256 != manifest.schema_sha256:
        raise _raise_manifest_error(
            f"schema_sha256 mismatch for {dataset_path}: "
            f"expected={manifest.schema_sha256} actual={actual_schema_sha256}"
        )
    if actual_row_count != manifest.row_count:
        raise _raise_manifest_error(
            f"row_count mismatch for {dataset_path}: "
            f"expected={manifest.row_count} actual={actual_row_count}"
        )
    return manifest


def build_split_manifest(
    *,
    run_id: str,
    snapshot: TrainingSnapshotManifest,
    snapshot_manifest_sha256: str,
    seeds: TrainingSeeds,
    test_size: float,
    val_size: float,
    split_positions: Mapping[str, Sequence[int]],
    feature_columns: Sequence[str],
    experiment_plan_receipt: ExperimentPlanReceipt | None = None,
) -> TrainingSplitManifest:
    """source row position과 feature 순서로 split manifest를 만든다."""
    expected_names = {"train", "validation", "test"}
    if set(split_positions) != expected_names:
        raise ValueError("split_positions는 train, validation, test를 정확히 포함해야 합니다")
    splits = {
        name: SplitMembership(
            row_count=len(positions),
            membership_sha256=membership_sha256(positions),
        )
        for name, positions in split_positions.items()
    }
    return TrainingSplitManifest(
        run_id=run_id,
        snapshot_sha256=snapshot.dataset_sha256,
        snapshot_manifest_sha256=snapshot_manifest_sha256,
        split_seed=seeds.split_seed,
        model_seed=seeds.model_seed,
        sampler_seed=seeds.sampler_seed,
        test_size=test_size,
        val_size=val_size,
        splits=splits,
        feature_columns_sha256=feature_columns_sha256(feature_columns),
        feature_columns=list(feature_columns),
        experiment_plan_receipt=experiment_plan_receipt,
    )
