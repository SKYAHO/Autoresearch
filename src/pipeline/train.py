#!/usr/bin/env python3
"""LightGBM 또는 NumPy MLP CTR 모델 학습 스크립트.

[파이프라인] 학습 데이터셋(training_dataset.csv) → 모델 학습 구간을 담당한다.
train/val/test 3-way 분할, negative downsampling, config가 선택한 모델 학습, 아티팩트
저장(joblib·ONNX·feature/categorical 목록·calibration), MLflow run 로깅,
registered model 버전 생성이 이 모듈의 책임이다.

[기능] config.yaml 기반 학습 실행과 함께, 학습 시작 전 라벨 분포를 검증해
단일 클래스(양성 0건/음성 0건)면 원인이 드러나는 메시지로 즉시 중단한다(#421).
`extra_features`를 주면 prod 모델 계약(`MODEL_FEATURE_COLUMNS`)을 건드리지 않고
그 뒤에 실험 피처를 덧붙여 학습한다(#405). 데이터셋에 없는 컬럼이면 정규 경로
(Feast ODFV, #399) 안내와 함께 중단하며, 실험 모델은 registry tag로 표시돼
승격 게이트가 거부한다. `experiment`를 주면 MLflow experiment·registry 이름이
prod와 분리되고 트래킹 URI 기본값이 로컬 파일 스토어가 된다(#406) — 좌표 결정은
`src/tracking/namespace.py`가 소유한다. 운영 경로에서 `MLFLOW_TRACKING_URI`가
비어 있으면 조용히 localhost로 넘어가지 않고 즉시 중단한다.
`scale_pos_weight="auto"` 계산의 0 나눗셈도 같은 이유로 fail-closed다. 결과는
`TrainingOutcome`으로 반환하며, `defer_registration=True`면 registered model
버전 생성을 보류하고 `PendingRegistration`을 넘겨 호출자가 평가 통과 뒤에
`register_pending_model()`로 등록하게 한다.

검증된 dataset sidecar가 있으면 snapshot·split manifest와 실제 CSV를 같은 MLflow run의
`reproducibility/` artifact로 보관하고, split/model/sampler seed를 분리해 기록한다(#423).
`require_snapshot=True` 호출은 sidecar가 없거나 현재 CSV와 불일치할 때 모델 fit 전에
실패한다.

승격 evidence 옵션(plan receipt + evidence root)을 주면 held-out test split에서
`HELD_OUT_METRIC_NAMES`(ROC-AUC·PR-AUC·Log Loss)를 계산해 지표마다 write-once
`HeldOutMetricEvidence`를 게시한다(#493). 주 지표 ROC-AUC receipt만 기존
`reproducibility/metrics/` artifact 경로로도 남겨 비교 계약을 유지한다.

[비책임] 데이터셋 조립(src/pipeline/build_training_dataset.py), held-out test set
채점(src/pipeline/evaluate.py), champion 승격 게이트(src/tracking/promote.py),
서빙 로드(src/serving/model_loader.py)는 이 모듈이 다루지 않는다.
"""

import math
import os
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Optional

import numpy as np
import yaml
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

import mlflow  # noqa: E402
import mlflow.onnx  # noqa: E402

from src.models.lgbm_model import LGBMModel  # noqa: E402
from src.models.mlp_model import MLPModel  # noqa: E402
from src.models.downsampling import downsample_negatives  # noqa: E402
from src.models.calibration import (  # noqa: E402
    CALIBRATION_PARAM_FILENAME,
    DownsamplingCalibrator,
)
from src.features.model_contract import (  # noqa: E402
    CATEGORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
    resolve_experiment_feature_columns,
)
from src.utils.model_utils import (  # noqa: E402
    convert_lgbm_to_onnx,
    save_categorical_columns,
    save_feature_columns,
)
from src.tracking.client import get_or_create_experiment, set_tracking_uri  # noqa: E402
from src.tracking.namespace import (  # noqa: E402
    derive_experiment_name,
    resolve_tracking_namespace,
)
from src.tracking.logger import (  # noqa: E402
    log_artifact,
    log_artifacts,
    log_dataset,
    log_metrics,
    log_parameters,
)
from src.tracking.model_package import (  # noqa: E402
    ModelPackageManifest,
    load_manifest,
    save_manifest,
    verify_model_package,
)
from src.tracking.registry import register_model  # noqa: E402
from src.pipeline.evaluate import (  # noqa: E402
    HELD_OUT_METRIC_NAMES,
    evaluate_held_out_metrics,
)
from src.pipeline.promotion_evidence import (  # noqa: E402
    DEFAULT_HELD_OUT_METRIC_NAME,
    ExperimentPlanReceipt,
    HeldOutMetricEvidence,
    HeldOutMetricReceipt,
    PromotionEvidenceStore,
    PromotionEvidenceValidationError,
)
from src.pipeline.training_provenance import (  # noqa: E402
    ProvenanceValidationError,
    TrainingSnapshotManifest,
    build_split_manifest,
    load_training_snapshot_manifest,
    resolve_training_seeds,
    sha256_file,
    snapshot_manifest_path,
    split_manifest_path,
    write_manifest_atomic,
)
from src.pipeline import training_snapshot_store  # noqa: E402

LABEL_COLUMN = "clicked"


@dataclass(frozen=True)
class PendingRegistration:
    """평가 통과 후 만들 registered model 버전 정보(#421).

    학습이 끝난 run의 model URI·모델 이름·버전 태그를 담아둔다. 등록 시점만
    미룰 뿐 내용은 학습 시점에 확정된다 — 평가가 실패하면 이 값은 버려지고
    registry에는 아무 버전도 생기지 않는다.
    """

    model_uri: str
    model_name: str
    tags: dict[str, str]


@dataclass(frozen=True)
class TrainingOutcome:
    """`main()`의 반환 계약(#421).

    Attributes:
        sampling_rate: 실현 negative downsampling 비율(#300). evaluate가
            오프라인 지표를 원분포 기준으로 재는 데 쓴다.
        run_id: 학습 MLflow run id.
        registered_version: 이 호출에서 만든 registered model 버전
            (등록을 미뤘거나 등록에 실패했으면 None).
        pending_registration: 등록을 미뤘을 때 호출자가 평가 통과 뒤
            `register_pending_model()`에 넘길 정보(미루지 않았으면 None).
        held_out_metric_receipt: 주 지표(ROC-AUC) held-out evidence receipt.
            `held_out_metric_receipts`의 부분집합이며, 기존 소비 계약
            (training_comparison.py)이 이 하나만 읽는다.
        held_out_metric_receipts: 이 run이 게시한 held-out evidence receipt 전부
            (`HELD_OUT_METRIC_NAMES` 순서, #493 D3). 승격 evidence를 끄고 학습하면
            빈 튜플이다.
    """

    sampling_rate: float
    run_id: str
    registered_version: Optional[str] = None
    pending_registration: Optional[PendingRegistration] = None
    # 시드 스윕이 시드별로 모아 평균·편차를 내는 지표(#407). 여기서 돌려주지 않으면
    # 반복 실행이 MLflow run을 다시 조회해야 한다.
    val_roc_auc: float = float("nan")
    held_out_metric_receipt: HeldOutMetricReceipt | None = None
    held_out_metric_receipts: tuple[HeldOutMetricReceipt, ...] = ()


