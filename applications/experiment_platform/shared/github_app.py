"""GitHub App installation token 발급 경계.

[파이프라인]
Agent Orchestration API의 기준 SHA 조회와 executor의 branch 생성 REST 호출 직전에,
각 역할의 GitHub App private key를 짧은 수명의 installation token으로 교환하는 구간이다.

[기능]
요구된 claim 창으로 RS256 App JWT를 서명하고, 호출자가 명시한 repository permission만
token endpoint에 요청한다. 응답 token과 만료 시각을 검증하며 오류는 자격·응답 본문 없이
정제한다.

[비책임]
private key의 배포·mount(Autoresearch-infra), Git ref 조회·생성(`github_refs.py`),
이슈 발행(`app/experiments/github_issues.py`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jwt


_GITHUB_API_URL = "https://api.github.com"
_API_VERSION = "2022-11-28"
_REQUEST_TIMEOUT_SEC = 30


@dataclass(frozen=True)
class GitHubAppCredentials:
    """GitHub App JWT 서명과 installation 선택에 필요한 좌표."""

    app_id: int
    installation_id: int
    private_key_path: Path


@dataclass(frozen=True)
class InstallationToken:
    """GitHub installation token과 만료 시각.

    token 값은 객체 repr에서 숨겨 예외·진단 출력에 우발적으로 섞이지 않게 한다.
    """

    value: str = field(repr=False)
    expires_at: datetime


class GitHubAppError(RuntimeError):
    """GitHub App 인증 경계가 정제된 사유와 함께 실패했다."""

    def __init__(self, reason: str, *, status_code: int | None = None) -> None:
        self.reason = reason
        self.status_code = status_code
        suffix = f" (status={status_code})" if status_code is not None else ""
        super().__init__(f"{reason}{suffix}")


def _app_jwt(credentials: GitHubAppCredentials, now: datetime) -> str:
    """GitHub이 요구하는 10분 claim 창의 App JWT를 서명한다."""
    if credentials.app_id < 1 or credentials.installation_id < 1:
        raise GitHubAppError("invalid_credentials")
    try:
        private_key = credentials.private_key_path.read_text(encoding="utf-8")
    except OSError as error:
        raise GitHubAppError("private_key_unavailable") from error
    timestamp = int(now.timestamp())
    try:
        return jwt.encode(
            {
                "iat": timestamp - 60,
                "exp": timestamp + 540,
                "iss": str(credentials.app_id),
            },
            private_key,
            algorithm="RS256",
        )
    except (jwt.PyJWTError, TypeError, ValueError) as error:
        raise GitHubAppError("jwt_signing_failed") from error


def _parse_expiration(value: object) -> datetime:
    """GitHub의 RFC 3339 만료 시각을 timezone-aware datetime으로 검증한다."""
    if not isinstance(value, str):
        raise GitHubAppError("invalid_response")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise GitHubAppError("invalid_response") from error
    if parsed.tzinfo is None:
        raise GitHubAppError("invalid_response")
    return parsed.astimezone(UTC)


async def create_installation_token(
    credentials: GitHubAppCredentials,
    *,
    permissions: Mapping[str, str],
    transport: httpx.AsyncBaseTransport | None = None,
) -> InstallationToken:
    """호출자가 지정한 permission만 가진 installation token을 발급한다."""
    encoded_jwt = _app_jwt(credentials, datetime.now(UTC))
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {encoded_jwt}",
        "X-GitHub-Api-Version": _API_VERSION,
    }
    try:
        async with httpx.AsyncClient(
            base_url=_GITHUB_API_URL,
            headers=headers,
            timeout=_REQUEST_TIMEOUT_SEC,
            transport=transport,
        ) as client:
            response = await client.post(
                f"/app/installations/{credentials.installation_id}/access_tokens",
                json={"permissions": dict(permissions)},
            )
    except httpx.HTTPError as error:
        raise GitHubAppError("request_failed") from error

    if response.status_code != 201:
        raise GitHubAppError("token_request_failed", status_code=response.status_code)
    try:
        payload = response.json()
    except ValueError as error:
        raise GitHubAppError("invalid_response") from error
    token = payload.get("token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise GitHubAppError("invalid_response")
    expires_at = _parse_expiration(payload.get("expires_at"))
    return InstallationToken(value=token, expires_at=expires_at)
