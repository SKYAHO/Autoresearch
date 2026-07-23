from __future__ import annotations

import os
import pickle
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, TypeAlias, TypeVar, assert_never

import joblib
import mlflow
from mlflow.tracking import MlflowClient
from pydantic import TypeAdapter, ValidationError

from src.features.model_contract import (
    CATEGORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
    FeatureContractError,
    require_categorical_feature_columns,
    require_model_feature_columns,
)
from src.serving.service import ProbabilityModel, Reranker

FEATURE_COLUMNS_ADAPTER: Final = TypeAdapter(tuple[str, ...])
CATEGORICAL_CATEGORIES_ADAPTER: Final = TypeAdapter(dict[str, tuple[str | int | float | bool, ...]])
_Metadata = TypeVar("_Metadata")
_PICKLE_METADATA_ERRORS: Final = (
    OSError,
    pickle.UnpicklingError,
    EOFError,
    AttributeError,
    ImportError,
    IndexError,
    KeyError,
    TypeError,
    ValueError,
    OverflowError,
)

# 학습 파이프라인(src/pipeline/train.py Step 8)의 log_artifact 경로와 계약이다.
# 학습 config(src/pipeline/config.yaml artifacts.*) 파일명이 바뀌면 함께 갱신한다.
MLFLOW_MODEL_ARTIFACT_PATH: Final = "model/lgbm_model.joblib"
MLFLOW_FEATURE_COLUMNS_ARTIFACT_PATH: Final = "features/feature_columns.pkl"
MLFLOW_CATEGORICAL_COLUMNS_ARTIFACT_PATH: Final = "features/categorical_columns.pkl"


class ModelSource(StrEnum):
    """모델 아티팩트를 어디서 읽을지 지정하는 소스 종류(로컬 파일 / MLflow 런 / Registry alias)."""

    LOCAL = "local"
    MLFLOW = "mlflow"
    REGISTRY = "registry"


@dataclass(frozen=True, slots=True)
class LocalModelSettings:
    """로컬 파일에서 로드할 때 필요한 모델·피처·카테고리 아티팩트 경로 묶음."""

    model_path: Path
    feature_columns_path: Path
    categorical_columns_path: Path


@dataclass(frozen=True, slots=True)
class MlflowModelSettings:
    """MLflow 런에서 아티팩트를 내려받을 때 필요한 tracking URI와 run_id."""

    tracking_uri: str
    run_id: str


@dataclass(frozen=True, slots=True)
class RegistryModelSettings:
    """Model Registry alias(예: models:/ctr-model@champion)로 로드할 때 필요한 설정."""

    tracking_uri: str
    model_name: str
    alias: str


ModelSettings: TypeAlias = LocalModelSettings | MlflowModelSettings | RegistryModelSettings


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """로드된 Reranker와 계보(run_id·Registry 버전)를 함께 담는다.

    local 소스는 run_id="local", registry가 아니면 model_version=None이다.
    """

    reranker: Reranker
    run_id: str
    model_version: str | None


@dataclass(frozen=True, slots=True)
class ModelConfigurationError(Exception):
    """환경변수 설정이 잘못됐을 때 발생한다(소스 값 오류·필수 변수 누락 등)."""

    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class ModelArtifactError(Exception):
    """아티팩트 자체가 없거나 형식·계약이 어긋날 때 발생한다."""

    reason: str

    def __str__(self) -> str:
        return self.reason