def require_binary_labels(labels: pd.Series, *, stage: str) -> tuple[int, int]:
    """라벨에 양성·음성이 모두 있는지 검증하고 (양성, 음성) 개수를 반환한다(#421).

    단일 클래스면 학습 자체는 돌아가지만 지표가 정의되지 않는다. 설치된
    scikit-learn의 `roc_auc_score`는 이 경우 예외 대신 경고 + `np.nan`을
    돌려주므로 학습·등록이 통과한 것처럼 흘러가고, 파이프라인 마지막
    `log_loss`에서야 "y_true contains only one label"로 터진다. 그 시점에는
    원인이 데이터라는 사실이 메시지에 드러나지 않으므로, 여기서 행 수와
    양성/음성 개수를 담아 먼저 막는다.

    Args:
        labels: 라벨(0/1) Series.
        stage: 에러 메시지에 넣을 검증 대상 이름(예: "학습 데이터셋", "Val split").

    Returns:
        (양성 개수, 음성 개수).

    Raises:
        ValueError: 양성 또는 음성이 0건이면.
    """
    positive = int((labels == 1).sum())
    negative = int((labels == 0).sum())
    if positive == 0 or negative == 0:
        raise ValueError(
            f"{stage} 라벨이 단일 클래스입니다 — rows={len(labels)}, "
            f"positive({LABEL_COLUMN}=1)={positive}, "
            f"negative({LABEL_COLUMN}=0)={negative}. "
            "양성·음성이 모두 있어야 학습과 평가 지표(ROC-AUC/LogLoss)가 성립합니다. "
            "action log 수집 결과와 클릭 임계값(click threshold) 설정을 확인하세요."
        )
    return positive, negative


def compute_auto_scale_pos_weight(labels: pd.Series) -> float:
    """`scale_pos_weight="auto"`의 음성/양성 비율을 계산한다(#421).

    양성이 0건이면 0으로 나누며 `RuntimeWarning: divide by zero` 뒤 `inf`가
    조용히 흘러가므로, 계산 대신 즉시 중단한다. 정상 경로에서는
    `require_binary_labels()`가 먼저 막으므로 여기까지 오지 않는다 — 마지막 보루다.

    Args:
        labels: train split 라벨(0/1) Series.

    Returns:
        음성/양성 비율.

    Raises:
        ValueError: 양성이 0건이면.
    """
    negative = int((labels == 0).sum())
    positive = int((labels == 1).sum())
    if positive == 0:
        raise ValueError(
            "scale_pos_weight='auto'인데 train split의 양성"
            f"({LABEL_COLUMN}=1)이 0건입니다 — rows={len(labels)}, "
            f"negative={negative}. 음성/양성 비율을 0으로 나눌 수 없습니다."
        )
    return negative / positive


def require_experiment_features_in_dataset(
    dataset: pd.DataFrame, extra_features: Sequence[str]
) -> tuple[str, ...]:
    """실험 피처가 학습 데이터셋에 쓸 수 있는 상태인지 확인하고 최종 입력 순서를 만든다(#405).

    이 경로는 데이터셋에 **이미 있는** 컬럼을 모델 입력으로 승격시킬 뿐 컬럼을
    만들어내지 않는다. 없는 컬럼을 즉석에서 계산해 채우면 서빙 시점에는 Feast
    online store에 그 피처가 없어 학습/서빙 스큐가 그대로 재현된다
    (`src/features/feature_builder.py`가 막으려는 바로 그것).

    그래서 fail-closed로 막되, 팀원이 정규 경로를 바로 찾아가도록 안내를 함께 낸다.

    검증을 **부수효과 이전에** 모두 끝내는 것이 이 함수의 두 번째 목적이다. 계약
    거부 조건(빈 목록·중복·prod 이름 충돌)을 나중 단계에서 확인하면, 그 사이에 MLflow
    run 생성과 held-out test set 덮어쓰기가 이미 끝나 공유 파일이 갈아엎히고 FAILED
    run만 남는다. 그래서 여기서 `resolve_experiment_feature_columns()`까지 함께 부른다.

    Args:
        dataset: 학습 데이터셋.
        extra_features: `--extra-features`로 지정한 실험 피처.

    Returns:
        prod 계약 + 실험 피처 순서의 최종 모델 입력 컬럼.

    Raises:
        ValueError: 지정한 컬럼이 데이터셋에 없거나 수치형이 아니면.
        FeatureContractError: 계약 계층의 거부 조건(빈 목록·중복·prod 충돌)에 걸리면.
    """
    # 계약 거부가 먼저다 — 데이터셋을 훑기 전에 목록 자체가 유효해야 한다.
    resolved = resolve_experiment_feature_columns(extra_features)

    missing = [name for name in extra_features if name not in dataset.columns]
    if missing:
        raise ValueError(
            f"실험 피처 {missing}가 학습 데이터셋에 없습니다. "
            "prod 계약(MODEL_FEATURE_COLUMNS)에도 없는 컬럼입니다.\n"
            f"데이터셋 컬럼 {len(dataset.columns)}개: {sorted(dataset.columns)}\n\n"
            "--extra-features는 데이터셋에 이미 있는 컬럼만 모델 입력으로 승격시킵니다. "
            "컬럼을 새로 만들려면 Feast ODFV 정의 → feast apply → "
            "FeatureService(ctr_training_v1) 갱신이 필요합니다. "
            "정규 경로는 #399를 참고하세요."
        )

    # 범주형 실험 피처는 이번 범위가 아니다. 그대로 두면 LightGBM fit()에서야
    # 터지는데, 그 시점엔 test set 덮어쓰기와 run 생성이 이미 끝난 뒤다.
    non_numeric = [
        name
        for name in extra_features
        if not pd.api.types.is_numeric_dtype(dataset[name])
    ]
    if non_numeric:
        raise ValueError(
            f"실험 피처 {non_numeric}가 수치형이 아닙니다"
            f"(dtype: {[str(dataset[n].dtype) for n in non_numeric]}).\n"
            "--extra-features는 수치형 컬럼만 지원합니다 — 범주형은 카테고리 코드 매핑이 "
            "서빙 로더와 얽혀 있어 별도 작업이 필요합니다. "
            "범주형 실험 피처가 필요하면 이슈로 올려 주세요."
        )

    return resolved


def register_pending_model(pending: PendingRegistration) -> str:
    """보류해둔 registered model 버전을 만든다(#421).

    실패를 삼키지 않고 그대로 전파한다. 삼킬지 말지는 호출 지점의 사정이라
    이 함수가 정하지 않는다 — run이 열려 있는 학습 중 등록(`main()`)은 이미
    끝난 run을 FAILED로 만들지 않으려고 호출부에서 감싸지만, 평가 통과 뒤
    등록(`run-pipeline`)은 run이 이미 닫혀 있어 그 근거가 성립하지 않는다.
    거기서 삼키면 "평가까지 통과했는데 신규 후보가 없는 날"이 exit 0으로
    지나가고, 후속 promote-model이 어제 버전(=이미 champion)을 집어
    ALREADY_CHAMPION으로 조용히 끝난다.

    Args:
        pending: 학습 시점에 확정된 등록 정보.

    Returns:
        생성된 모델 버전 문자열.

    Raises:
        Exception: registry 등록이 실패하면 그대로 전파한다.
    """
    version = register_model(pending.model_uri, pending.model_name, tags=pending.tags)
    print(f"  [OK] {pending.model_name} v{version} 등록 완료")
    return version


def get_project_root():
    """프로젝트 루트 경로 반환."""
    current = os.path.dirname(os.path.abspath(__file__))
    while current != "/":
        if os.path.exists(os.path.join(current, "src")):
            return current
        current = os.path.dirname(current)
    raise RuntimeError("프로젝트 루트를 찾을 수 없습니다")


