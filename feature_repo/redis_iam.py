"""Memorystore Redis Cluster IAM 인증·TLS Feast online store 어댑터."""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Literal, Optional, Tuple

import google.auth
import google.auth.transport.requests
from feast.infra.online_stores.redis import (
    RedisOnlineStore,
    RedisOnlineStoreConfig,
    RedisType,
)
from pydantic import StrictStr, field_validator
from redis import Redis
from redis.cluster import ClusterNode, RedisCluster
from redis.credentials import CredentialProvider

_TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _default_credentials() -> Any:
    credentials, _ = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
    return credentials


class GCPIAMCredentialProvider(CredentialProvider):
    """연결 시점마다 유효한 IAM access token을 Redis AUTH 자격으로 제공한다."""

    def __init__(
        self, credentials_factory: Callable[[], Any] | None = None
    ) -> None:
        self._lock = threading.Lock()
        self._credentials = (credentials_factory or _default_credentials)()

    def get_credentials(self) -> Tuple[str]:
        with self._lock:
            if self._needs_refresh():
                request = google.auth.transport.requests.Request()
                self._credentials.refresh(request)
            token: Optional[str] = self._credentials.token
        if not token:
            raise RuntimeError("IAM access token could not be issued")
        return (token,)

    def _needs_refresh(self) -> bool:
        if not self._credentials.valid:
            return True
        expiry = self._credentials.expiry
        if expiry is None:
            return False
        # google-auth의 expiry는 naive UTC이므로 같은 표현으로 비교한다.
        now_utc = datetime.now(UTC).replace(tzinfo=None)
        return expiry - now_utc < _TOKEN_REFRESH_MARGIN


class IAMRedisOnlineStoreConfig(RedisOnlineStoreConfig):
    """IAM 인증·TLS Redis Cluster용 online store 설정."""

    type: Literal["feature_repo.redis_iam.IAMRedisOnlineStore"] = (
        "feature_repo.redis_iam.IAMRedisOnlineStore"
    )

    iam_auth: bool = True

    tls_ca_cert_path: Optional[StrictStr] = None

    @field_validator("tls_ca_cert_path", mode="before")
    @classmethod
    def _drop_unexpanded_env(cls, value: object) -> object:
        if isinstance(value, str) and value.startswith("${"):
            return None
        return value


class IAMRedisOnlineStore(RedisOnlineStore):
    """IAM token 인증과 TLS를 주입하는 RedisOnlineStore 확장."""

    _credential_provider: Optional[GCPIAMCredentialProvider] = None

    def _iam_kwargs(self, config: IAMRedisOnlineStoreConfig) -> dict[str, Any]:
        if self._credential_provider is None:
            self._credential_provider = GCPIAMCredentialProvider()
        kwargs: dict[str, Any] = {
            "credential_provider": self._credential_provider,
            "ssl": True,
        }
        if config.tls_ca_cert_path:
            if not os.path.exists(config.tls_ca_cert_path):
                raise FileNotFoundError(
                    f"Redis TLS CA bundle not found: {config.tls_ca_cert_path}"
                )
            kwargs["ssl_ca_certs"] = config.tls_ca_cert_path
        return kwargs

    def _get_client(self, online_store_config: IAMRedisOnlineStoreConfig):
        if not online_store_config.iam_auth:
            return super()._get_client(online_store_config)
        if not self._client:
            startup_nodes, kwargs = self._parse_connection_string(
                online_store_config.connection_string
            )
            kwargs.update(self._iam_kwargs(online_store_config))
            if online_store_config.redis_type == RedisType.redis_cluster:
                kwargs["startup_nodes"] = [
                    ClusterNode(**node) for node in startup_nodes
                ]
                self._client = RedisCluster(**kwargs)
            else:
                kwargs["host"] = startup_nodes[0]["host"]
                kwargs["port"] = startup_nodes[0]["port"]
                self._client = Redis(**kwargs)
        return self._client

    async def _get_client_async(
        self, online_store_config: IAMRedisOnlineStoreConfig
    ):
        raise NotImplementedError(
            "IAMRedisOnlineStore does not support the async client yet"
        )
