"""GitHub App JWT와 installation token 발급의 최소 권한·비노출 계약을 검증한다.

전체 파이프라인에서 API/executor가 짧은 수명의 GitHub installation token을 얻는 인증
경계를 담당한다. 이슈 발행, Git ref 처리와 private key 배포는 검증하지 않는다.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import pytest

from applications.experiment_platform.shared.github_app import (
    GitHubAppCredentials,
    GitHubAppError,
    create_installation_token,
)


class RecordingTransport(httpx.AsyncBaseTransport):
    """한 요청을 기록하고 준비된 GitHub 응답을 반환한다."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.request: httpx.Request | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        return self.response


@pytest.fixture(scope="module")
def test_rsa_private_key() -> str:
    """실제 App key와 무관한 테스트 전용 즉석 RSA key."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def _jwt_json(segment: str) -> dict[str, object]:
    padding = "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment + padding))


def test_installation_token_requests_only_supplied_permissions(
    tmp_path: Path, test_rsa_private_key: str
) -> None:
    key_path = tmp_path / "app.pem"
    key_path.write_text(test_rsa_private_key, encoding="utf-8")
    transport = RecordingTransport(
        httpx.Response(
            201,
            json={"token": "secret-token", "expires_at": "2026-08-05T01:00:00Z"},
        )
    )
    token = asyncio.run(
        create_installation_token(
            GitHubAppCredentials(123, 456, key_path),
            permissions={"contents": "read"},
            transport=transport,
        )
    )

    assert transport.request is not None
    assert transport.request.url.path == "/app/installations/456/access_tokens"
    assert json.loads(transport.request.content) == {
        "permissions": {"contents": "read"}
    }
    assert token.value == "secret-token"
    assert token.expires_at == datetime(2026, 8, 5, 1, 0, tzinfo=UTC)
    assert "secret-token" not in repr(token)


def test_app_jwt_uses_the_required_claim_window(
    tmp_path: Path, test_rsa_private_key: str
) -> None:
    key_path = tmp_path / "app.pem"
    key_path.write_text(test_rsa_private_key, encoding="utf-8")
    transport = RecordingTransport(
        httpx.Response(
            201,
            json={"token": "secret-token", "expires_at": "2026-08-05T01:00:00Z"},
        )
    )

    before = int(datetime.now(UTC).timestamp())
    asyncio.run(
        create_installation_token(
            GitHubAppCredentials(123, 456, key_path),
            permissions={"contents": "read"},
            transport=transport,
        )
    )
    after = int(datetime.now(UTC).timestamp())

    assert transport.request is not None
    encoded = transport.request.headers["Authorization"].removeprefix("Bearer ")
    header_segment, payload_segment, _signature = encoded.split(".")
    header = _jwt_json(header_segment)
    payload = _jwt_json(payload_segment)
    assert header["alg"] == "RS256"
    assert payload["iss"] == "123"
    assert payload["exp"] - payload["iat"] == 600
    assert before - 60 <= payload["iat"] <= after - 60


def test_installation_token_error_does_not_expose_credentials_or_body(
    tmp_path: Path, test_rsa_private_key: str
) -> None:
    key_path = tmp_path / "app.pem"
    key_path.write_text(test_rsa_private_key, encoding="utf-8")
    transport = RecordingTransport(
        httpx.Response(403, text="private-response-body secret-token")
    )

    with pytest.raises(GitHubAppError) as captured:
        asyncio.run(
            create_installation_token(
                GitHubAppCredentials(123, 456, key_path),
                permissions={"contents": "read"},
                transport=transport,
            )
        )

    message = str(captured.value)
    assert "private-response-body" not in message
    assert "secret-token" not in message
    assert "Authorization" not in message
    assert test_rsa_private_key not in message