def load_model_settings_from_environment() -> ModelSettings:
    """환경변수(RERANK_MODEL_SOURCE 등)를 읽어 소스별 설정 객체로 변환한다."""
    raw_source = os.getenv("RERANK_MODEL_SOURCE", ModelSource.LOCAL.value)
    try:
        source = ModelSource(raw_source)
    except ValueError as error:
        raise ModelConfigurationError(
            reason="RERANK_MODEL_SOURCE must be 'local', 'mlflow', or 'registry'."
        ) from error

    match source:
        case ModelSource.LOCAL:
            return LocalModelSettings(
                model_path=Path(_required_environment_value("RERANK_MODEL_PATH")),
                feature_columns_path=Path(
                    _required_environment_value("RERANK_FEATURE_COLUMNS_PATH")
                ),
                categorical_columns_path=Path(
                    _required_environment_value("RERANK_CATEGORICAL_COLUMNS_PATH")
                ),
            )
        case ModelSource.MLFLOW:
            return MlflowModelSettings(
                tracking_uri=_required_environment_value("MLFLOW_TRACKING_URI"),
                run_id=_required_environment_value("RERANK_MLFLOW_RUN_ID"),
            )
        case ModelSource.REGISTRY:
            return RegistryModelSettings(
                tracking_uri=_required_environment_value("MLFLOW_TRACKING_URI"),
                model_name=os.getenv("RERANK_REGISTRY_MODEL_NAME", "ctr-model"),
                alias=os.getenv("RERANK_REGISTRY_ALIAS", "champion"),
            )
        case unreachable:
            assert_never(unreachable)


def load_reranker(settings: ModelSettings) -> Reranker:
    """설정 종류에 따라 로컬/MLflow 로더로 분기해 Reranker를 만든다."""
    match settings:
        case LocalModelSettings():
            return load_local_model(settings)
        case MlflowModelSettings():
            return load_mlflow_model(settings)
        case RegistryModelSettings():
            return _load_registry_model(settings).reranker
        case unreachable:
            assert_never(unreachable)


def load_local_model(settings: LocalModelSettings) -> Reranker:
    """로컬 경로의 아티팩트들로 Reranker를 로드한다."""
    return _load_reranker(
        model_path=settings.model_path,
        feature_columns_path=settings.feature_columns_path,
        categorical_columns_path=settings.categorical_columns_path,
    )


def load_mlflow_model(settings: MlflowModelSettings) -> Reranker:
    """MLflow 런에서 모델·피처·카테고리 아티팩트를 내려받아 Reranker를 로드한다."""
    mlflow.set_tracking_uri(settings.tracking_uri)
    model_path = Path(
        mlflow.artifacts.download_artifacts(
            artifact_uri=f"runs:/{settings.run_id}/{MLFLOW_MODEL_ARTIFACT_PATH}"
        )
    )
    feature_columns_path = Path(
        mlflow.artifacts.download_artifacts(
            artifact_uri=f"runs:/{settings.run_id}/{MLFLOW_FEATURE_COLUMNS_ARTIFACT_PATH}"
        )
    )
    categorical_columns_path = Path(
        mlflow.artifacts.download_artifacts(
            artifact_uri=f"runs:/{settings.run_id}/{MLFLOW_CATEGORICAL_COLUMNS_ARTIFACT_PATH}"
        )
    )
    return _load_reranker(
        model_path=model_path,
        feature_columns_path=feature_columns_path,
        categorical_columns_path=categorical_columns_path,
    )


def _load_registry_model(settings: RegistryModelSettings) -> ResolvedModel:
    """Registry alias를 run_id로 해석한 뒤 기존 run 아티팩트 다운로드 경로를 재사용한다."""
    mlflow.set_tracking_uri(settings.tracking_uri)
    try:
        version = MlflowClient().get_model_version_by_alias(settings.model_name, settings.alias)
    except Exception as error:
        raise ModelArtifactError(
            reason=(
                f"Failed to resolve registry alias models:/{settings.model_name}"
                f"@{settings.alias}: {error}"
            )
        ) from error
    reranker = load_mlflow_model(
        MlflowModelSettings(tracking_uri=settings.tracking_uri, run_id=version.run_id)
    )
    return ResolvedModel(
        reranker=reranker, run_id=version.run_id, model_version=str(version.version)
    )


def load_reranker_with_lineage(settings: ModelSettings) -> ResolvedModel:
    """설정 종류에 따라 로드하고 계보(run_id·버전)를 함께 반환한다."""
    match settings:
        case RegistryModelSettings():
            return _load_registry_model(settings)
        case MlflowModelSettings():
            return ResolvedModel(
                reranker=load_mlflow_model(settings), run_id=settings.run_id, model_version=None
            )
        case LocalModelSettings():
            return ResolvedModel(
                reranker=load_local_model(settings), run_id="local", model_version=None
            )
        case unreachable:
            assert_never(unreachable)


