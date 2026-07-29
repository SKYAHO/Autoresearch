"""Feast repository 실행 전 준비(부트스트랩) 헬퍼.

[파이프라인] 피처 구간 — Feast를 사용하는 공개 batch 명령
(``autoresearch.jobs.feast_materialize``)과 serving reader가 Feast repo
config를 읽기 **직전**에 필요한 실행 환경을 갖추는 구간을 담당한다.

[기능] Redis TLS CA 번들을 확인하거나 Secret Manager에서 조달해
``REDIS_TLS_CA_PATH``를 채우고(같은 프로세스용 ``ensure_redis_ca_bundle``,
별도 프로세스가 이어받을 고정 경로용 ``download_redis_ca_bundle``),
``feature_repo.*`` custom online store adapter를 import할 수 있도록 repo의 부모
디렉터리를 ``sys.path``에 넣으며, 준비가 끝난 repo로 Feast ``FeatureStore``를
생성한다.

[비책임] CLI 인자 계약·종료 코드는 ``autoresearch/jobs/``의 각 batch 모듈이,
Entity·FeatureView 정의는 ``feature_repo/``의 정의 파일이 소유한다.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import MutableMapping
from pathlib import Path


def _fetch_ca_secret(project_id: str, secret_id: str) -> bytes:
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data


def ensure_redis_ca_bundle(
    environment: MutableMapping[str, str] | None = None,
) -> str | None:
    """Redis TLS CA bundle을 확인하거나 Secret Manager에서 준비한다."""
    env = os.environ if environment is None else environment
    ca_path = env.get("REDIS_TLS_CA_PATH", "").strip()
    if ca_path and Path(ca_path).exists():
        return ca_path
    secret_id = env.get("REDIS_CA_SECRET_ID", "").strip()
    if not secret_id:
        if ca_path:
            raise RuntimeError(f"Redis TLS CA bundle not found: {ca_path}")
        return None
    project_id = env.get("GCP_PROJECT_ID", "").strip()
    if not project_id:
        raise RuntimeError(
            "GCP_PROJECT_ID is required to fetch the Redis CA bundle"
        )
    payload = _fetch_ca_secret(project_id, secret_id)
    handle = tempfile.NamedTemporaryFile(mode="wb", suffix=".pem", delete=False)
    with handle:
        handle.write(payload)
    env["REDIS_TLS_CA_PATH"] = handle.name
    return handle.name


def download_redis_ca_bundle(
    destination: str | Path,
    environment: MutableMapping[str, str] | None = None,
) -> str:
    """Redis TLS CA 번들을 Secret Manager에서 받아 지정한 경로에 기록한다.

    ``ensure_redis_ca_bundle``은 임시 파일 경로를 호출 프로세스의 env에만
    심으므로, CA 조달과 ``feast`` 실행이 서로 다른 프로세스로 나뉘는 경우
    (GKE Job에서 CA를 먼저 받고 feast CLI를 실행하는 경로)에는 쓸 수 없다.
    이 함수는 호출자가 지정한 고정 경로에 써서 후속 프로세스가
    ``REDIS_TLS_CA_PATH``로 같은 파일을 가리킬 수 있게 한다.
    """
    env = os.environ if environment is None else environment
    secret_id = env.get("REDIS_CA_SECRET_ID", "").strip()
    if not secret_id:
        raise RuntimeError(
            "REDIS_CA_SECRET_ID is required to download the Redis CA bundle"
        )
    project_id = env.get("GCP_PROJECT_ID", "").strip()
    if not project_id:
        raise RuntimeError(
            "GCP_PROJECT_ID is required to fetch the Redis CA bundle"
        )
    payload = _fetch_ca_secret(project_id, secret_id)
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    env["REDIS_TLS_CA_PATH"] = str(target)
    return str(target)


def ensure_repo_importable(repo_path: str | Path) -> Path:
    """repo의 부모 디렉터리를 sys.path에 넣어 `feature_repo.*` import를 가능하게 한다.

    feature_store.yaml의 ``online_store.type``이 custom adapter
    (``feature_repo.redis_iam.IAMRedisOnlineStore``)를 가리키므로 config 검증
    전에 이 처리가 끝나 있어야 한다. 해석된 절대 경로를 반환한다.
    """
    resolved = Path(repo_path).resolve()
    parent = str(resolved.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    return resolved


def load_feature_store(repo_path: str | Path) -> object:
    """지정한 repository path로 Feast FeatureStore를 생성한다.

    ``feature_store.yaml``의 ``full_scan_for_deletion: ${FEAST_ONLINE_FULL_SCAN_FOR_DELETION}``
    치환이 성립하도록, config를 읽기 전에 그 env 기본값을 채운다(prod/dev 환경
    분리, #399 — prod=true, dev=false). 배포가 이미 심은 값은 덮지 않는다.
    """
    resolved = ensure_repo_importable(repo_path)
    from feature_repo.env import ensure_online_store_env

    ensure_online_store_env()
    from feast import FeatureStore

    return FeatureStore(repo_path=str(resolved))
