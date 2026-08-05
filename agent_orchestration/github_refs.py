"""GitHub Git refs 조회·생성 REST 경계.

[파이프라인]
API가 이슈 발행 전 `dev` 기준 SHA를 읽는 구간과 executor가 봉인된 SHA에 exp branch를
생성하는 구간에서 GitHub Git refs API를 호출한다.

[기능]
ref 조회의 404만 부재로 반환하고, 조회·생성 성공 응답의 commit SHA를 40자리 소문자로
검증한다. 다른 HTTP·응답 오류는 자격·응답 본문을 포함하지 않는 typed error로 정제한다.

[비책임]
installation token 발급(`github_app.py`), 기존 ref와 기준 SHA의 멱등성 판단(executor),
Git checkout·push와 GitHub Actions branch workflow.
"""

from __future__ import annotations

import re

import httpx


_GITHUB_API_URL = "https://api.github.com"
_API_VERSION = "2022-11-28"
_REQUEST_TIMEOUT_SEC = 30
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class GitHubRefError(RuntimeError):
    """Git ref REST 호출이 실패했거나 응답을 신뢰할 수 없다."""

    def __init__(self, reason: str, *, status_code: int | None = None) -> None:
        self.reason = reason
        self.status_code = status_code
        suffix = f" (status={status_code})" if status_code is not None else ""
        super().__init__(f"{reason}{suffix}")


def _response_sha(response: httpx.Response) -> str:
    """GitHub ref 응답의 object SHA를 엄격히 검증한다."""
    try:
        payload = response.json()
    except ValueError as error:
        raise GitHubRefError("invalid_response") from error
    sha = (
        payload.get("object", {}).get("sha")
        if isinstance(payload, dict) and isinstance(payload.get("object"), dict)
        else None
    )
    if not isinstance(sha, str) or _SHA_PATTERN.fullmatch(sha) is None:
        raise GitHubRefError("invalid_response")
    return sha


class GitHubRefs:
    """installation token으로 GitHub Git refs API를 호출한다."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def _request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        json_body: dict[str, str] | None = None,
    ) -> httpx.Response:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        try:
            async with httpx.AsyncClient(
                base_url=_GITHUB_API_URL,
                headers=headers,
                timeout=_REQUEST_TIMEOUT_SEC,
                transport=self._transport,
            ) as client:
                return await client.request(method, path, json=json_body)
        except httpx.HTTPError as error:
            raise GitHubRefError("request_failed") from error

    async def get_sha(self, repository: str, ref: str, token: str) -> str | None:
        """ref의 commit SHA를 반환하며, 404일 때만 `None`을 반환한다."""
        response = await self._request(
            "GET", f"/repos/{repository}/git/ref/{ref}", token
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise GitHubRefError("get_failed", status_code=response.status_code)
        return _response_sha(response)

    async def create(
        self,
        repository: str,
        ref: str,
        sha: str,
        token: str,
    ) -> str:
        """봉인된 SHA에 ref를 생성하고 GitHub가 반환한 SHA를 검증한다."""
        if _SHA_PATTERN.fullmatch(sha) is None:
            raise GitHubRefError("invalid_sha")
        response = await self._request(
            "POST",
            f"/repos/{repository}/git/refs",
            token,
            json_body={"ref": f"refs/{ref}", "sha": sha},
        )
        if response.status_code != 201:
            raise GitHubRefError("create_failed", status_code=response.status_code)
        created_sha = _response_sha(response)
        if created_sha != sha:
            raise GitHubRefError("unexpected_sha")
        return created_sha
