"""Memorystore Redis Cluster IAM 인증·TLS Feast online store 어댑터."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Tuple

import google.auth
import google.auth.transport.requests
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
        return expiry - datetime.utcnow() < _TOKEN_REFRESH_MARGIN
