from __future__ import annotations

import logging
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
from src.models.calibration import CALIBRATION_PARAM_FILENAME, DownsamplingCalibrator
from src.serving.onnx_model import OnnxProbabilityModel
from src.serving.service import ProbabilityModel, Reranker

logger = logging.getLogger(__name__)

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
# calibration 모델 아티팩트(JSON w). 학습 train.py Step 9의 artifact_path="calibration"와 계약.
# 별도 등록 모델(config.registry.calibration_model_name)의 run 아래 이 경로로 로깅된다(#302).
MLFLOW_CALIBRATION_ARTIFACT_PATH: Final = f"calibration/{CALIBRATION_PARAM_FILENAME}"
# ONNX 모델 아티팩트 디렉토리(#302/#179). 학습 train.py [Step 8b]의
# log_onnx_model(artifact_path="model_onnx")와 계약 — mlflow.onnx.log_model이 이 경로 아래
# MLmodel 디렉토리를 만든다. 이 아티팩트가 있으면 서빙은 onnxruntime로 추론하고, 없으면
# (기존 champion 등 joblib만 있는 버전) joblib로 폴백한다(하위호환). "서빙 pickle 완전 제거"는
# 모든 champion이 ONNX로 재학습된 뒤 폴백을 걷어내는 후속 슬라이스에서 완성한다.
MLFLOW_ONNX_MODEL_ARTIFACT_PATH: Final = "model_onnx"


class ModelSource(StrEnum):
    """모델 아티팩트를 어디서 읽을지 지정하는 소스 종류(로컬 파일 / MLflow 런 / Registry alias)."""

    LOCAL = "local"
    MLFLOW = "mlflow"
    REGISTRY = "registry"


@dataclass(frozen=True, slots=True)
class LocalModelSettings:
    """로컬 파일에서 로드할 때 필요한 모델·피처·카테고리 아티팩트 경로 묶음.

    calibration_model_path는 optional이다(#302). 지정하면 main→calibration 체이닝을
    적용하고, None이면 calibration 없이(항등) 기존 1-모델 동작을 유지한다(하위호환).

    onnx_model_path도 optional이다(#302/#179). 지정하면 그 .onnx 파일을 onnxruntime로
    추론하고, None이면 model_path의 joblib으로 로드한다(하위호환).
    """

    model_path: Path
    feature_columns_path: Path
    categorical_columns_path: Path
    calibration_model_path: Path | None = None
    onnx_model_path: Path | None = None


