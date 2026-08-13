from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, TypeAlias, TypeVar, assert_never

import mlflow
from mlflow.tracking import MlflowClient
from pydantic import TypeAdapter, ValidationError

from autoresearch.feature_engineering.model_contract import (
    CATEGORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
    FeatureContractError,
    require_categorical_feature_columns,
    require_model_feature_columns,
)
from autoresearch.model_training.calibration import CALIBRATION_PARAM_FILENAME, DownsamplingCalibrator
from src.serving.onnx_model import OnnxProbabilityModel
from src.serving.service import ProbabilityModel, Reranker
from autoresearch.model_registry.model_package import load_manifest, verify_model_package

logger = logging.getLogger(__name__)

FEATURE_COLUMNS_ADAPTER: Final = TypeAdapter(tuple[str, ...])
CATEGORICAL_CATEGORIES_ADAPTER: Final = TypeAdapter(dict[str, tuple[str | int | float | bool, ...]])
_Metadata = TypeVar("_Metadata")
_JSON_METADATA_ERRORS: Final = (
    OSError,
    UnicodeDecodeError,
    json.JSONDecodeError,
)

# 학습 파이프라인(autoresearch/model_training/train.py Step 8)의 log_artifact 경로와 계약이다.
# 학습 config(autoresearch/model_training/config.yaml artifacts.*) 파일명이 바뀌면 함께 갱신한다.
MLFLOW_FEATURE_COLUMNS_ARTIFACT_PATH: Final = "features/feature_columns.json"
MLFLOW_CATEGORICAL_COLUMNS_ARTIFACT_PATH: Final = "features/categorical_columns.json"
# calibration 상수 아티팩트(JSON w). 학습 train.py의 artifact_path="calibration"와 계약.
# main과 **같은 run** 아래 이 경로로 로깅된다(#390 run_id 종속) — 별도 등록 모델이 아니다.
MLFLOW_CALIBRATION_ARTIFACT_PATH: Final = f"calibration/{CALIBRATION_PARAM_FILENAME}"
# ONNX 모델과 manifest 경로는 학습 train.py Step 8b의 fail-closed 패키지 계약이다.
MLFLOW_ONNX_MODEL_ARTIFACT_PATH: Final = "model_onnx"
MLFLOW_MANIFEST_ARTIFACT_PATH: Final = "manifest/manifest.json"


class ModelSource(StrEnum):
    """모델 아티팩트를 어디서 읽을지 지정하는 소스 종류(로컬 파일 / MLflow 런 / Registry alias)."""

    LOCAL = "local"
    MLFLOW = "mlflow"
    REGISTRY = "registry"


@dataclass(frozen=True, slots=True)
class LocalModelSettings:
    """로컬 ONNX 배포 패키지 경로 묶음."""

    onnx_model_path: Path
    feature_columns_path: Path
    categorical_columns_path: Path
    manifest_path: Path
    calibration_model_path: Path | None = None


@dataclass(frozen=True, slots=True)
class MlflowModelSettings:
    """MLflow run의 단일 배포 패키지를 로드하는 좌표."""

    tracking_uri: str
    run_id: str


@dataclass(frozen=True, slots=True)
class RegistryModelSettings:
    """Model Registry alias(예: models:/ctr-model@champion)로 로드할 때 필요한 설정.

    main을 alias로 resolve한 뒤, main이 downsampling(`sampling_rate` tag < 1.0)이면 그 버전의
    run_id로 **같은 run**의 calibration 아티팩트를 함께 로드해 체이닝한다(#390 run_id 종속).
    calibration은 별도 등록 모델·alias가 아니므로 여기서 지정하지 않는다.
    """

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


# Exception traceback은 런타임이 속성을 할당하므로 frozen dataclass로 만들지 않는다.
@dataclass(slots=True)
class ModelConfigurationError(Exception):
    """환경변수 설정이 잘못됐을 때 발생한다(소스 값 오류·필수 변수 누락 등)."""

    reason: str

    def __str__(self) -> str:
        return self.reason