def _load_reranker(
    model_path: Path, feature_columns_path: Path, categorical_columns_path: Path
) -> Reranker:
    """세 아티팩트의 존재·형식·상호 계약(카테고리 컬럼 ⊆ 피처)을 검증하고 Reranker를 조립한다."""
    if not model_path.is_file():
        raise ModelArtifactError(reason=f"Model artifact does not exist: {model_path}")
    if not feature_columns_path.is_file():
        raise ModelArtifactError(
            reason=f"Feature-column artifact does not exist: {feature_columns_path}"
        )
    if not categorical_columns_path.is_file():
        raise ModelArtifactError(
            reason=(
                "Categorical-column artifact does not exist: "
                f"{categorical_columns_path} (categorical_columns.pkl을 저장하는 "
                "학습 파이프라인으로 재학습이 필요합니다.)"
            )
        )

    model = joblib.load(model_path)
    if not isinstance(model, ProbabilityModel):
        raise ModelArtifactError(reason="Loaded model does not implement predict_proba.")

    feature_columns = _load_pickled_metadata(
        feature_columns_path,
        adapter=FEATURE_COLUMNS_ADAPTER,
        artifact_label="Feature-column",
        malformed_reason="must contain a sequence of strings.",
    )

    try:
        require_model_feature_columns(feature_columns)
    except FeatureContractError as error:
        raise ModelArtifactError(
            reason=(
                "Feature-column artifact does not match the canonical model feature "
                f"contract at {feature_columns_path}; expected "
                f"{len(MODEL_FEATURE_COLUMNS)} ordered columns, got {feature_columns!r}: "
                f"{error}"
            )
        ) from error

    categorical_categories = _load_pickled_metadata(
        categorical_columns_path,
        adapter=CATEGORICAL_CATEGORIES_ADAPTER,
        artifact_label="Categorical-column",
        malformed_reason="must map column names to category lists.",
    )

    categorical_columns = tuple(categorical_categories)
    try:
        require_categorical_feature_columns(categorical_columns)
    except FeatureContractError as error:
        raise ModelArtifactError(
            reason=(
                "Categorical-column artifact does not match the canonical categorical "
                f"feature contract at {categorical_columns_path}; expected "
                f"{len(CATEGORICAL_FEATURE_COLUMNS)} ordered columns, got "
                f"{categorical_columns!r}: {error}"
            )
        ) from error

    unknown_columns = tuple(
        column for column in categorical_categories if column not in feature_columns
    )
    if unknown_columns:
        raise ModelArtifactError(
            reason=(
                "Categorical-column artifact has columns outside the feature set: "
                f"{', '.join(unknown_columns)}"
            )
        )

    return Reranker(
        model=model,
        feature_columns=feature_columns,
        categorical_categories=categorical_categories,
    )


def _load_pickled_metadata(
    path: Path,
    *,
    adapter: TypeAdapter[_Metadata],
    artifact_label: str,
    malformed_reason: str,
) -> _Metadata:
    try:
        with path.open("rb") as metadata_file:
            return adapter.validate_python(pickle.load(metadata_file))
    except ValidationError as error:
        raise ModelArtifactError(
            reason=(
                f"{artifact_label} artifact {malformed_reason} "
                f"(path: {path})"
            )
        ) from error
    except _PICKLE_METADATA_ERRORS as error:
        raise ModelArtifactError(
            reason=(
                f"{artifact_label} artifact could not be deserialized from "
                f"{path}: {error}"
            )
        ) from error


def _required_environment_value(name: str) -> str:
    """필수 환경변수를 읽고, 없거나 공백이면 ModelConfigurationError를 던진다."""
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ModelConfigurationError(reason=f"{name} is required to load the reranking model.")
    return value