@dataclass(frozen=True, slots=True)
class MlflowModelSettings:
    """MLflow 런에서 아티팩트를 내려받을 때 필요한 tracking URI와 run_id.

    calibration_run_id는 optional이다(#302). 지정하면 그 run의 calibration 아티팩트를
    로드해 체이닝하고, None이면 calibration 없이(항등) 동작한다(하위호환). 이 경로는
    수동 run 지정용이라 페어링 자동 검증 대상이 아니다.
    """

    tracking_uri: str
    run_id: str
    calibration_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class RegistryModelSettings:
    """Model Registry alias(예: models:/ctr-model@champion)로 로드할 때 필요한 설정.

    calibration_model_name은 optional이다(#302). 지정하면 그 등록 모델의 alias를
    resolve해 main과 짝이 맞는지(main_run_id tag) fail-closed로 검증한 뒤 체이닝하고,
    None이면 calibration 없이(항등) 동작한다(하위호환). calibration_alias 미지정 시
    main alias를 재사용한다.
    """

    tracking_uri: str
    model_name: str
    alias: str
    calibration_model_name: str | None = None
    calibration_alias: str | None = None


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

    # calibration 관련 env는 전부 optional이다(#302, 하위호환) — 필수값처럼
    # _required_environment_value로 읽지 않고 os.getenv(default None)로 분기한다.
    # None이면 calibration 미로드 → 항등으로 자연스럽게 빠진다.
    match source:
        case ModelSource.LOCAL:
            calibration_path = os.getenv("RERANK_CALIBRATION_MODEL_PATH")
            onnx_path = os.getenv("RERANK_ONNX_MODEL_PATH")
            return LocalModelSettings(
                model_path=Path(_required_environment_value("RERANK_MODEL_PATH")),
                feature_columns_path=Path(
                    _required_environment_value("RERANK_FEATURE_COLUMNS_PATH")
                ),
                categorical_columns_path=Path(
                    _required_environment_value("RERANK_CATEGORICAL_COLUMNS_PATH")
                ),
                calibration_model_path=Path(calibration_path) if calibration_path else None,
                onnx_model_path=Path(onnx_path) if onnx_path else None,
            )
        case ModelSource.MLFLOW:
            return MlflowModelSettings(
                tracking_uri=_required_environment_value("MLFLOW_TRACKING_URI"),
                run_id=_required_environment_value("RERANK_MLFLOW_RUN_ID"),
                calibration_run_id=os.getenv("RERANK_MLFLOW_CALIBRATION_RUN_ID"),
            )
        case ModelSource.REGISTRY:
            return RegistryModelSettings(
                tracking_uri=_required_environment_value("MLFLOW_TRACKING_URI"),
                model_name=os.getenv("RERANK_REGISTRY_MODEL_NAME", "ctr-model"),
                alias=os.getenv("RERANK_REGISTRY_ALIAS", "champion"),
                calibration_model_name=os.getenv("RERANK_REGISTRY_CALIBRATION_MODEL_NAME"),
                calibration_alias=os.getenv("RERANK_REGISTRY_CALIBRATION_ALIAS"),
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
    """로컬 경로의 아티팩트들로 Reranker를 로드한다(calibration 경로가 있으면 함께).

    onnx_model_path가 지정되면 그 .onnx를 onnxruntime로 추론하고, 없으면 model_path의
    joblib으로 로드한다(하위호환).
    """
    calibration = (
        DownsamplingCalibrator.load(settings.calibration_model_path)
        if settings.calibration_model_path is not None
        else None
    )
    onnx_session = (
        _build_onnx_session_from_path(settings.onnx_model_path)
        if settings.onnx_model_path is not None
        else None
    )
    return _load_reranker(
        model_path=settings.model_path,
        feature_columns_path=settings.feature_columns_path,
        categorical_columns_path=settings.categorical_columns_path,
        calibration=calibration,
        onnx_session=onnx_session,
    )


def load_mlflow_model(settings: MlflowModelSettings) -> Reranker:
    """MLflow 런에서 모델·피처·카테고리 아티팩트를 내려받아 Reranker를 로드한다.

    run에 model_onnx/ 아티팩트가 있으면 onnxruntime로 추론하고(joblib 다운로드·역직렬화
    생략), 없으면 joblib으로 폴백한다(기존 champion 등 하위호환).
    """
    mlflow.set_tracking_uri(settings.tracking_uri)
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
    calibration = (
        _load_calibration_from_run(settings.calibration_run_id)
        if settings.calibration_run_id is not None
        else None
    )
    onnx_session = _try_load_onnx_session_from_run(settings.run_id)
    model_path = (
        None
        if onnx_session is not None
        else Path(
            mlflow.artifacts.download_artifacts(
                artifact_uri=f"runs:/{settings.run_id}/{MLFLOW_MODEL_ARTIFACT_PATH}"
            )
        )
    )
    return _load_reranker(
        model_path=model_path,
        feature_columns_path=feature_columns_path,
        categorical_columns_path=categorical_columns_path,
        calibration=calibration,
        onnx_session=onnx_session,
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


def _try_load_onnx_session_from_run(run_id: str):
    """run에 model_onnx/ 아티팩트가 있으면 onnxruntime 세션을 만들고, 없으면 None을 반환한다.

    존재 여부는 로더가 이미 쓰는 mlflow.artifacts.download_artifacts 시임으로 확인한다
    (별도 MlflowClient API를 추가하지 않아 로더가 하나의 다운로드 경로만 갖는다). 다운로드나
    ONNX 로드가 실패하면 None을 반환해 joblib으로 폴백한다 — model_onnx/ 부재(기존 joblib-only
    champion, 정상)든 로드 불가(opset 불일치·손상)든 서빙 기동을 막지 않는 하위호환 정책이다.
    joblib 폴백은 아직 서빙에 남아있는 표현이라(pickle 완전 제거는 후속 슬라이스), ONNX가
    없거나 못 읽으면 그쪽으로 안전하게 되돌아간다. 단, 아티팩트가 있는데 못 읽는 경우는
    조용히 넘기지 않고 warning으로 남겨 관측 가능하게 한다.
    """
    try:
        local_model_dir = mlflow.artifacts.download_artifacts(
            artifact_uri=f"runs:/{run_id}/{MLFLOW_ONNX_MODEL_ARTIFACT_PATH}"
        )
    except Exception:
        # model_onnx/ 아티팩트 부재(예: joblib만 있는 기존 champion) → joblib 폴백(정상 경로).
        return None
    try:
        # `import mlflow.onnx`(별칭 없이)는 함수 스코프에서 `mlflow` 이름을 지역 변수로
        # 만들어 위의 `mlflow.artifacts` 참조를 UnboundLocalError로 깨뜨린다. 별칭 import로
        # 모듈 전역 `mlflow`를 가리지 않게 한다.
        import mlflow.onnx as mlflow_onnx
        import onnxruntime as ort

        onnx_model = mlflow_onnx.load_model(local_model_dir)
        return ort.InferenceSession(onnx_model.SerializeToString())
    except Exception:
        logger.warning(
            "model_onnx/ 아티팩트를 내려받았지만 onnxruntime 세션을 만들지 못해 joblib으로 "
            "폴백합니다(run=%s). ONNX opset/런타임 호환성 또는 아티팩트 손상을 확인하세요.",
            run_id,
            exc_info=True,
        )
        return None


def _load_calibration_from_run(run_id: str) -> DownsamplingCalibrator:
    """run의 calibration 아티팩트(JSON w)를 내려받아 DownsamplingCalibrator로 만든다.

    calibration_run_id가 명시적으로 지정된 경로에서만 호출되므로, 아티팩트가 없거나
    파싱에 실패하면 misconfiguration으로 보고 ModelArtifactError로 fail-closed한다.
    """
    try:
        path = Path(
            mlflow.artifacts.download_artifacts(
                artifact_uri=f"runs:/{run_id}/{MLFLOW_CALIBRATION_ARTIFACT_PATH}"
            )
        )
        return DownsamplingCalibrator.load(path)
    except Exception as error:
        raise ModelArtifactError(
            reason=(
                f"calibration 아티팩트를 로드하지 못했습니다(run={run_id}, "
                f"{MLFLOW_CALIBRATION_ARTIFACT_PATH}): {error}"
            )
        ) from error


def _load_registry_model(settings: RegistryModelSettings) -> ResolvedModel:
    """Registry alias를 run_id로 해석한 뒤 기존 run 아티팩트 다운로드 경로를 재사용한다.

    calibration_model_name이 지정되면 calibration alias도 resolve해 main과 짝이 맞는지
    fail-closed로 검증한다(#302 페어링 검증).
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
    calibration_run_id = _resolve_paired_calibration_run_id(settings, main_version=version)
    reranker = load_mlflow_model(
        MlflowModelSettings(
            tracking_uri=settings.tracking_uri,
            run_id=version.run_id,
            calibration_run_id=calibration_run_id,
        )
    )
    return ResolvedModel(
        reranker=reranker, run_id=version.run_id, model_version=str(version.version)
    )


def _resolve_paired_calibration_run_id(
    settings: RegistryModelSettings, *, main_version: object
) -> str | None:
    """calibration을 쓸지 판단하고, 쓴다면 main과 짝이 맞는지 fail-closed로 검증한다.

    판단 기준은 **main 모델 버전의 `sampling_rate` tag**다:

    - main이 non-downsampling(`sampling_rate >= 1.0` 또는 tag 없음, 예 #300 이전 v6)이면
      보정할 것이 없으므로 calibration을 **스킵**하고 None(항등)을 반환한다. calibration env가
      설정돼 있어도 무시한다 — main을 v6로 **롤백**했는데 `ctr-calibration-model@champion`은
      옛 downsampling을 가리키는 상황에서, 롤백이 서빙 기동을 막지 않게 하려는 것이다.
    - main이 downsampling(`sampling_rate < 1.0`)이면 calibration이 **반드시** 있어야 한다.
      calibration이 구성되지 않았으면(모델명 미설정) 보정 안 된 편향 확률을 서빙하는 것을
      막기 위해 `ModelArtifactError`로 기동을 거부한다.
    - calibration을 쓰는 경우, calibration 버전의 `main_run_id` tag가 지금 resolve된 main
      run_id와 다르면(main@champion=v8, calibration@champion=v3처럼 각자 다른 시점에 승격돼
      어긋난 조합) `ModelArtifactError`로 기동을 거부한다.

    이 판단·검증은 Registry 경로 전용이다. MLflow 직접 run 지정(`MlflowModelSettings`)은
    실험·수동 경로라 alias 자동 승격처럼 몰래 어긋날 리스크가 없어 대상이 아니다.
    """
    main_run_id = main_version.run_id
    main_tags = getattr(main_version, "tags", None) or {}
    main_sampling_rate = float(main_tags.get("sampling_rate", 1.0))
    if main_sampling_rate >= 1.0:
        # non-downsampling main → 보정 불필요. calibration env가 있어도(롤백 등) 항등.
        return None
    if settings.calibration_model_name is None:
        raise ModelArtifactError(
            reason=(
                f"main 모델이 downsampling(sampling_rate={main_sampling_rate})인데 서빙에 "
                "calibration이 구성되지 않았습니다(RERANK_REGISTRY_CALIBRATION_MODEL_NAME 미설정). "
                "보정 안 된 편향 확률이 서빙에 나가는 것을 막기 위해 기동을 거부합니다 — "
                "calibration 모델을 배선하거나 non-downsampling 모델을 champion으로 두세요."
            )
        )
    calibration_alias = settings.calibration_alias or settings.alias
    try:
        cal_version = MlflowClient().get_model_version_by_alias(
            settings.calibration_model_name, calibration_alias
        )
    except Exception as error:
        raise ModelArtifactError(
            reason=(
                f"calibration alias models:/{settings.calibration_model_name}"
                f"@{calibration_alias}를 resolve하지 못했습니다: {error}"
            )
        ) from error
    paired_main_run_id = (cal_version.tags or {}).get("main_run_id")
    if paired_main_run_id != main_run_id:
        raise ModelArtifactError(
            reason=(
                "calibration 모델과 main 모델의 짝이 맞지 않습니다(#302 페어링 검증). "
                f"main run_id={main_run_id}, calibration이 가리키는 main_run_id="
                f"{paired_main_run_id}. 서로 다른 학습에서 나온 조합이라 서빙을 거부합니다 — "
                "main과 calibration alias를 같은 학습 버전으로 맞춰 승격하세요."
            )
        )
    return cal_version.run_id


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
    model_path: Path | None,
    feature_columns_path: Path,
    categorical_columns_path: Path,
    calibration: DownsamplingCalibrator | None = None,
    onnx_session: object | None = None,
) -> Reranker:
    """세 아티팩트의 존재·형식·상호 계약(카테고리 컬럼 ⊆ 피처)을 검증하고 Reranker를 조립한다.

    calibration이 주어지면 Reranker가 main 예측 후 calibration을 체이닝한다. None이면
    calibration 없이(항등) 동작한다(하위호환).

    onnx_session이 주어지면 joblib 대신 ONNX 어댑터를 모델로 쓴다(model_path는 무시). 어댑터는
    feature_columns 순서로 입력을 인코딩하므로, 아래에서 feature_columns를 먼저 로드·검증한 뒤
    조립한다. onnx_session이 None이면 model_path의 joblib을 로드한다(하위호환).
    """
    if onnx_session is None and (model_path is None or not model_path.is_file()):
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

    if onnx_session is not None:
        model: ProbabilityModel = OnnxProbabilityModel(onnx_session, feature_columns)
    else:
        model = joblib.load(model_path)
    if not isinstance(model, ProbabilityModel):
        raise ModelArtifactError(reason="Loaded model does not implement predict_proba.")

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
        calibration=calibration,
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
