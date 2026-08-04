"""champion 승격 게이트 판정.

[파이프라인] 학습(src/pipeline/train.py) 이후, 서빙이 alias로 모델을 로드하기
전 — Model Registry의 champion alias를 신규 후보 버전으로 옮길지 판정하는
구간을 담당한다. Airflow ctr_model_promote DAG(Autoresearch-airflow#137)가
호출하는 promote-model CLI(src/cli.py)의 판정 본체다.

[기능] 최신 등록 버전을 후보로 삼아 held-out 지표(val_roc_auc)가 현재
champion 이상인지, downsampling 후보면 같은 run에 calibration 아티팩트가
있는지 확인한다. 정상 판정은 구조화 결과로 반환하고 게이트를 통과하면
champion alias를 옮긴다(#390 단일 기준).

[비책임] 서빙 시점 alias resolve·manifest/calibration 로드(src/serving/model_loader.py),
Airflow DAG 스케줄링·재시도
(Autoresearch-airflow).
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from mlflow.tracking import MlflowClient
from src.models.calibration import CALIBRATION_PARAM_FILENAME
from src.tracking.client import set_tracking_uri
from src.tracking.namespace import is_experiment_model_name
from src.tracking.model_package import load_manifest
from src.tracking.promotion_result import (
    ModelPromotionResult,
    PromotionExecutionError,
    PromotionOutcome,
    PromotionReasonCode,
)
from src.tracking.registry import (
    ServingCalibrationNotReadyError,
    get_model_metrics_by_alias,
    get_model_versions,
    set_model_alias,
)


class GateRejectedError(RuntimeError):
    """legacy CLI adapter가 구조화 전 호출 계약을 보존할 때 사용하는 예외."""


def _run_id_for_version(versions: list[dict], version: str) -> str:
    for entry in versions:
        if entry["version"] == version:
            return entry["run_id"]
    raise ValueError(f"버전 {version}의 run_id를 찾을 수 없습니다.")


def _run_has_calibration_artifact(client: MlflowClient, run_id: str) -> bool:
    """run에 calibration 아티팩트가 있는지 조회한다."""

    try:
        artifacts = client.list_artifacts(run_id, "calibration")
    except Exception as error:
        raise PromotionExecutionError(
            PromotionReasonCode.ARTIFACT_LOOKUP_FAILED,
            "calibration 아티팩트 존재 확인 중 저장소 접근에 실패했습니다.",
        ) from error
    return any(entry.path.endswith(CALIBRATION_PARAM_FILENAME) for entry in artifacts)


def _run_has_valid_manifest(client: MlflowClient, run_id: str) -> bool:
    """후보 run의 고정 manifest가 존재하고 v1 스키마를 만족하는지 확인한다."""
    try:
        artifacts = client.list_artifacts(run_id, "manifest")
    except Exception as error:
        raise PromotionExecutionError(
            PromotionReasonCode.ARTIFACT_LOOKUP_FAILED,
            "manifest 아티팩트 존재 확인 중 저장소 접근에 실패했습니다.",
        ) from error
    if not any(entry.path == "manifest/manifest.json" for entry in artifacts):
        return False
    try:
        with TemporaryDirectory(prefix="ctr-promotion-manifest-") as workspace:
            path = client.download_artifacts(
                run_id, "manifest/manifest.json", dst_path=workspace
            )
            load_manifest(Path(path))
    except Exception:
        return False
    return True


def main(
    model_name: str,
    champion_alias: str,
) -> ModelPromotionResult:
    """최신 후보의 champion 승격을 판정하고 구조화 결과를 반환한다.

    Args:
        model_name: main 모델 registry 이름.
        champion_alias: 승격 대상 alias(보통 'champion').

    Returns:
        승격, 게이트 미달 또는 후보 없음 결과.

    Raises:
        PromotionExecutionError: registry·지표·아티팩트·alias 실행 오류.
    """
    # 실험 네임스페이스 모델은 애초에 승격 대상이 아니다(#406). 실험은 prod와 다른
    # 이름으로 등록되므로 정상 경로에서는 여기 도달하지 않지만, 이름을 직접 넘겨
    # 부르는 경우까지 막아 "실행해도 prod champion에 영향이 없음"을 보장한다.
    if is_experiment_model_name(model_name):
        # NO_CANDIDATE인 이유는 #405의 후보 제외와 같다 — 게이트를 못 넘은 게 아니라
        # 애초에 승격 가능한 후보가 없는 상태다. 두 경로가 같은 분류를 쓰므로 일일
        # DAG의 알람 해석이 한 가지로 유지된다.
        return ModelPromotionResult(
            outcome=PromotionOutcome.NO_CANDIDATE,
            model_name=model_name,
            champion_alias=champion_alias,
            reason_code=PromotionReasonCode.EXPERIMENT_MODEL,
        )

    # os.getenv(name, default)는 **빈 문자열**에 default를 적용하지 않는다. .env.example
    # 대로 `MLFLOW_TRACKING_URI=`가 주입된 환경에서는 set_tracking_uri("")가 불려
    # 조용히 엉뚱한 곳을 보게 된다 — 학습은 #406에서 막았으므로 승격도 같이 막아
    # 한 폐루프 안에 fail-fast와 silent fallback이 공존하지 않게 한다(#406 리뷰 3).
    promote_tracking_uri = (os.getenv("MLFLOW_TRACKING_URI") or "").strip()
    if not promote_tracking_uri:
        raise PromotionExecutionError(
            PromotionReasonCode.REGISTRY_ACCESS_FAILED,
            "MLFLOW_TRACKING_URI가 설정되지 않아 승격 대상 registry를 특정할 수 없습니다.",
        )

    try:
        set_tracking_uri(promote_tracking_uri)
        existing_versions = get_model_versions(model_name)
    except Exception as error:
        raise PromotionExecutionError(
            PromotionReasonCode.REGISTRY_ACCESS_FAILED,
            "모델 후보 조회에 실패했습니다.",
        ) from error

    if not existing_versions:
        return ModelPromotionResult(
            outcome=PromotionOutcome.NO_CANDIDATE,
            model_name=model_name,
            champion_alias=champion_alias,
            reason_code=PromotionReasonCode.REGISTRY_EMPTY,
        )

    champion_entry = next(
        (v for v in existing_versions if champion_alias in v["aliases"]), None
    )
    champion_version = (
        champion_entry["version"] if champion_entry is not None else None
    )

    # 실험 피처로 학습한 버전은 후보 **선택에서 제외**한다(#405). 단순히 거부만 하면
    # 실험 학습이 만든 버전이 번호상 최신이라는 이유로 후보를 차지하고, 그 앞의
    # 정상 prod 후보가 영원히 승격되지 못한다 — 실험을 돌린 날의 champion 갱신이
    # 조용히 유실된다. prod 계약에 없는 입력으로 학습돼 서빙이 그 피처를 만들어낼
    # 수 없으므로 애초에 후보 자격이 없다.
    promotable = [
        entry for entry in existing_versions if not entry["tags"].get("experiment_features")
    ]
    if not promotable:
        return ModelPromotionResult(
            outcome=PromotionOutcome.NO_CANDIDATE,
            model_name=model_name,
            champion_alias=champion_alias,
            champion_version=champion_version,
            reason_code=PromotionReasonCode.EXPERIMENT_MODEL,
        )

    candidate_entry = max(promotable, key=lambda entry: int(entry["version"]))
    candidate_version = candidate_entry["version"]
    # 아래 sampling_rate 게이트가 다시 조회하지 않도록 여기서 한 번만 읽는다.
    candidate_tags = candidate_entry["tags"]

    if champion_entry is not None and champion_entry["version"] == candidate_version:
        return ModelPromotionResult(
            outcome=PromotionOutcome.NO_CANDIDATE,
            model_name=model_name,
            champion_alias=champion_alias,
            candidate_version=candidate_version,
            champion_version=champion_version,
            reason_code=PromotionReasonCode.ALREADY_CHAMPION,
        )

    try:
        client = MlflowClient()
        candidate_run_id = _run_id_for_version(existing_versions, candidate_version)
        candidate_metrics = client.get_run(candidate_run_id).data.metrics
    except Exception as error:
        raise PromotionExecutionError(
            PromotionReasonCode.REGISTRY_ACCESS_FAILED,
            "후보 모델 지표 조회에 실패했습니다.",
            candidate_version=candidate_version,
            champion_version=champion_version,
        ) from error
    candidate_val_roc_auc = candidate_metrics.get("val_roc_auc")
    if candidate_val_roc_auc is None or not math.isfinite(candidate_val_roc_auc):
        raise PromotionExecutionError(
            PromotionReasonCode.METRIC_MISSING,
            "후보 모델에 유한한 val_roc_auc 지표가 없습니다.",
            candidate_version=candidate_version,
            champion_version=champion_version,
        )

    if not _run_has_valid_manifest(client, candidate_run_id):
        return ModelPromotionResult(
            outcome=PromotionOutcome.REJECTED,
            model_name=model_name,
            champion_alias=champion_alias,
            candidate_version=candidate_version,
            champion_version=champion_version,
            candidate_metric=candidate_val_roc_auc,
            reason_code=PromotionReasonCode.MANIFEST_ARTIFACT_INVALID,
        ).with_legacy_message(
            f"게이트 미달: 후보 {model_name} v{candidate_version}의 "
            "manifest/manifest.json이 없거나 ctr-model-package-v1 계약에 맞지 않습니다."
        )

    try:
        champion_metrics = get_model_metrics_by_alias(model_name, champion_alias)
    except Exception as error:
        raise PromotionExecutionError(
            PromotionReasonCode.REGISTRY_ACCESS_FAILED,
            "champion 모델 지표 조회에 실패했습니다.",
            candidate_version=candidate_version,
            champion_version=champion_version,
            candidate_metric=candidate_val_roc_auc,
        ) from error
    champion_val_roc_auc: float | None = None
    if champion_metrics is not None:
        champion_val_roc_auc = champion_metrics.get("val_roc_auc")
        if champion_val_roc_auc is None or not math.isfinite(champion_val_roc_auc):
            raise PromotionExecutionError(
                PromotionReasonCode.METRIC_MISSING,
                "champion 모델에 유한한 val_roc_auc 지표가 없습니다.",
                candidate_version=candidate_version,
                champion_version=champion_version,
                candidate_metric=candidate_val_roc_auc,
            )
        if candidate_val_roc_auc < champion_val_roc_auc:
            return ModelPromotionResult(
                outcome=PromotionOutcome.REJECTED,
                model_name=model_name,
                champion_alias=champion_alias,
                candidate_version=candidate_version,
                champion_version=champion_version,
                candidate_metric=candidate_val_roc_auc,
                champion_metric=champion_val_roc_auc,
                reason_code=PromotionReasonCode.METRIC_BELOW_CHAMPION,
            )

    # 후보 선택 때 읽은 tag를 재사용한다 — registry 왕복을 한 번 줄이고, 두 조회
    # 사이에 tag가 바뀔 여지도 없앤다(#405 리뷰).
    try:
        sampling_rate = float(candidate_tags.get("sampling_rate", 1.0))
    except (TypeError, ValueError) as error:
        raise PromotionExecutionError(
            PromotionReasonCode.REGISTRY_ACCESS_FAILED,
            "후보 모델 sampling_rate tag를 해석할 수 없습니다.",
            candidate_version=candidate_version,
            champion_version=champion_version,
            candidate_metric=candidate_val_roc_auc,
            champion_metric=champion_val_roc_auc,
        ) from error
    if sampling_rate < 1.0:
        # downsampling 후보는 같은 run에 calibration 아티팩트가 있어야 한다(#390 run_id 종속).
        # 서빙이 승격 후 main run_id로 이 아티팩트를 로드하므로, 없으면 보정 안 된 편향 확률이
        # 나간다 — 승격 전에 fail-closed로 막는다(서빙 로더도 런타임에서 한 번 더 방어).
        try:
            has_calibration_artifact = _run_has_calibration_artifact(
                client, candidate_run_id
            )
        except PromotionExecutionError as error:
            raise PromotionExecutionError(
                error.reason_code,
                str(error),
                candidate_version=candidate_version,
                champion_version=champion_version,
                candidate_metric=candidate_val_roc_auc,
                champion_metric=champion_val_roc_auc,
            ) from error
        if not has_calibration_artifact:
            return ModelPromotionResult(
                outcome=PromotionOutcome.REJECTED,
                model_name=model_name,
                champion_alias=champion_alias,
                candidate_version=candidate_version,
                champion_version=champion_version,
                candidate_metric=candidate_val_roc_auc,
                champion_metric=champion_val_roc_auc,
                reason_code=PromotionReasonCode.CALIBRATION_ARTIFACT_MISSING,
            ).with_legacy_message(
                f"게이트2 미달: 후보 {model_name} v{candidate_version}는 "
                f"downsampling(sampling_rate={sampling_rate})인데 "
                f"run({candidate_run_id})에 calibration 아티팩트"
                f"(calibration/{CALIBRATION_PARAM_FILENAME})가 없습니다."
            )

    # 승격 기준은 main 하나뿐이다(#390). calibration은 이 후보와 같은 run에 종속돼 있어 서빙이
    # main run_id로 함께 로드하므로, 별도 alias를 옮길 필요가 없고 두 alias의 비원자 전환에서
    # 오던 어긋난 조합(동기화) 문제 자체가 사라진다.
    try:
        set_model_alias(model_name, champion_alias, candidate_version)
    except ServingCalibrationNotReadyError:
        return ModelPromotionResult(
            outcome=PromotionOutcome.REJECTED,
            model_name=model_name,
            champion_alias=champion_alias,
            candidate_version=candidate_version,
            champion_version=champion_version,
            candidate_metric=candidate_val_roc_auc,
            champion_metric=champion_val_roc_auc,
            reason_code=PromotionReasonCode.SERVING_CALIBRATION_NOT_READY,
        )
    except Exception as error:
        raise PromotionExecutionError(
            PromotionReasonCode.ALIAS_UPDATE_FAILED,
            "champion alias 이동에 실패했습니다.",
            candidate_version=candidate_version,
            champion_version=champion_version,
            candidate_metric=candidate_val_roc_auc,
            champion_metric=champion_val_roc_auc,
        ) from error

    return ModelPromotionResult(
        outcome=PromotionOutcome.PROMOTED,
        model_name=model_name,
        champion_alias=champion_alias,
        candidate_version=candidate_version,
        champion_version=champion_version,
        candidate_metric=candidate_val_roc_auc,
        champion_metric=champion_val_roc_auc,
        reason_code=(
            PromotionReasonCode.FIRST_CHAMPION
            if champion_version is None
            else PromotionReasonCode.METRIC_NOT_DEGRADED
        ),
    )