def load_config(config_path):
    """config.yaml 로드."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def resolve_model_type(config: dict) -> str:
    """학습 config의 모델 종류를 정규화한다.

    ``model_type``가 없는 기존 config는 LightGBM으로 해석한다. 이 하위 호환
    기본값을 두면 운영 config와 기존 테스트용 config가 새 challenger 설정을
    알지 못해도 기존 모델을 계속 선택한다.
    """
    model_config = config.get("model", {})
    raw_type = model_config.get("model_type", model_config.get("type", "LightGBM"))
    normalized = str(raw_type).strip().lower()
    if normalized in {"lightgbm", "lgbm"}:
        return "LightGBM"
    if normalized in {"mlp", "numpy_mlp"}:
        return "MLP"
    raise ValueError(
        f"지원하지 않는 model_type입니다: {raw_type!r}. "
        "'LightGBM' 또는 'MLP'를 사용하세요."
    )


def collect_categorical_categories(
    X_train: pd.DataFrame, X_val: pd.DataFrame, categorical_columns: list
) -> dict:
    """
    Categorical 컬럼을 train/val union 카테고리로 캐스팅하고 카테고리 목록을 반환.

    반환된 dict는 categorical_columns.json 아티팩트로 저장되어 서빙이 학습과
    동일한 category 코드 매핑을 재현하는 데 사용된다 (src/serving/model_loader.py).
    """
    categories_by_column: dict = {}
    for col in categorical_columns:
        if col not in X_train.columns:
            continue
        categories = pd.api.types.union_categoricals(
            [X_train[col].astype("category"), X_val[col].astype("category")]
        ).categories
        X_train[col] = pd.Categorical(X_train[col], categories=categories)
        X_val[col] = pd.Categorical(X_val[col], categories=categories)
        categories_by_column[col] = categories.tolist()
    return categories_by_column


def require_snapshot_coverage(*, spine_usable_days: int | None, min_days: int) -> None:
    """재사용 스냅샷이 커버리지 하한을 만족하는지 검증한다(#530).

    ``require_spine_coverage``(#464)와 같은 술어를 manifest 값으로 재현한다. 재조립을
    하지 않는 경로라 실측 coverage 객체가 없으므로 sidecar가 실은 값을 쓴다.
    ``None``은 이 필드가 없던 시절 manifest라는 뜻이며, 게이트가 켜져 있으면 검증할
    근거가 없으므로 조용히 통과시키지 않는다.

    Args:
        spine_usable_days: manifest에 기록된 spine 사용 가능 일수(없으면 ``None``).
        min_days: 요구하는 최소 일수. ``0`` 이하면 게이트를 명시적으로 우회한다.

    Raises:
        ProvenanceValidationError: 게이트가 켜져 있는데 근거(``spine_usable_days``)가
            없거나, 기록된 일수가 최소치에 못 미치면.
    """
    if min_days <= 0:
        return
    if spine_usable_days is None:
        raise ProvenanceValidationError(
            "스냅샷에 spine 커버리지 기록이 없어 커버리지 게이트를 검증할 수 없습니다 "
            f"(최소 {min_days}일 필요). 데이터셋을 다시 조립하거나 "
            "min_coverage_days=0으로 명시적으로 우회하십시오."
        )
    if spine_usable_days < min_days:
        raise ProvenanceValidationError(
            f"스냅샷의 사용 가능한 날이 {spine_usable_days}일로 최소 {min_days}일에 "
            "미달합니다. 기간을 넓혀 재조립하거나 min_coverage_days=0으로 우회하십시오."
        )


def _log_reproducibility_artifacts(
    *,
    dataset_path: Path,
    snapshot_path: Path,
    split_path: Path,
) -> None:
    """verified training 입력을 canonical MLflow artifact 이름으로 기록한다."""
    with TemporaryDirectory(prefix="training_reproducibility_") as temporary_dir:
        temporary_root = Path(temporary_dir)
        canonical_dataset = temporary_root / "training_dataset.csv"
        canonical_snapshot = temporary_root / "snapshot_manifest.json"
        canonical_split = temporary_root / "split_manifest.json"
        shutil.copyfile(dataset_path, canonical_dataset)
        shutil.copyfile(snapshot_path, canonical_snapshot)
        shutil.copyfile(split_path, canonical_split)

        log_artifact(
            local_path=str(canonical_dataset),
            artifact_path="reproducibility/snapshot",
        )
        log_artifact(
            local_path=str(canonical_snapshot),
            artifact_path="reproducibility/snapshot",
        )
        log_artifact(
            local_path=str(canonical_split),
            artifact_path="reproducibility/split",
        )


def _load_experiment_plan_receipt(path: Path) -> ExperimentPlanReceipt:
    """로컬 전달 receipt를 strict 계약으로 읽되 raw body를 오류에 싣지 않는다."""
    try:
        return ExperimentPlanReceipt.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PromotionEvidenceValidationError(
            "experiment plan receipt를 읽거나 검증할 수 없습니다"
        ) from None


def _log_held_out_metric_receipt(receipt: HeldOutMetricReceipt) -> None:
    """metric receipt를 MLflow 좌표 탐색용 복사본으로만 기록한다."""
    with TemporaryDirectory(prefix="held_out_metric_receipt_") as temporary_dir:
        receipt_path = Path(temporary_dir) / "held_out_metric_receipt.json"
        write_manifest_atomic(receipt, receipt_path)
        log_artifact(
            local_path=str(receipt_path),
            artifact_path="reproducibility/metrics",
        )


def _log_lgbm_deployment_package(
    *,
    model: LGBMModel,
    feature_columns_path: str,
    categorical_columns_path: str,
    feature_count: int,
    sampling_rate: float,
) -> None:
    """LightGBM 전용 ONNX 배포 패키지를 검증하고 MLflow에 기록한다."""
    # 이 패키지는 기존 LightGBM 서빙 계약이 소유한다. MLP는 ONNX 변환기가
    # 지원하는 입력 모델이 아니므로 이 함수 자체를 호출하지 않는다.
    with TemporaryDirectory(prefix="ctr-model-package-") as package_root_text:
        package_root = Path(package_root_text)
        onnx_dir = package_root / "model_onnx"
        package_features_dir = package_root / "features"
        package_features_dir.mkdir(parents=True)
        package_feature_columns = package_features_dir / "feature_columns.json"
        package_categorical_columns = package_features_dir / "categorical_columns.json"
        shutil.copy2(feature_columns_path, package_feature_columns)
        shutil.copy2(categorical_columns_path, package_categorical_columns)
        onnx_model = convert_lgbm_to_onnx(model, n_features=feature_count)
        mlflow.onnx.save_model(onnx_model, path=str(onnx_dir))

        if not (0.0 < sampling_rate <= 1.0):
            raise ValueError("realized sampling_rate는 (0, 1] 범위여야 합니다")
        calibration_path: Path | None
        if sampling_rate < 1.0:
            calibration_path = package_root / CALIBRATION_PARAM_FILENAME
            DownsamplingCalibrator(sampling_rate).save(calibration_path)
        else:
            calibration_path = None

        manifest = ModelPackageManifest.build(
            sampling_rate=sampling_rate,
            model_onnx=onnx_dir,
            feature_columns=package_feature_columns,
            categorical_columns=package_categorical_columns,
            calibration=calibration_path,
        )
        manifest_path = package_root / "manifest.json"
        save_manifest(manifest, manifest_path)
        loaded_manifest = load_manifest(manifest_path)
        verify_model_package(
            loaded_manifest,
            model_onnx=onnx_dir,
            feature_columns=package_feature_columns,
            categorical_columns=package_categorical_columns,
            calibration=calibration_path,
        )

        log_artifacts(str(onnx_dir), artifact_path="model_onnx")
        log_artifact(local_path=str(package_feature_columns), artifact_path="features")
        log_artifact(local_path=str(package_categorical_columns), artifact_path="features")
        if calibration_path is not None:
            log_artifact(local_path=str(calibration_path), artifact_path="calibration")
        log_artifact(local_path=str(manifest_path), artifact_path="manifest")
        print("  [OK] ONNX + manifest 배포 패키지 검증·로깅 완료")


def main(
    config_path: str = None,
    data_path: str = None,
    model_output: str = None,
    test_set_output: str = None,
    feature_columns_output: str = None,
    categorical_columns_output: str = None,
    test_size: float = None,
    val_size: float = None,
    random_state: int = None,
    split_seed: int = None,
    model_seed: int = None,
    sampler_seed: int = None,
    extra_params: dict = None,
    defer_registration: bool = False,
    extra_features: Optional[Sequence[str]] = None,
    experiment: Optional[str] = None,
    require_snapshot: bool = False,
    experiment_plan_receipt_path: str | None = None,
    promotion_evidence_root: str | None = None,
    promotion_evidence_store: PromotionEvidenceStore | None = None,
    dataset_uri: str | None = None,
    min_coverage_days: int | None = None,
) -> TrainingOutcome:
    """config가 선택한 CTR 모델을 학습하고 MLflow에 기록한다.

    ``dataset_uri``를 주면 조립을 다시 돌리지 않고 게시된 스냅샷(#530)을 내려받아
    바로 학습 입력으로 쓴다 — ``data_path``와는 함께 지정할 수 없다. 다운로드한
    CSV는 임시 디렉터리에 두며, 학습이 끝나든 예외로 중단되든 반드시 정리한다.

    Args:
        defer_registration: True면 registered model 버전을 만들지 않고
            `TrainingOutcome.pending_registration`으로 넘긴다(#421). run 로깅
            (파라미터·메트릭·아티팩트)은 그대로 수행한다. run-pipeline이 평가
            통과 뒤에 등록하도록 이 경로를 쓴다.
        dataset_uri: 게시된 학습 스냅샷의 by-hash URI(#530). 주어지면 재조립 없이
            해당 스냅샷을 내려받아 학습한다.
        min_coverage_days: 재사용 스냅샷이 만족해야 할 최소 spine 사용 가능
            일수(#530). ``0`` 이하면 게이트를 명시적으로 우회한다.
            ``dataset_uri``를 줄 때는 **필수**이며, ``None``이면 거부한다(#537) —
            기본값으로 게이트가 조용히 꺼지는 경로를 남기지 않기 위해서다.
            ``dataset_uri`` 없이 학습할 때는 쓰이지 않는다.

    Returns:
        학습 결과(TrainingOutcome).
    """
    snapshot_download_dir: TemporaryDirectory | None = None
    if dataset_uri is not None:
        if data_path is not None:
            raise ValueError(
                "dataset_uri와 data_path는 함께 지정할 수 없습니다 — "
                "어느 쪽이 학습 입력인지 결정할 수 없습니다"
            )
        # 기본값을 두면 인자를 빠뜨린 호출에서 커버리지 게이트가 **조용히** 꺼진다(#537).
        # 지금은 두 CLI 호출부가 모두 명시적으로 넘겨 도달하지 않지만, 그 근거는
        # "설계가 안전해서"가 아니라 "아무도 그 조합을 쓰지 않아서"였다. 우회는
        # 0을 명시하는 것으로만 가능하게 해, 우회 여부가 항상 호출부에 드러나게 한다.
        if min_coverage_days is None:
            raise ValueError(
                "dataset_uri로 재사용 학습할 때는 min_coverage_days를 명시해야 합니다 — "
                "기본값으로 커버리지 게이트가 조용히 꺼지지 않게 하기 위해서입니다. "
                "의도적으로 우회하려면 0을 명시하십시오."
            )
        snapshot_download_dir = TemporaryDirectory(prefix="training_snapshot_")
        data_path = str(
            training_snapshot_store.download_snapshot(
                dataset_uri=dataset_uri,
                destination_dir=Path(snapshot_download_dir.name),
            )
        )
    try:
        return _train_from_resolved_dataset(
            config_path=config_path,
            data_path=data_path,
            model_output=model_output,
            test_set_output=test_set_output,
            feature_columns_output=feature_columns_output,
            categorical_columns_output=categorical_columns_output,
            test_size=test_size,
            val_size=val_size,
            random_state=random_state,
            split_seed=split_seed,
            model_seed=model_seed,
            sampler_seed=sampler_seed,
            extra_params=extra_params,
            defer_registration=defer_registration,
            extra_features=extra_features,
            experiment=experiment,
            require_snapshot=require_snapshot,
            experiment_plan_receipt_path=experiment_plan_receipt_path,
            promotion_evidence_root=promotion_evidence_root,
            promotion_evidence_store=promotion_evidence_store,
            dataset_uri=dataset_uri,
            min_coverage_days=min_coverage_days,
        )
    finally:
        if snapshot_download_dir is not None:
            snapshot_download_dir.cleanup()


def _train_from_resolved_dataset(
    *,
    config_path: str = None,
    data_path: str = None,
    model_output: str = None,
    test_set_output: str = None,
    feature_columns_output: str = None,
    categorical_columns_output: str = None,
    test_size: float = None,
    val_size: float = None,
    random_state: int = None,
    split_seed: int = None,
    model_seed: int = None,
    sampler_seed: int = None,
    extra_params: dict = None,
    defer_registration: bool = False,
    extra_features: Optional[Sequence[str]] = None,
    experiment: Optional[str] = None,
    require_snapshot: bool = False,
    experiment_plan_receipt_path: str | None = None,
    promotion_evidence_root: str | None = None,
    promotion_evidence_store: PromotionEvidenceStore | None = None,
    dataset_uri: str | None = None,
    min_coverage_days: int | None = None,
) -> TrainingOutcome:
    """``main()``의 실제 학습 본문 — ``data_path``가 이미 확정된 뒤 호출된다.

    ``dataset_uri``는 다운로드 자체가 아니라(그건 ``main()``이 끝냈다) 재사용
    스냅샷 커버리지 게이트·lineage 파라미터 병합 여부를 가르는 표지로만 쓰인다.
    """
    promotion_evidence_enabled = experiment_plan_receipt_path is not None
    if promotion_evidence_enabled != (promotion_evidence_root is not None):
        raise ValueError(
            "experiment_plan_receipt_path와 promotion_evidence_root는 함께 지정해야 합니다"
        )
    if promotion_evidence_store is not None and not promotion_evidence_enabled:
        raise ValueError(
            "promotion_evidence_store는 promotion evidence 옵션과 함께만 지정할 수 있습니다"
        )
    if promotion_evidence_enabled and not require_snapshot:
        raise ValueError("promotion evidence를 사용하려면 require_snapshot=True가 필요합니다")

    experiment_plan_receipt: ExperimentPlanReceipt | None = None
    evidence_store: PromotionEvidenceStore | None = None
    if promotion_evidence_enabled:
        experiment_plan_receipt = _load_experiment_plan_receipt(
            Path(experiment_plan_receipt_path)
        )
        evidence_store = promotion_evidence_store or PromotionEvidenceStore(
            promotion_evidence_root
        )
        evidence_store.verify_plan_receipt(experiment_plan_receipt)

    project_root = get_project_root()
    if config_path is None:
        config_path = os.path.join(project_root, "src", "pipeline", "config.yaml")
    elif not os.path.isabs(config_path):
        config_path = os.path.join(project_root, config_path)
    config = load_config(config_path)
    model_config = config.get("model", {})
    model_type = resolve_model_type(config)
    is_mlp = model_type == "MLP"
    mlp_config = model_config.get("mlp", {}) if is_mlp else {}

    if data_path is None:
        data_path = os.path.join(project_root, config["data"]["path"])
    elif not os.path.isabs(data_path):
        data_path = os.path.join(project_root, data_path)

    snapshot_manifest: TrainingSnapshotManifest | None = None
    snapshot_path = snapshot_manifest_path(Path(data_path))
    if snapshot_path.is_file():
        snapshot_manifest = load_training_snapshot_manifest(Path(data_path))
    elif require_snapshot:
        raise ProvenanceValidationError(
            f"verified training snapshot이 필요하지만 sidecar가 없습니다: {snapshot_path}"
        )

    if dataset_uri is not None:
        # fail-closed: 오늘은 download_snapshot이 sidecar 부재 시 반드시 raise하므로
        # (load_training_snapshot_manifest 호출) snapshot_manifest가 None일 수
        # 없지만, 그 불변식은 이 함수가 아니라 download_snapshot 쪽에 있다.
        # require_snapshot도 train-model 기본값이 False라 여기서 강제되지 않는다.
        # 그 함수가 나중에 리팩터링되면 커버리지 게이트가 조용히 꺼질 수 있으므로,
        # manifest 자체를 여기서 다시 검증해 한 단계 아래에서도 fail-closed로 막는다.
        if snapshot_manifest is None:
            raise ProvenanceValidationError(
                "재사용 스냅샷의 sidecar를 읽을 수 없어 커버리지 게이트를 검증할 수 "
                f"없습니다: {snapshot_path}. 스냅샷을 다시 내려받거나 재조립하십시오."
            )
        require_snapshot_coverage(
            spine_usable_days=snapshot_manifest.spine_usable_days,
            min_days=min_coverage_days,
        )
        # 재조립을 하지 않은 실행도 "어떤 조건으로 만든 데이터인가"를 run 파라미터만
        # 보고 판별할 수 있어야 한다(#530 §7.2). 조립 반환값이 없는 대신 sidecar가
        # 같은 값을 전부 싣고 있으므로 거기서 채운다. 호출부(cli)가 아니라 여기서
        # 채우는 이유는, manifest를 실제로 읽는 주체가 이 함수뿐이기 때문이다.
        reuse_params = {
            "events_start_date": snapshot_manifest.events_start_date.isoformat(),
            "events_end_date": snapshot_manifest.events_end_date.isoformat(),
            "feature_service": snapshot_manifest.feature_service,
            "feast_registry_path": snapshot_manifest.registry_uri,
            "feast_registry_generation": snapshot_manifest.registry_generation,
            # 게이트 우회 여부는 spine_usable_days만으로는 복원할 수 없다 — #464가
            # 막던 바로 그 비대칭이 재사용 경로에도 그대로 있으면 안 된다. 조립 경로의
            # SpineCoverage.as_lineage_params와 같은 인코딩(문자열, on/off)을 그대로 쓴다.
            "spine_coverage_min_days_applied": str(min_coverage_days),
            "spine_coverage_guard": "off" if min_coverage_days <= 0 else "on",
            # train-model 단독 실행도 어떤 스냅샷으로 학습했는지 run 파라미터만 보고
            # 알 수 있어야 한다 — run-pipeline만 이 값을 남기면 두 소비 경로의
            # provenance가 어긋난다(#530).
            "training_snapshot_uri": dataset_uri,
        }
        if snapshot_manifest.spine_usable_days is not None:
            reuse_params["spine_usable_days"] = str(snapshot_manifest.spine_usable_days)
        extra_params = {**(extra_params or {}), **reuse_params}

    explicit_seed_values = (split_seed, model_seed, sampler_seed)
    effective_seeds = resolve_training_seeds(
        random_state=random_state,
        split_seed=split_seed,
        model_seed=model_seed,
        sampler_seed=sampler_seed,
        config_seed=int(config["data"]["random_state"]),
    )
    legacy_seed_path = not any(seed is not None for seed in explicit_seed_values)
    if legacy_seed_path:
        # 기존 random_state parameter와 모델 동작을 유지한다.
        random_state = effective_seeds.model_seed

    # MLflow 초기화 — 운영/실험 좌표를 한 번에 정한다(#406). tracking URI만 바꾸고
    # 등록 이름을 그대로 두면 로컬 스토어에 prod와 동명인 모델이 쌓인다.
    # --extra-features만 주고 --experiment를 빼도 실험 좌표를 쓴다(#406 리뷰 1).
    # 그러지 않으면 prod 계약에 없는 입력으로 학습한 산출물이 prod registry 이름
    # 아래 쌓여, 이 분리가 없애려던 상황이 그대로 재현된다.
    resolved_experiment = derive_experiment_name(experiment, extra_features)
    namespace = resolve_tracking_namespace(
        prod_model_name=config["registry"]["model_name"],
        experiment=resolved_experiment,
        tracking_uri_env=os.getenv("MLFLOW_TRACKING_URI"),
    )
    set_tracking_uri(namespace.tracking_uri)
    experiment_id = get_or_create_experiment(namespace.experiment_name)

    print("=" * 70)
    print(f"{model_type} 모델 훈련")
    print("=" * 70)
    if namespace.is_experiment:
        print(f"  [실험] experiment={namespace.experiment_name}")
        print(f"  [실험] registry={namespace.registry_model_name} (prod 승격 대상 아님)")
        print(f"  [실험] tracking_uri={namespace.tracking_uri}")

    with mlflow.start_run(experiment_id=experiment_id) as run:
        print("\n[Step 1] 데이터 로드...")
        dataset = pd.read_csv(data_path)
        print(f"  [OK] {len(dataset)} rows, {len(dataset.columns)} columns")

        # 데이터셋 lineage(#359): run의 Datasets 섹션에 학습 데이터셋을 input으로 남긴다.
        # params(extra_params)와 별개로 dataset 엔티티(이름·source·행 수)와 provenance
        # 태그(피처 소스·기간·FeatureService 등)를 붙여, 어떤 데이터로 학습했는지 run에서
        # 바로 추적 가능하게 한다. extra_params가 없으면(standalone train) 행 수만 남는다.
        dataset_tags = {"rows": str(len(dataset))}
        if extra_params:
            dataset_tags.update(
                {k: str(v) for k, v in extra_params.items() if v is not None}
            )
        log_dataset(
            dataset,
            name="training_dataset",
            source=data_path,
            targets="clicked",
            context="training",
            tags=dataset_tags,
        )

        # 라벨 분포 사전 검증(#421). 학습·저장·등록을 시작하기 전에 막아, 단일
        # 클래스 데이터가 지표 nan인 모델 버전을 registry에 남기지 못하게 한다.
        positive_count, negative_count = require_binary_labels(
            dataset[LABEL_COLUMN], stage="학습 데이터셋"
        )
        print(f"  [OK] 라벨 분포: positive={positive_count}, negative={negative_count}")

        # 실험 피처 사전 검증(#405). 분할·test set 저장·run 로깅·등록을 시작하기
        # 전에 계약 거부를 모두 끝낸다 — 여기서 확정한 순서를 Step 3에서 그대로 쓴다.
        experiment_feature_columns: Optional[tuple[str, ...]] = None
        if extra_features:
            experiment_feature_columns = require_experiment_features_in_dataset(
                dataset, extra_features
            )
            print(f"  [OK] 실험 피처 {list(extra_features)} 확인 — prod 계약 뒤에 덧붙입니다")

        print("\n[Step 2] Train/Val/Test 분할 (Test는 완전 held-out)...")
        if test_size is None:
            test_size = config["data"]["test_size"]
        if val_size is None:
            val_size = config["data"]["val_size"]

        if test_size + val_size >= 1:
            raise ValueError(
                f"test_size({test_size}) + val_size({val_size}) >= 1 입니다 — "
                "train에 데이터가 남지 않거나 분할 자체가 불가능합니다. "
                "두 값의 합이 1보다 작아야 합니다 (예: test_size=0.2, val_size=0.2)."
            )

        source_positions = np.arange(len(dataset))
        train_val_positions, test_positions = train_test_split(
            source_positions,
            test_size=test_size,
            random_state=effective_seeds.split_seed,
            stratify=dataset["clicked"],
        )
        val_ratio_within_train_val = val_size / (1 - test_size)
        train_positions, val_positions = train_test_split(
            train_val_positions,
            test_size=val_ratio_within_train_val,
            random_state=effective_seeds.split_seed,
            stratify=dataset.iloc[train_val_positions]["clicked"],
        )
        train_df = dataset.iloc[train_positions].copy()
        val_df = dataset.iloc[val_positions].copy()
        test_df = dataset.iloc[test_positions].copy()
        print(f"  [OK] Train: {len(train_df)}, Val: {len(val_df)}, Test: {len(test_df)} (Test는 학습에 미사용)")

        # 분할별 라벨 검증(#421). stratify를 쓰므로 전체가 정상이면 대개 각 분할도
        # 정상이지만, 양성이 극소수면 반올림으로 특정 분할의 양성이 0이 될 수 있다
        # (예: 200행 중 양성 2건 → val/test 양성 0건). 그대로 두면 Val ROC-AUC가
        # nan이 되거나 평가 단계 log_loss에서 터지므로 여기서 막는다.
        for stage, split_df in (
            ("Train split", train_df),
            ("Val split", val_df),
            ("Test split", test_df),
        ):
            require_binary_labels(split_df[LABEL_COLUMN], stage=stage)

        if test_set_output is None:
            test_set_path = os.path.join(project_root, config["artifacts"]["test_set_path"])
        elif not os.path.isabs(test_set_output):
            test_set_path = os.path.join(project_root, test_set_output)
        else:
            test_set_path = test_set_output
        os.makedirs(os.path.dirname(test_set_path), exist_ok=True)
        test_df.to_csv(test_set_path, index=False)
        print(f"  [저장] Test set (held-out): {test_set_path}")

        print("\n[Step 3] Feature/Label 분리...")
        # 실험 피처가 없으면 prod 계약 그대로다. 있으면 Step 1에서 이미 검증하며
        # 확정한 순서를 재사용한다 — 여기서 다시 계산하면 거부 시점이 부수효과
        # 뒤로 밀린다(#405).
        if experiment_feature_columns is not None:
            feature_columns = list(experiment_feature_columns)
        else:
            feature_columns = list(MODEL_FEATURE_COLUMNS)
        categorical_columns = list(CATEGORICAL_FEATURE_COLUMNS)

        split_manifest_path_local: Path | None = None
        split_manifest_sha256: str | None = None
        if snapshot_manifest is not None:
            split_manifest_path_local = split_manifest_path(Path(test_set_path))
            split_manifest = build_split_manifest(
                run_id=run.info.run_id,
                snapshot=snapshot_manifest,
                snapshot_manifest_sha256=sha256_file(snapshot_path),
                seeds=effective_seeds,
                test_size=test_size,
                val_size=val_size,
                split_positions={
                    "train": train_positions.tolist(),
                    "validation": val_positions.tolist(),
                    "test": test_positions.tolist(),
                },
                feature_columns=feature_columns,
                experiment_plan_receipt=experiment_plan_receipt,
            )
            write_manifest_atomic(split_manifest, split_manifest_path_local)
            split_manifest_sha256 = sha256_file(split_manifest_path_local)
            _log_reproducibility_artifacts(
                dataset_path=Path(data_path),
                snapshot_path=snapshot_path,
                split_path=split_manifest_path_local,
            )

        X_train = train_df[feature_columns].copy()
        y_train = train_df["clicked"].copy()
        X_val = val_df[feature_columns].copy()
        y_val = val_df["clicked"].copy()

        print(f"  [OK] Train features: {X_train.shape}, ratio={y_train.mean():.3%}")
        print(f"  [OK] Val features: {X_val.shape}, ratio={y_val.mean():.3%}")

        # negative downsampling — train split에만 적용한다(#300). val/test는 위에서
        # 원분포로 유지된다. sampling_rate=1.0이면 downsample_negatives가 원본 그대로
        # 반환(no-op)한다. realized_sampling_rate는 라운딩으로 nominal과 미세하게
        # 다를 수 있어 실현값을 이후 보정·기록에 쓴다.
        nominal_sampling_rate = float(config["model"].get("sampling_rate", 1.0))
        if not math.isfinite(nominal_sampling_rate) or not (
            0.0 < nominal_sampling_rate <= 1.0
        ):
            raise ValueError("sampling_rate는 유한한 (0, 1] 값이어야 합니다")
        realized_sampling_rate = 1.0
        if nominal_sampling_rate < 1.0:
            print("\n[Step 3b] Negative downsampling (train split only)...")
            n_train_before = len(y_train)
            X_train, y_train, realized_sampling_rate = downsample_negatives(
                X_train,
                y_train,
                nominal_sampling_rate,
                random_state=effective_seeds.sampler_seed,
            )
            print(
                f"  [OK] {n_train_before} → {len(y_train)} rows "
                f"(sampling_rate nominal={nominal_sampling_rate}, "
                f"realized={realized_sampling_rate:.4f}), ratio={y_train.mean():.3%}"
            )

        print("\n[Step 4] Categorical 컬럼 dtype 변환...")
        categories_by_column = collect_categorical_categories(
            X_train, X_val, categorical_columns
        )
        print(f"  [OK] {len(categorical_columns)} categorical columns 설정")

        print("\n[Step 5] scale_pos_weight 계산...")
        configured_spw = model_config.get("scale_pos_weight", "auto")
        # downsampling과 scale_pos_weight를 둘 다 걸면 이중 보정이라 He 보정 공식의
        # 전제가 깨진다(#300 결정 6). downsampling이 켜지면 scale_pos_weight는
        # 1로 대체(강제)한다. 단 누군가 config에 명시적 숫자값(≠1, "auto" 아님)을
        # downsampling과 함께 세팅했다면 의도 충돌이므로 fail-closed로 막는다.
        # 이 강제는 auto 계산 이전에 수행한다 — 순서가 뒤바뀌면 auto가 계산한 큰
        # 값이 강제(=1)를 덮어써 이중 보정이 그대로 남는다.
        # LightGBM엔 is_unbalance라는 또 다른 자동 밸런싱 옵션이 있으나 현재
        # config/lgbm_model.py 어디에도 없다(비활성). 추가되면 여기 가드를 확장한다.
        # "downsampling 모드" 판단은 Step 3b 활성화와 동일하게 nominal 기준으로
        # 통일한다. realized로 판단하면 negative가 0/극소라 realized==1.0으로
        # 떨어지는 경우 이 강제를 건너뛰고 auto 경로로 빠져 neg_count=0 →
        # scale_pos_weight=0/pos=0(무효값)이 되는 divergence가 생긴다.
        if is_mlp:
            # MLP는 config의 고정값을 쓰지 않고 weighted BCE의 양성 가중치를
            # 현재 학습 split에서 직접 계산한다. downsampling이 켜져도 그 결과
            # split의 neg/pos가 곧 BCE 가중치가 된다.
            neg_count = int((y_train == 0).sum())
            pos_count = int((y_train == 1).sum())
            scale_pos_weight = compute_auto_scale_pos_weight(y_train)
            print(
                "  [OK] MLP weighted BCE: "
                f"neg={neg_count}, pos={pos_count}, ratio={scale_pos_weight:.2f}"
            )
        elif nominal_sampling_rate < 1.0:
            explicit_numeric = configured_spw != "auto" and float(configured_spw) != 1
            if explicit_numeric:
                raise ValueError(
                    "downsampling(sampling_rate<1.0)과 scale_pos_weight="
                    f"{configured_spw}를 함께 세팅했습니다 — 이중 보정입니다. "
                    "downsampling이 scale_pos_weight를 대체하므로 둘 중 하나만 쓰세요"
                    "(#300 결정 6)."
                )
            scale_pos_weight = 1
            print("  [OK] downsampling 활성 → scale_pos_weight=1 강제(이중 보정 방지)")
        elif configured_spw == "auto":
            # 0 나눗셈(양성 0건)은 compute_auto_scale_pos_weight가 fail-closed로 막는다(#421).
            neg_count = int((y_train == 0).sum())
            pos_count = int((y_train == 1).sum())
            scale_pos_weight = compute_auto_scale_pos_weight(y_train)
            print(f"  [OK] auto 계산: neg={neg_count}, pos={pos_count}, ratio={scale_pos_weight:.2f}")
        else:
            scale_pos_weight = configured_spw
            print(f"  [OK] 고정값: {scale_pos_weight}")

        if is_mlp:
            mlp_hidden_layer_sizes = tuple(
                mlp_config.get(
                    "hidden_layer_sizes",
                    model_config.get(
                        "hidden_layer_sizes",
                        model_config.get("mlp_hidden_layer_sizes", (32, 16)),
                    ),
                )
            )
            mlp_epochs = int(
                mlp_config.get(
                    "epochs",
                    model_config.get("epochs", model_config.get("mlp_epochs", 200)),
                )
            )
            mlp_learning_rate = float(
                mlp_config.get(
                    "learning_rate",
                    model_config.get(
                        "mlp_learning_rate",
                        0.001 if "mlp" in model_config else model_config.get("learning_rate", 0.001),
                    ),
                )
            )
            mlp_batch_size = int(
                mlp_config.get(
                    "batch_size",
                    model_config.get("batch_size", model_config.get("mlp_batch_size", 256)),
                )
            )
            mlp_l2 = float(
                mlp_config.get(
                    "l2", model_config.get("l2", model_config.get("mlp_l2", 1e-4))
                )
            )
            params = {
                "model_type": model_type,
                "hidden_layer_sizes": ",".join(
                    str(size) for size in mlp_hidden_layer_sizes
                ),
                "epochs": mlp_epochs,
                "learning_rate": mlp_learning_rate,
                "batch_size": mlp_batch_size,
                "l2": mlp_l2,
                "scale_pos_weight": scale_pos_weight,
                "split_seed": effective_seeds.split_seed,
                "model_seed": effective_seeds.model_seed,
                "sampler_seed": effective_seeds.sampler_seed,
                # downsampling 실현 비율(#300). 1.0이면 downsampling 미적용. 서빙 보정은
                # 이 값을 쓰므로 실현값(nominal 아님)을 기록한다.
                "sampling_rate": realized_sampling_rate,
                "train_size": len(train_df),
                "val_size": len(val_df),
                "test_size": len(test_df),
            }
        else:
            params = {
                "model_type": model_type,
                "n_estimators": model_config["n_estimators"],
                "learning_rate": model_config["learning_rate"],
                "num_leaves": model_config["num_leaves"],
                "scale_pos_weight": scale_pos_weight,
                "split_seed": effective_seeds.split_seed,
                "model_seed": effective_seeds.model_seed,
                "sampler_seed": effective_seeds.sampler_seed,
                # downsampling 실현 비율(#300). 1.0이면 downsampling 미적용. 서빙 보정은
                # 이 값을 쓰므로 실현값(nominal 아님)을 기록한다.
                "sampling_rate": realized_sampling_rate,
                "train_size": len(train_df),
                "val_size": len(val_df),
                "test_size": len(test_df),
            }
        if legacy_seed_path:
            params["random_state"] = random_state
        if extra_params:
            # 데이터 소스 계보(예: events_source/events_start_date/events_end_date)를
            # run에 남겨서, 어떤 기간의 데이터로 학습했는지 항상 조회 가능하게 한다.
            params.update(extra_params)
        # 실험 좌표를 run에 남긴다(#406 리뷰 2). 모든 실험이 같은 experiment를
        # 공유하므로 이게 없으면 어느 실험의 run인지 registry 이름으로만 알 수
        # 있는데, run-pipeline은 등록을 평가 뒤로 미루므로 **평가에서 실패한 run은
        # 영영 식별 불가**가 된다.
        if namespace.is_experiment:
            params["experiment_name"] = resolved_experiment
            params["registry_model_name"] = namespace.registry_model_name
            if extra_features:
                params["experiment_features"] = ",".join(extra_features)
        log_parameters(params)

        print(f"\n[Step 6] {model_type} 모델 훈련...")
        if is_mlp:
            model = MLPModel(
                scale_pos_weight=scale_pos_weight,
                hidden_layer_sizes=mlp_hidden_layer_sizes,
                epochs=mlp_epochs,
                learning_rate=mlp_learning_rate,
                batch_size=mlp_batch_size,
                l2=mlp_l2,
                random_state=effective_seeds.model_seed,
            )
        else:
            model = LGBMModel(
                scale_pos_weight=scale_pos_weight,
                n_estimators=model_config["n_estimators"],
                learning_rate=model_config["learning_rate"],
                num_leaves=model_config["num_leaves"],
                random_state=effective_seeds.model_seed,
            )
        model.fit(X_train, y_train, categorical_features=categorical_columns)
        print("  [OK] 훈련 완료")

        print("\n[Step 7] 검증...")
        y_val_pred_proba = model.predict_proba(X_val)[:, 1]
        val_roc_auc = roc_auc_score(y_val, y_val_pred_proba)
        print(f"  [OK] Val ROC-AUC: {val_roc_auc:.4f}")

        log_metrics(
            {
                "val_roc_auc": val_roc_auc,
                "train_positive_ratio": float(y_train.mean()),
                "val_positive_ratio": float(y_val.mean()),
            }
        )

        if (X_val["historical_category_match"] == 1).sum() == 0:
            print("  ⚠️  historical_category_match에 1이 없음 (dtype 불일치 가능성)")

        print("\n[Step 8] 모델 저장...")
        if model_output is None:
            model_path = os.path.join(project_root, config["artifacts"]["model_path"])
        elif not os.path.isabs(model_output):
            model_path = os.path.join(project_root, model_output)
        else:
            model_path = model_output
        if feature_columns_output is None:
            feature_columns_path = os.path.join(project_root, config["artifacts"]["feature_columns_path"])
        elif not os.path.isabs(feature_columns_output):
            feature_columns_path = os.path.join(project_root, feature_columns_output)
        else:
            feature_columns_path = feature_columns_output
        if categorical_columns_output is None:
            categorical_columns_path = os.path.join(
                project_root, config["artifacts"]["categorical_columns_path"]
            )
        elif not os.path.isabs(categorical_columns_output):
            categorical_columns_path = os.path.join(project_root, categorical_columns_output)
        else:
            categorical_columns_path = categorical_columns_output

        model.save(model_path)
        save_feature_columns(feature_columns, feature_columns_path)
        save_categorical_columns(categories_by_column, categorical_columns_path)

        # artifact 경로(model/, features/)는 서빙 로더(src/serving/model_loader.py)의
        # MLflow 다운로드 경로 상수와 계약이다 — 변경 시 양쪽을 함께 갱신한다.
        log_artifact(local_path=model_path, artifact_path="model")

        held_out_metric_receipt: HeldOutMetricReceipt | None = None
        held_out_metric_receipts: list[HeldOutMetricReceipt] = []
        if promotion_evidence_enabled:
            if (
                evidence_store is None
                or experiment_plan_receipt is None
                or split_manifest is None
                or split_manifest_sha256 is None
            ):
                raise RuntimeError("promotion evidence 학습 상태가 불완전합니다")
            held_out_metrics = evaluate_held_out_metrics(
                model, test_df, feature_columns, sampling_rate=realized_sampling_rate
            )
            log_metrics(
                {f"held_out_{name}": value for name, value in held_out_metrics.items()}
            )
            # 지표마다 별도 evidence를 게시한다(#493 D3). object key가 evidence body에
            # content-addressed 되어 있어 metric_name이 다르면 key도 달라지므로
            # create-only publish가 충돌하지 않고, roc_auc의 key도 그대로다.
            model_artifact_path = f"model/{Path(model_path).name}"
            model_artifact_sha256 = sha256_file(Path(model_path))
            for metric_name in HELD_OUT_METRIC_NAMES:
                metric_receipt = evidence_store.publish_held_out_metric(
                    HeldOutMetricEvidence(
                        run_id=run.info.run_id,
                        plan_receipt=experiment_plan_receipt,
                        metric_name=metric_name,
                        value=held_out_metrics[metric_name],
                        split_manifest_sha256=split_manifest_sha256,
                        test_membership_sha256=split_manifest.splits[
                            "test"
                        ].membership_sha256,
                        model_artifact_path=model_artifact_path,
                        model_artifact_sha256=model_artifact_sha256,
                    )
                )
                held_out_metric_receipts.append(metric_receipt)
                if metric_name == DEFAULT_HELD_OUT_METRIC_NAME:
                    held_out_metric_receipt = metric_receipt
            if held_out_metric_receipt is None:
                raise RuntimeError("held-out 주 지표 evidence를 게시하지 못했습니다")
            # 주 지표 receipt만 기존 artifact 경로로 남긴다. 이 경로는
            # training_comparison.py의 소비 계약이며, 판정이 다중 지표를 받아들이는
            # 것은 후속 단계(#493 3단계)다.
            _log_held_out_metric_receipt(held_out_metric_receipt)

        # [Step 8b] 기존 LightGBM만 ONNX 배포 패키지를 만든다. MLP challenger는
        # 위에서 저장한 joblib 모델이 evaluate-model의 predict_proba 입력이며,
        # LightGBM 전용 변환기를 호출하지 않는다.
        if is_mlp:
            print("  [OK] MLP joblib 모델 저장 완료 — ONNX 변환 생략")
        else:
            _log_lgbm_deployment_package(
                model=model,
                feature_columns_path=feature_columns_path,
                categorical_columns_path=categorical_columns_path,
                feature_count=len(feature_columns),
                sampling_rate=realized_sampling_rate,
            )

        print("\n[Step 9] Model Registry 등록...")
        # 실험이면 prod와 다른 registry 이름으로 등록된다(#406). 승격 게이트는
        # prod 이름만 조회하므로 실험 버전이 champion 후보로 섞이지 않는다.
        model_name = namespace.registry_model_name
        model_artifact_name = "model" if is_mlp else "model_onnx"
        model_uri = f"runs:/{run.info.run_id}/{model_artifact_name}"
        # sampling_rate를 모델 버전 tag로도 기록한다(#300 결정 7). 서빙이 alias로
        # 모델 버전을 로드하는 순간 tag에서 직접 읽어(run→param 간접 조회 없이)
        # 로드 시 1회 캐싱한다. 승격 게이트(set_model_alias)도 이 tag를 본다.
        registry_tags = {
            "val_roc_auc": f"{val_roc_auc:.4f}",
            "sampling_rate": f"{realized_sampling_rate}",
        }
        # 실험 피처로 학습한 모델은 prod 승격 대상이 아니다. 승격 게이트가 지표를
        # 보기 전에 이 tag로 거부한다(#405 완료조건 4).
        if extra_features:
            registry_tags["experiment_features"] = ",".join(extra_features)
        if extra_params:
            registry_tags.update({k: str(v) for k, v in extra_params.items()})
        if snapshot_manifest is not None and split_manifest_sha256 is not None:
            registry_tags["snapshot_sha256"] = snapshot_manifest.dataset_sha256
            registry_tags["split_manifest_sha256"] = split_manifest_sha256
        pending_registration = PendingRegistration(
            model_uri=model_uri, model_name=model_name, tags=registry_tags
        )
        registered_version = None
        if defer_registration:
            # 평가 통과 뒤에 등록한다(#421). 여기서 등록해버리면 평가가 실패해도
            # 지표를 신뢰할 수 없는 후보 버전이 registry에 남는다. run 로깅과
            # 아티팩트는 위에서 이미 끝났으므로 미뤄도 잃는 정보가 없다.
            print("  [보류] 평가 통과 후 등록합니다 — run 로깅·아티팩트는 이미 저장됨(#421)")
        else:
            # 여기는 아직 run 컨텍스트 안이다. 등록 실패로 예외를 올리면 이미 끝난
            # 학습 run이 FAILED로 마감되고 모델·아티팩트가 실패한 run에 묶인다.
            # 그래서 이 경로에서만 삼킨다(best-effort). 미룬 경로는 run이 닫힌 뒤
            # 호출되므로 이 근거가 없어 호출부에서 그대로 실패시킨다.
            try:
                registered_version = register_pending_model(pending_registration)
            except Exception as exc:
                print(
                    "  ⚠️  Model Registry 등록 실패 — 학습 결과(모델·아티팩트)는 "
                    f"정상 저장됨: {exc}"
                )
            pending_registration = None

    print("\n" + "=" * 70)
    print("훈련 완료")
    print("=" * 70)
    print(f"Val ROC-AUC: {val_roc_auc:.4f}")
    print(f"Model: {model_path}")
    print(f"Feature columns: {feature_columns_path}")
    print(f"Categorical columns: {categorical_columns_path}")
    if registered_version is not None:
        print(f"Registered model: {model_name} v{registered_version}")
    elif pending_registration is not None:
        print(f"Registered model: 등록 보류 (평가 통과 후 등록, run_id={run.info.run_id})")
    else:
        print(f"Registered model: 등록 실패 (건너뜀 — 위 경고 로그 참고, run_id={run.info.run_id})")

    # 실현 sampling_rate는 run-pipeline이 evaluate에 넘겨 오프라인 지표
    # (LogLoss/calibration)를 원분포 기준으로 재게 한다(#300 결정 4). 등록을
    # 미뤘으면 pending_registration도 함께 돌려줘, 호출자가 평가 통과 뒤
    # register_pending_model()로 버전을 만든다(#421).
    return TrainingOutcome(
        sampling_rate=realized_sampling_rate,
        run_id=run.info.run_id,
        registered_version=registered_version,
        pending_registration=pending_registration,
        val_roc_auc=val_roc_auc,
        held_out_metric_receipt=held_out_metric_receipt,
        held_out_metric_receipts=tuple(held_out_metric_receipts),
    )


if __name__ == "__main__":
    main()
