from __future__ import annotations

import os
import pickle
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, TypeAlias, assert_never

import joblib
import mlflow
from pydantic import TypeAdapter, ValidationError

from src.serving.service import ProbabilityModel, Reranker

FEATURE_COLUMNS_ADAPTER: Final = TypeAdapter(tuple[str, ...])


class ModelSource(StrEnum):
    LOCAL = "local"
    MLFLOW = "mlflow"


@dataclass(frozen=True, slots=True)
class LocalModelSettings:
    model_path: Path
    feature_columns_path: Path


@dataclass(frozen=True, slots=True)
class MlflowModelSettings:
    tracking_uri: str
    run_id: str


ModelSettings: TypeAlias = LocalModelSettings | MlflowModelSettings


@dataclass(frozen=True, slots=True)
class ModelConfigurationError(Exception):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class ModelArtifactError(Exception):
    reason: str

    def __str__(self) -> str:
        return self.reason


def load_model_settings_from_environment() -> ModelSettings:
    raw_source = os.getenv("RERANK_MODEL_SOURCE", ModelSource.LOCAL.value)
    try:
        source = ModelSource(raw_source)
    except ValueError as error:
        raise ModelConfigurationError(
            reason="RERANK_MODEL_SOURCE must be 'local' or 'mlflow'."
        ) from error

    match source:
        case ModelSource.LOCAL:
            return LocalModelSettings(
                model_path=Path(_required_environment_value("RERANK_MODEL_PATH")),
                feature_columns_path=Path(
                    _required_environment_value("RERANK_FEATURE_COLUMNS_PATH")
                ),
            )
        case ModelSource.MLFLOW:
            return MlflowModelSettings(
                tracking_uri=_required_environment_value("MLFLOW_TRACKING_URI"),
                run_id=_required_environment_value("RERANK_MLFLOW_RUN_ID"),
            )
        case unreachable:
            assert_never(unreachable)


def load_reranker(settings: ModelSettings) -> Reranker:
    match settings:
        case LocalModelSettings():
            return load_local_model(settings)
        case MlflowModelSettings():
            return load_mlflow_model(settings)
        case unreachable:
            assert_never(unreachable)


def load_local_model(settings: LocalModelSettings) -> Reranker:
    return _load_reranker(
        model_path=settings.model_path,
        feature_columns_path=settings.feature_columns_path,
    )


def load_mlflow_model(settings: MlflowModelSettings) -> Reranker:
    mlflow.set_tracking_uri(settings.tracking_uri)
    model_path = Path(
        mlflow.artifacts.download_artifacts(
            artifact_uri=f"runs:/{settings.run_id}/model/lgbm_model.joblib"
        )
    )
    feature_columns_path = Path(
        mlflow.artifacts.download_artifacts(
            artifact_uri=f"runs:/{settings.run_id}/features/feature_columns.pkl"
        )
    )
    return _load_reranker(model_path=model_path, feature_columns_path=feature_columns_path)


def _load_reranker(model_path: Path, feature_columns_path: Path) -> Reranker:
    if not model_path.is_file():
        raise ModelArtifactError(reason=f"Model artifact does not exist: {model_path}")
    if not feature_columns_path.is_file():
        raise ModelArtifactError(
            reason=f"Feature-column artifact does not exist: {feature_columns_path}"
        )

    model = joblib.load(model_path)
    if not isinstance(model, ProbabilityModel):
        raise ModelArtifactError(reason="Loaded model does not implement predict_proba.")

    with feature_columns_path.open("rb") as feature_columns_file:
        try:
            feature_columns = FEATURE_COLUMNS_ADAPTER.validate_python(
                pickle.load(feature_columns_file)
            )
        except ValidationError as error:
            raise ModelArtifactError(
                reason="Feature-column artifact must contain a sequence of strings."
            ) from error

    if not feature_columns:
        raise ModelArtifactError(reason="Feature-column artifact must not be empty.")
    return Reranker(model=model, feature_columns=feature_columns)


def _required_environment_value(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ModelConfigurationError(reason=f"{name} is required to load the reranking model.")
    return value
