"""Executor의 read-only GitHub issue 조회 경계.

[파이프라인]
봉인 branch가 준비된 뒤 workspace-preparer가 DB에 봉인한 이슈 본문을 원격 GitHub 원문과
대조하기 전에, 해당 단일 이슈를 읽는 구간을 담당한다.

[기능]
installation token으로 특정 repository/issue 번호의 제목과 body만 GET하고, HTTP·응답
형식 실패를 응답 본문과 token을 포함하지 않는 typed error로 정제한다.

[비책임]
이슈 발행(`app/experiments/github_issues.py`), Issue Form 파싱(`tools/auto_research_issue_branch.py`),
branch 생성과 Git checkout은 담당하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


_GITHUB_API_URL = "https://api.github.com"
_API_VERSION = "2022-11-28"
_REQUEST_TIMEOUT_SEC = 30


class ExecutorGitHubIssueError(RuntimeError):
    """issue 조회가 실패했거나 GitHub 응답을 신뢰할 수 없다."""

    def __init__(self, reason: str, *, status_code: int | None = None) -> None:
        self.reason = reason
        self.status_code = status_code
        suffix = f" (status={status_code})" if status_code is not None else ""
        super().__init__(f"{reason}{suffix}")


@dataclass(frozen=True)
class GitHubIssueSnapshot:
    """GitHub에서 읽은 단일 이슈의 검증 전 원문."""

    title: str
    body: str


class GitHubIssues:
    """installation token으로 단일 GitHub Issue를 read-only 조회한다."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def get(
        self,
        repository: str,
        issue_number: int,
        token: str,
    ) -> GitHubIssueSnapshot:
        """정확한 이슈 URL에서 제목과 본문을 읽어 반환한다."""
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
                response = await client.get(
                    f"/repos/{repository}/issues/{issue_number}"
                )
        except httpx.HTTPError as error:
            raise ExecutorGitHubIssueError("request_failed") from error
        if response.status_code != 200:
            raise ExecutorGitHubIssueError(
                "get_failed", status_code=response.status_code
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise ExecutorGitHubIssueError("invalid_response") from error
        if not isinstance(payload, dict):
            raise ExecutorGitHubIssueError("invalid_response")
        title = payload.get("title")
        body = payload.get("body")
        if not isinstance(title, str) or not isinstance(body, str):
            raise ExecutorGitHubIssueError("invalid_response")
        return GitHubIssueSnapshot(title=title, body=body)
