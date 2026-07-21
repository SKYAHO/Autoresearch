from __future__ import annotations

import os
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


def load_feature_store(repo_path: str | Path) -> object:
    """지정한 repository path로 Feast FeatureStore를 생성한다."""
    from feast import FeatureStore

    return FeatureStore(repo_path=str(repo_path))
