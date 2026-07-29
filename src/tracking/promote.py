"""champion 승격 게이트 판정.

[파이프라인] 학습(src/pipeline/train.py) 이후, 서빙이 alias로 모델을 로드하기
전 — Model Registry의 champion alias를 신규 후보 버전으로 옮길지 판정하는
구간을 담당한다. Airflow ctr_model_promote DAG(Autoresearch-airflow#137)가
호출하는 promote-model CLI(src/cli.py)의 판정 본체다.

[기능] 최신 등록 버전을 후보로 삼아 held-out 지표(val_roc_auc)가 현재
champion 이상인지, downsampling 후보면 같은 run에 calibration 아티팩트가
있는지 확인한 뒤 게이트를 통과하면 champion alias를 옮긴다(#390 단일 기준).

[비책임] 서빙 시점 alias resolve·calibration 로드(src/serving/model_loader.py의
_resolve_calibration_run_id), Airflow DAG 스케줄링·재시도
(Autoresearch-airflow).
"""

from __future__ import annotations

import os
from typing import Optional

from mlflow.tracking import MlflowClient
from src.models.calibration import CALIBRATION_PARAM_FILENAME
from src.tracking.client import set_tracking_uri
from src.tracking.registry import (
    get_latest_version,
    get_model_metrics_by_alias,
    get_model_versions,
    set_model_alias,
)


class GateRejectedError(RuntimeError):
    """게이트 조건(지표 비교 또는 downsampling calibration 아티팩트 부재) 미달로 승격이 거부됨."""


def _run_id_for_version(versions: list[dict], version: str) -> str:
    for entry in versions:
        if entry["version"] == version:
            return entry["run_id"]
    raise ValueError(f"버전 {version}의 run_id를 찾을 수 없습니다.")


def _run_has_calibration_artifact(client: MlflowClient, run_id: str) -> bool:
    """run에 calibration 아티팩트(calibration/calibration.json)가 있으면 True.

    아티팩트 스토어 접근 실패(인프라 오류)는 "게이트 미달"(GateRejectedError)과 구분해야
    한다 — 전자는 재시도·인프라 점검 대상이고 후자는 재학습 대상이라 운영 대응이 다르다.
    그래서 list_artifacts 예외를 GateRejectedError로 삼키지 않고 RuntimeError로 감싸 그대로
    전파한다(CLI에서 "[게이트 미달]"이 아니라 "[에러]"로 갈리고, DAG 알림도 구분 가능).
    """
    try:
        artifacts = client.list_artifacts(run_id, "calibration")
    except Exception as error:
        raise RuntimeError(
            f"calibration 아티팩트 존재 확인 중 아티팩트 스토어 접근에 실패했습니다"
            f"(인프라 오류, run={run_id}): {error}"
        ) from error
    return any(entry.path.endswith(CALIBRATION_PARAM_FILENAME) for entry in artifacts)


def main(
    model_name: str,
    champion_alias: str,
) -> Optional[str]:
    """게이트 통과 시 champion alias를 최신 후보 버전으로 옮긴다(#390 단일 기준).

    Args:
        model_name: main 모델 registry 이름.
        champion_alias: 승격 대상 alias(보통 'champion').

    Returns:
        승격된 후보 버전 문자열. 평가할 신규 후보가 없으면(등록된 버전이
        없거나 최신 버전이 이미 champion) None.

    Raises:
        GateRejectedError: 게이트 조건 미달로 승격 거부.
        ValueError: 후보 버전의 run에 val_roc_auc 지표가 없음(데이터 결함).
        (기타) MLflow 연결·아티팩트 스토어 접근 실패 등 실행 중 오류는 그대로 전파한다.
    """
    set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000"))
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
        if champion_val_roc_auc is None:
            raise ValueError(
                f"{model_name}@{champion_alias}의 run에 val_roc_auc 지표가 없어 "
                "후보와 비교할 수 없습니다(수동 승격 등으로 지표 없이 등록된 "
                "champion일 수 있습니다) — 비교 불가를 자동 통과로 처리하지 않고 "
                "fail-closed로 거부합니다."
            )
        if candidate_val_roc_auc < champion_val_roc_auc:
            raise GateRejectedError(
                f"게이트1 미달: 후보 {model_name} v{candidate_version} "
                f"val_roc_auc={candidate_val_roc_auc:.4f} < champion"
                f"({champion_alias}) val_roc_auc={champion_val_roc_auc:.4f}"
            )

    candidate_mv = client.get_model_version(name=model_name, version=candidate_version)
    sampling_rate = float((candidate_mv.tags or {}).get("sampling_rate", 1.0))
    if sampling_rate < 1.0:
        # downsampling 후보는 같은 run에 calibration 아티팩트가 있어야 한다(#390 run_id 종속).
        # 서빙이 승격 후 main run_id로 이 아티팩트를 로드하므로, 없으면 보정 안 된 편향 확률이
        # 나간다 — 승격 전에 fail-closed로 막는다(서빙 로더도 런타임에서 한 번 더 방어).
        if not _run_has_calibration_artifact(client, candidate_run_id):
            raise GateRejectedError(
                f"게이트2 미달: 후보 {model_name} v{candidate_version}는 "
                f"downsampling(sampling_rate={sampling_rate})인데 run({candidate_run_id})에 "
                f"calibration 아티팩트(calibration/{CALIBRATION_PARAM_FILENAME})가 없습니다."
            )

    # 승격 기준은 main 하나뿐이다(#390). calibration은 이 후보와 같은 run에 종속돼 있어 서빙이
    # main run_id로 함께 로드하므로, 별도 alias를 옮길 필요가 없고 두 alias의 비원자 전환에서
    # 오던 어긋난 조합(동기화) 문제 자체가 사라진다.
    set_model_alias(model_name, champion_alias, candidate_version)
    return candidate_version
