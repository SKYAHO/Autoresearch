"""Feast repository 실행 전 준비(부트스트랩) 헬퍼.

[파이프라인] 피처 구간 — Feast를 사용하는 공개 batch 명령
(``autoresearch.jobs.feast_materialize``)과 serving reader가 Feast repo
config를 읽기 **직전**에 필요한 실행 환경을 갖추는 구간을 담당한다.

[기능] Redis TLS CA 번들을 확인하거나 Secret Manager에서 조달해
``REDIS_TLS_CA_PATH``를 채우고, ``feature_repo.*`` custom online store adapter를
import할 수 있도록 repo의 부모 디렉터리를 ``sys.path``에 넣으며, 준비가 끝난
repo로 Feast ``FeatureStore``를 생성한다.

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
    """지정한 repository path로 Feast FeatureStore를 생성한다."""
    resolved = ensure_repo_importable(repo_path)
    from feast import FeatureStore

    return FeatureStore(repo_path=str(resolved))
