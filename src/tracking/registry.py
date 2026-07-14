"""MLflow Model Registry 관리.

모델 등록, 버전 조회, Alias 기반 운영 상태 변경, 메트릭 비교를 담당합니다.
"""

import logging
from typing import Dict, Optional

import mlflow
from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient

logger = logging.getLogger(__name__)


def register_model(model_uri: str, model_name: str, tags: Optional[Dict[str, str]] = None) -> str:
    """Model Registry에 모델 등록.

    Args:
        model_uri: 모델 URI (runs:/<run_id>/model)
        model_name: 모델 이름 (예: ctr-model)
        tags: 모델 태그 (optional, key/value 딕셔너리)

    Returns:
        모델 버전 문자열
    """
    model_version = mlflow.register_model(model_uri, model_name)
    if tags:
        client = MlflowClient()
        for key, value in tags.items():
            client.set_model_version_tag(
                model_name,
                model_version.version,
                key,
                str(value),
            )
    return model_version.version


def get_model_versions(model_name: str) -> list[Dict]:
    """모델의 모든 버전 조회.

    Args:
        model_name: 모델 이름

    Returns:
        버전 정보 리스트 (version, aliases, run_id, creation_timestamp 포함)
    """
    client = MlflowClient()
    versions = client.search_model_versions(f"name='{model_name}'")
    return [
        {
            "version": v.version,
            "aliases": list(v.aliases) if v.aliases else [],
            "run_id": v.run_id,
            "creation_timestamp": v.creation_timestamp,
        }
        for v in versions
    ]


def get_latest_version(model_name: str) -> Optional[str]:
    """버전 번호가 가장 높은 모델 버전 조회.

    주의: 이 함수는 버전 번호 순서로 "최신"을 판단합니다.
    실제 운영 중인 모델(champion alias)과는 다를 수 있습니다.
    
    용도 구분:
    - 가장 최근 등록된 후보 모델 조회 → 이 함수 사용 (가장 큰 버전 번호)
    - 현재 운영 모델 조회 → get_model_metrics_by_alias() 사용 (champion alias)

    Args:
        model_name: 모델 이름

    Returns:
        가장 높은 버전 번호 (없으면 None)
    """
    client = MlflowClient()
    versions = client.search_model_versions(f"name='{model_name}'")
    if not versions:
        return None
    return max(versions, key=lambda v: int(v.version)).version


def set_model_alias(model_name: str, alias: str, version: str) -> None:
    """모델에 Alias 할당.

    Alias를 사용하여 모델 버전을 논리적으로 구분합니다.
    기본 alias: 'champion' (운영 모델).

    Args:
        model_name: 모델 이름
        alias: Alias 이름 (예: 'champion', 'challenger', 'rollback')
        version: 모델 버전 번호
    """
    client = MlflowClient()
    client.set_registered_model_alias(name=model_name, alias=alias, version=version)


def get_model_metrics_by_alias(
    model_name: str,
    alias: str = "champion",
) -> Optional[Dict[str, float]]:
    """특정 Alias를 가진 모델의 메트릭 조회.

    정상 상태(Alias 미존재)와 실제 장애(서버 연결 실패, 권한 오류)를 구분합니다:
    - Alias 미존재 → None 반환 (정상)
    - 서버 연결 실패/권한 오류 → 예외 재전파 (장애)

    Args:
        model_name: 모델 이름
        alias: Alias 이름 (기본값: 'champion')

    Returns:
        메트릭 딕셔너리 (Alias 미존재 시 None)

    Raises:
        MlflowException: Tracking 서버 연결 실패, 권한 오류 등
    """
    client = MlflowClient()
    try:
        model_version = client.get_model_version_by_alias(
            name=model_name,
            alias=alias,
        )
    except MlflowException as e:
        error_msg = str(e).lower()
        if "no alias" in error_msg or "not found" in error_msg or "does not exist" in error_msg:
            return None
        logger.error(f"MLflow Alias 조회 중 오류: {e}")
        raise

    run = client.get_run(model_version.run_id)
    return dict(run.data.metrics)
