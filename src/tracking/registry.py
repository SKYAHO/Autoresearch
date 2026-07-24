"""MLflow Model Registry 관리.

모델 등록, 버전 조회, Alias 기반 운영 상태 변경, 메트릭 비교를 담당합니다.
"""

import logging
import os
from typing import Dict, Optional

import mlflow
from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient

logger = logging.getLogger(__name__)


def _serving_calibration_ready() -> bool:
    """서빙 추론에 downsampling 보정이 편입됐는지 여부(#300/#302).

    #302가 서빙에 calibration 체이닝(main → calibration)을 편입·배포하면 이 플래그를
    켠다. 그 전까지 기본값 False라 downsampling 모델(`sampling_rate<1.0`)은 champion으로
    승격되지 못한다(보정 안 된 편향 확률이 서빙에 나가는 것 방지).

    검토 결론(#302): 게이트를 "calibration_model이 Registry에 존재하는가"로 바꾸지 않고
    **env 플래그를 유지**한다. calibration 모델이 등록돼 있어도 서빙이 실제로 그것을
    로드·체이닝하려면 배포 측에서 `RERANK_REGISTRY_CALIBRATION_MODEL_NAME` 등을 세팅해야
    하므로, "모델 존재"는 "서빙이 보정을 적용함"의 충분조건이 아니다. 이 플래그는 서빙
    calibration 배선이 실제로 라이브임을 나타내는 **배포 결합 신호**이고, main↔calibration
    버전이 어긋난 조합을 막는 것은 로더의 페어링 fail-closed 검증(model_loader
    `_resolve_paired_calibration_run_id`)이 런타임에서 담당한다(승격 게이트와 역할 분담).
    """
    return os.environ.get("CTR_SERVING_CALIBRATION_READY", "false").lower() in (
        "1",
        "true",
        "yes",
    )


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

    champion 승격 시 fail-closed 게이트(#300 순서 가드): 승격 대상 버전이
    downsampling 모델(`sampling_rate` tag < 1.0)인데 서빙 보정이 아직 준비되지
    않았으면(`_serving_calibration_ready()`가 False, 기본값) 승격을 거부한다.
    downsampling 모델은 출력 q가 원분포보다 높게 나오므로, 서빙 보정(#302) 전에
    champion으로 올리면 보정 안 된 편향 확률이 서빙 트래픽에 나간다. 이 프로젝트의
    반복 실패 패턴("스펙엔 있는데 코드가 안 지킴")을 코드로 차단한다.
    `sampling_rate` tag가 없는 기존 모델(v6 등)은 1.0으로 간주해 정상 승격된다.

    Args:
        model_name: 모델 이름
        alias: Alias 이름 (예: 'champion', 'challenger', 'rollback')
        version: 모델 버전 번호

    Raises:
        ValueError: champion 승격 대상이 downsampling 모델인데 서빙 보정 미준비.
    """
    client = MlflowClient()
    if alias == "champion" and not _serving_calibration_ready():
        mv = client.get_model_version(name=model_name, version=str(version))
        sampling_rate = float((mv.tags or {}).get("sampling_rate", 1.0))
        if sampling_rate < 1.0:
            raise ValueError(
                f"{model_name} v{version}는 downsampling 모델(sampling_rate="
                f"{sampling_rate})인데 서빙 보정이 아직 준비되지 않았습니다"
                "(#302 미완). 보정 안 된 편향 확률이 서빙에 나가므로 champion "
                "승격을 거부합니다. #302가 서빙 보정을 편입하면 "
                "CTR_SERVING_CALIBRATION_READY=true로 승격하세요(#300 순서 가드)."
            )
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
