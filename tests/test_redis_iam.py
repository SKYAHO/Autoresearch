"""feature_repo.redis_iam 어댑터 단위 테스트 (feast 그룹 환경 전용)."""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("feast")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from feature_repo import redis_iam  # noqa: E402


class _FakeCredentials:
    def __init__(self, *, token="token-1", valid=True, expiry=None):
        self.token = token
        self.valid = valid
        self.expiry = expiry
        self.refresh_calls = 0

    def refresh(self, request):
        self.refresh_calls += 1
        self.token = f"token-{self.refresh_calls + 1}"
        self.valid = True
        self.expiry = datetime.utcnow() + timedelta(hours=1)


def _provider(credentials):
    return redis_iam.GCPIAMCredentialProvider(
        credentials_factory=lambda: credentials
    )


def test_returns_cached_token_before_refresh_margin():
    credentials = _FakeCredentials(
        expiry=datetime.utcnow() + timedelta(hours=1)
    )
    provider = _provider(credentials)

    assert provider.get_credentials() == ("token-1",)
    assert credentials.refresh_calls == 0


def test_refreshes_token_near_expiry():
    credentials = _FakeCredentials(
        expiry=datetime.utcnow() + timedelta(minutes=1)
    )
    provider = _provider(credentials)

    assert provider.get_credentials() == ("token-2",)
    assert credentials.refresh_calls == 1


def test_refreshes_invalid_credentials():
    credentials = _FakeCredentials(valid=False)
    provider = _provider(credentials)

    assert provider.get_credentials() == ("token-2",)
    assert credentials.refresh_calls == 1


def test_empty_token_raises():
    credentials = _FakeCredentials(
        token="", expiry=datetime.utcnow() + timedelta(hours=1)
    )
    provider = _provider(credentials)

    with pytest.raises(RuntimeError):
        provider.get_credentials()