# 위와 동일하게 traceback chaining을 허용한다.
@dataclass(slots=True)
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
            calibration_path = os.getenv("RERANK_CALIBRATION_MODEL_PATH")
            return LocalModelSettings(
                onnx_model_path=Path(_required_environment_value("RERANK_ONNX_MODEL_PATH")),
                feature_columns_path=Path(
                    _required_environment_value("RERANK_FEATURE_COLUMNS_PATH")
                ),
                categorical_columns_path=Path(
                    _required_environment_value("RERANK_CATEGORICAL_COLUMNS_PATH")
                ),
                manifest_path=Path(_required_environment_value("RERANK_MANIFEST_PATH")),
                calibration_model_path=Path(calibration_path) if calibration_path else None,
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
    """로컬 ONNX 패키지를 manifest로 검증한 뒤 Reranker를 로드한다."""
    try:
        manifest = load_manifest(settings.manifest_path)
        onnx_dir = settings.onnx_model_path.parent
        manifest_onnx_path = onnx_dir / manifest.artifacts.model_onnx.entrypoint
        if settings.onnx_model_path != manifest_onnx_path:
            raise ModelArtifactError(
                reason=(
                    "로컬 ONNX 경로는 manifest entrypoint와 일치해야 합니다: "
                    f"{manifest_onnx_path}"
                )
            )
        verify_model_package(
            manifest,
            model_onnx=onnx_dir,
            feature_columns=settings.feature_columns_path,
            categorical_columns=settings.categorical_columns_path,
            calibration=settings.calibration_model_path,
        )
        calibration = _load_calibration(settings.calibration_model_path)
        onnx_session = _build_onnx_session_from_path(manifest_onnx_path)
        return _load_reranker(
            feature_columns_path=settings.feature_columns_path,
            categorical_columns_path=settings.categorical_columns_path,
            calibration=calibration,
            onnx_session=onnx_session,
            workspace_owner=None,
        )
    except Exception as error:
        if isinstance(error, ModelArtifactError):
            raise
        raise ModelArtifactError(reason=f"로컬 모델 패키지 검증 실패: {error}") from error


def load_mlflow_model(settings: MlflowModelSettings) -> Reranker:
    """MLflow run 패키지를 전용 workspace에 받아 검증한 뒤 ONNX로 로드한다."""
    mlflow.set_tracking_uri(settings.tracking_uri)
    owner = tempfile.TemporaryDirectory(prefix="ctr-model-serving-")
    workspace = Path(owner.name)
    try:
        manifest_path = _download_run_artifact(
            settings.run_id, MLFLOW_MANIFEST_ARTIFACT_PATH, workspace
        )
        manifest = load_manifest(manifest_path)
        onnx_dir = _download_run_artifact(
            settings.run_id, MLFLOW_ONNX_MODEL_ARTIFACT_PATH, workspace
        )
        feature_columns_path = _download_run_artifact(
            settings.run_id, MLFLOW_FEATURE_COLUMNS_ARTIFACT_PATH, workspace
        )
        categorical_columns_path = _download_run_artifact(
            settings.run_id, MLFLOW_CATEGORICAL_COLUMNS_ARTIFACT_PATH, workspace
        )
        calibration_path = (
            _download_run_artifact(
                settings.run_id, MLFLOW_CALIBRATION_ARTIFACT_PATH, workspace
            )
            if manifest.sampling_rate < 1.0
            else None
        )
        verify_model_package(
            manifest,
            model_onnx=onnx_dir,
            feature_columns=feature_columns_path,
            categorical_columns=categorical_columns_path,
            calibration=calibration_path,
        )
        onnx_path = onnx_dir / manifest.artifacts.model_onnx.entrypoint
        session = _build_onnx_session_from_path(onnx_path)
        calibration = _load_calibration(calibration_path)
        reranker = _load_reranker(
            feature_columns_path=feature_columns_path,
            categorical_columns_path=categorical_columns_path,
            calibration=calibration,
            onnx_session=session,
            workspace_owner=owner,
        )
        logger.info("검증된 ONNX 모델 패키지 로드 완료 — run=%s", settings.run_id)
        return reranker
    except Exception as error:
        owner.cleanup()
        if isinstance(error, ModelArtifactError):
            raise
        raise ModelArtifactError(
            reason=f"MLflow 모델 패키지 검증·로드 실패(run={settings.run_id}): {error}"
        ) from error


def _download_run_artifact(run_id: str, artifact_path: str, workspace: Path) -> Path:
    """동일 run의 아티팩트를 프로세스 전용 workspace로 다운로드한다."""
    return Path(
        mlflow.artifacts.download_artifacts(
            artifact_uri=f"runs:/{run_id}/{artifact_path}", dst_path=str(workspace)
        )
    )


def _build_onnx_session_from_path(onnx_model_path: Path):
    """로컬 .onnx 파일에서 onnxruntime 추론 세션을 만든다(#302/#179).

    onnx_model_path가 명시적으로 지정된 경로에서만 호출되므로, 파일이 없거나 세션 생성이
    실패하면 misconfiguration으로 보고 ModelArtifactError로 fail-closed한다.
    """
    if not onnx_model_path.is_file():
        raise ModelArtifactError(reason=f"ONNX model artifact does not exist: {onnx_model_path}")
    try:
        import onnxruntime as ort

        return ort.InferenceSession(str(onnx_model_path))
    except Exception as error:
        raise ModelArtifactError(
            reason=f"ONNX 세션을 만들지 못했습니다({onnx_model_path}): {error}"
        ) from error


def _load_calibration(path: Path | None) -> DownsamplingCalibrator | None:
    """검증된 calibration JSON을 모델로 로드한다."""
    if path is None:
        return None
    try:
        return DownsamplingCalibrator.load(path)
    except Exception as error:
        raise ModelArtifactError(
            reason=f"calibration 아티팩트를 로드하지 못했습니다({path}): {error}"
        ) from error


def _load_registry_model(settings: RegistryModelSettings) -> ResolvedModel:
    """Registry alias를 run_id로 해석한 뒤 기존 run 아티팩트 다운로드 경로를 재사용한다.

    main이 downsampling이면 그 run_id로 같은 run의 calibration 아티팩트를 함께 로드한다
    (#390 run_id 종속).
    """
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
        MlflowModelSettings(
            tracking_uri=settings.tracking_uri,
            run_id=version.run_id,
        )
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
    feature_columns_path: Path,
    categorical_columns_path: Path,
    calibration: DownsamplingCalibrator | None,
    onnx_session: object,
    workspace_owner: object | None,
) -> Reranker:
    """검증된 ONNX와 JSON 메타데이터로 Reranker를 조립한다."""
    if not feature_columns_path.is_file():
        raise ModelArtifactError(
            reason=f"Feature-column artifact does not exist: {feature_columns_path}"
        )
    if not categorical_columns_path.is_file():
        raise ModelArtifactError(
            reason=(
                "Categorical-column artifact does not exist: "
                f"{categorical_columns_path} (categorical_columns.json을 저장하는 "
                "학습 파이프라인으로 재학습이 필요합니다.)"
            )
        )

    feature_columns = _load_json_metadata(
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

    model: ProbabilityModel = OnnxProbabilityModel(
        onnx_session, feature_columns, workspace_owner=workspace_owner
    )
    if not isinstance(model, ProbabilityModel):
        raise ModelArtifactError(reason="Loaded model does not implement predict_proba.")

    categorical_categories = _load_json_metadata(
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
        calibration=calibration,
    )


def _load_json_metadata(
    path: Path,
    *,
    adapter: TypeAdapter[_Metadata],
    artifact_label: str,
    malformed_reason: str,
) -> _Metadata:
    try:
        with path.open(encoding="utf-8") as metadata_file:
            return adapter.validate_python(json.load(metadata_file))
    except ValidationError as error:
        raise ModelArtifactError(
            reason=(
                f"{artifact_label} artifact {malformed_reason} "
                f"(path: {path})"
            )
        ) from error
    except _JSON_METADATA_ERRORS as error:
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
