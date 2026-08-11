"""GitHub Pull Requests 생성·조회 REST 경계(#689).

[파이프라인]
완주한 실험의 exp branch를 dev로 향하는 Pull Request로 여는 구간에서 GitHub Pull
Requests API를 호출한다.

[기능]
생성 응답의 PR 번호를 검증하고, 422를 "이미 존재"와 "차이 없음"으로 갈라 호출자가
회복 가능한 상황과 그렇지 않은 상황을 구분하게 한다. 403은 App 권한 부재 신호로
따로 남긴다. 실패 사유에 token·응답 본문을 넣지 않는다.

[비책임]
installation token 발급(`github_app.py`), 어떤 실험이 대상인지 판정과 멱등 기록
(`launcher/pull_request.py`), Git checkout·push, App 권한 부여
(`SKYAHO/Autoresearch-infra#629`).
"""

from __future__ import annotations

import httpx


_GITHUB_API_URL = "https://api.github.com"
_API_VERSION = "2022-11-28"
_REQUEST_TIMEOUT_SEC = 30

# GitHub이 같은 head→base 중복을 거부할 때 쓰는 문구다. 422 하나에 서로 다른 상황이
# 섞여 오므로 본문으로 가른다 — 상태 코드만으로는 구분할 수 없다.
_ALREADY_EXISTS_MARKER = "already exists"
_NO_COMMITS_MARKER = "no commits between"


class GitHubPullRequestError(RuntimeError):
    """Pull Request REST 호출이 실패했거나 응답을 신뢰할 수 없다."""

    def __init__(self, reason: str, *, status_code: int | None = None) -> None:
        self.reason = reason
        self.status_code = status_code
        suffix = f" (status={status_code})" if status_code is not None else ""
        super().__init__(f"{reason}{suffix}")


def _error_messages(response: httpx.Response) -> str:
    """422 본문의 사유 문구만 소문자로 모은다. 값 자체는 예외에 싣지 않는다."""
    try:
        payload = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    errors = payload.get("errors")
    messages = [payload.get("message")] if isinstance(payload.get("message"), str) else []
    if isinstance(errors, list):
        messages.extend(
            entry.get("message")
            for entry in errors
            if isinstance(entry, dict) and isinstance(entry.get("message"), str)
        )
    return " ".join(message for message in messages if message).lower()


def _response_number(response: httpx.Response) -> int:
    """생성 응답의 PR 번호를 엄격히 읽는다.

    못 읽으면 기록할 수 없다. 0이나 `None`으로 넘기면 멱등 판정이 깨져 다음 주기에
    같은 PR을 또 만들려 한다.
    """
    try:
        payload = response.json()
    except ValueError as error:
        raise GitHubPullRequestError("invalid_response") from error
    number = payload.get("number") if isinstance(payload, dict) else None
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise GitHubPullRequestError("invalid_response")
    return number


class GitHubPullRequests:
    """installation token으로 GitHub Pull Requests API를 호출한다."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def _request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        json_body: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
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
                return await client.request(
                    method, path, json=json_body, params=params
                )
        except httpx.HTTPError as error:
            raise GitHubPullRequestError("request_failed") from error

    async def create(
        self,
        repository: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        token: str,
    ) -> int:
        """Pull Request를 만들고 번호를 반환한다.

        422는 두 상황이 같은 코드로 온다. `pull_request_exists`는 먼저 만들고 기록
        전에 죽은 흔적이라 기존 번호를 조회해 회복할 수 있고,
        `pull_request_no_commits`는 올릴 변경이 없어 조회해도 소용이 없다.
        """
        response = await self._request(
            "POST",
            f"/repos/{repository}/pulls",
            token,
            json_body={"head": head, "base": base, "title": title, "body": body},
        )
        if response.status_code == 201:
            return _response_number(response)
        if response.status_code == 403:
            # App에 `Pull requests: write`가 없다. 기다려서 낫는 종류가 아니다.
            raise GitHubPullRequestError(
                "pull_request_forbidden", status_code=response.status_code
            )
        if response.status_code == 422:
            messages = _error_messages(response)
            if _NO_COMMITS_MARKER in messages:
                raise GitHubPullRequestError(
                    "pull_request_no_commits", status_code=response.status_code
                )
            if _ALREADY_EXISTS_MARKER in messages:
                raise GitHubPullRequestError(
                    "pull_request_exists", status_code=response.status_code
                )
        raise GitHubPullRequestError(
            "pull_request_create_failed", status_code=response.status_code
        )

    async def find_open(
        self,
        repository: str,
        *,
        head: str,
        token: str,
    ) -> int | None:
        """열려 있는 PR 번호를 찾는다. 없으면 `None`이다.

        `create`가 `pull_request_exists`를 낸 뒤 기록을 채우는 용도다. 멱등 판정
        자체를 이 조회로 대신하지 않는다 — 사람이 PR을 닫으면 다시 만들게 된다
        (정본 계약 §결정 3).
        """
        owner = repository.split("/", 1)[0]
        response = await self._request(
            "GET",
            f"/repos/{repository}/pulls",
            token,
            params={"head": f"{owner}:{head}", "state": "open"},
        )
        if response.status_code != 200:
            raise GitHubPullRequestError(
                "pull_request_lookup_failed", status_code=response.status_code
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise GitHubPullRequestError("invalid_response") from error
        if not isinstance(payload, list) or not payload:
            return None
        first = payload[0]
        number = first.get("number") if isinstance(first, dict) else None
        if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
            raise GitHubPullRequestError("invalid_response")
        return number
