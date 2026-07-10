"""MLflow Model Registry 관리.

모델 등록, 버전 조회, 운영 상태 변경, 메트릭 비교를 담당합니다.
"""

from typing import Dict, List, Optional

import mlflow
from mlflow.entities import Model


def register_model(model_uri: str, model_name: str, tags: Optional[Dict[str, str]] = None) -> str:
    """Model Registry에 모델 등록.

    Args:
        model_uri: 모델 URI (runs:/<run_id>/model)
        model_name: 모델 이름 (예: ctr-model)
        tags: 모델 태그 (optional)

    Returns:
        모델 버전 문자열
    """
    model_version = mlflow.register_model(model_uri, model_name)
    if tags:
        client = mlflow.tracking.MlflowClient()
        client = mlflow.tracking.MlflowClient()
        for key, value in tags.items():
            client.set_model_version_tag(model_name, model_version.version, key, value)
    return model_version.version


def get_model_versions(model_name: str) -> List[Dict]:
    """모델의 모든 버전 조회.

    Args:
        model_name: 모델 이름

    Returns:
        버전 정보 리스트
    """
    client = mlflow.tracking.MlflowClient()
    versions = client.search_model_versions(f"name='{model_name}'")
    return [
        {
            "version": v.version,
            "stage": v.current_stage,
            "run_id": v.run_id,
            "creation_timestamp": v.creation_timestamp,
        }
        for v in versions
    ]


def get_latest_version(model_name: str) -> Optional[str]:
    """최신 버전 조회.

    Args:
        model_name: 모델 이름

    Returns:
        최신 버전 번호 (없으면 None)
    """
    client = mlflow.tracking.MlflowClient()
    versions = client.search_model_versions(f"name='{model_name}'")
    if not versions:
        return None
    return max(versions, key=lambda v: int(v.version)).version


def transition_model_stage(model_name: str, version: str, stage: str) -> None:
    """모델 스테이지 변경.

    Args:
        model_name: 모델 이름
        version: 모델 버전
        stage: 대상 스테이지 (Staging, Production, Archived)
    """
    client = mlflow.tracking.MlflowClient()
    client.transition_model_version_stage(model_name, version, stage)


def get_production_model_metrics(model_name: str) -> Optional[Dict]:
    """Production 모델의 메트릭 조회.

    Args:
        model_name: 모델 이름

    Returns:
        메트릭 딕셔너리 (없으면 None)
    """
    client = mlflow.tracking.MlflowClient()
    versions = client.search_model_versions(
        f"name='{model_name}' AND stage='Production'"
    )
    if not versions:
        return None

    prod_version = versions[0]
    run = client.get_run(prod_version.run_id)
    return run.data.metrics
