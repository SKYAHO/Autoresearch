"""champion 승격 게이트 판정.

[파이프라인] 학습(src/pipeline/train.py) 이후, 서빙이 alias로 모델을 로드하기
전 — Model Registry의 champion alias를 신규 후보 버전으로 옮길지 판정하는
구간을 담당한다. Airflow ctr_model_promote DAG(Autoresearch-airflow#137)가
호출하는 promote-model CLI(src/cli.py)의 판정 본체다.

[기능] 최신 등록 버전을 후보로 삼아 held-out 지표(val_roc_auc)가 현재
champion 이상인지, downsampling 후보면 짝 calibration 버전이 등록돼 있는지
확인한 뒤 게이트를 통과하면 champion(+짝 calibration) alias를 옮긴다.

[비책임] 서빙 시점 alias resolve·페어링 검증(src/serving/model_loader.py의
_resolve_paired_calibration_run_id), Airflow DAG 스케줄링·재시도
(Autoresearch-airflow).
"""

from __future__ import annotations

from typing import Optional

from mlflow.tracking import MlflowClient
from src.tracking.registry import (
    get_latest_version,
    get_model_metrics_by_alias,
    get_model_versions,
    set_model_alias,
)


class GateRejectedError(RuntimeError):
    """게이트 조건(지표 비교 또는 downsampling 페어링) 미달로 승격이 거부됨."""


def _run_id_for_version(versions: list[dict], version: str) -> str:
    for entry in versions:
        if entry["version"] == version:
            return entry["run_id"]
    raise ValueError(f"버전 {version}의 run_id를 찾을 수 없습니다.")


def main(
    model_name: str,
    champion_alias: str,
    calibration_model_name: str,
) -> Optional[str]:
    """게이트 통과 시 champion(+짝 calibration) alias를 최신 후보 버전으로 옮긴다.

    Args:
        model_name: main 모델 registry 이름.
        champion_alias: 승격 대상 alias(보통 'champion').
        calibration_model_name: 짝 calibration 모델 registry 이름.

    Returns:
        승격된 후보 버전 문자열. 평가할 신규 후보가 없으면(등록된 버전이
        없거나 최신 버전이 이미 champion) None.

    Raises:
        GateRejectedError: 게이트 조건 미달로 승격 거부.
        (기타) MLflow 연결 실패 등 실행 중 오류는 그대로 전파한다.
    """
    candidate_version = get_latest_version(model_name)
    if candidate_version is None:
        return None

    existing_versions = get_model_versions(model_name)
    champion_entry = next(
        (v for v in existing_versions if champion_alias in v["aliases"]), None
    )
    if champion_entry is not None and champion_entry["version"] == candidate_version:
        return None

    client = MlflowClient()
    candidate_run_id = _run_id_for_version(existing_versions, candidate_version)
    candidate_metrics = client.get_run(candidate_run_id).data.metrics
    candidate_val_roc_auc = candidate_metrics.get("val_roc_auc")
    if candidate_val_roc_auc is None:
        raise ValueError(
            f"{model_name} v{candidate_version}의 run({candidate_run_id})에 "
            "val_roc_auc 지표가 없습니다."
        )

    champion_metrics = get_model_metrics_by_alias(model_name, champion_alias)
    if champion_metrics is not None:
        champion_val_roc_auc = champion_metrics.get("val_roc_auc")
        if (
            champion_val_roc_auc is not None
            and candidate_val_roc_auc < champion_val_roc_auc
        ):
            raise GateRejectedError(
                f"게이트1 미달: 후보 {model_name} v{candidate_version} "
                f"val_roc_auc={candidate_val_roc_auc:.4f} < champion"
                f"({champion_alias}) val_roc_auc={champion_val_roc_auc:.4f}"
            )

    set_model_alias(model_name, champion_alias, candidate_version)
    return candidate_version
