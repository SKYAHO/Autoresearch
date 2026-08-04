"""Auto Research 이슈 발행의 GitHub CLI 경계.

[파이프라인]
조립된 Issue Form 본문이 실제 GitHub 이슈가 되는 구간을 담당한다. 본문 조립은
issue_authoring, DB 기록과 멱등성 판단은 service의 책임이다.

[기능]
`gh issue create`/`gh issue list`를 요청별 임시 홈에서 실행하고, 성공 시 stdout의 이슈
URL을 설정된 저장소와 대조해 파싱하며, 실패 사유를 분류해 올린다. 시간 초과 시 프로세스
그룹을 회수한다.

[비책임]
자격 증명의 발급·보관(Autoresearch-infra), 재시도 판단(service), 이슈 본문의 내용.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from tempfile import TemporaryDirectory
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class _Settings(Protocol):
    github_token: str
    github_repository: str
    gh_timeout_sec: int


class IssueRef(BaseModel):
    """발행되었거나 이미 존재하는 이슈의 좌표."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    number: int
    url: str


class GitHubIssueError(RuntimeError):
    """`gh` 호출이 실패했거나 결과를 신뢰할 수 없다."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


# stderr 문자열 기반 분류다. gh 버전을 이미지에 고정해야 조용히 깨지지 않는다.
_REASON_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"bad credentials|authentication|HTTP 401", "authentication_failed"),
    (r"HTTP 403|rate limit|api rate", "rate_limited"),
    (r"could not add label|not found.*label|label.*not found", "label_missing"),
    (r"HTTP 404|could not resolve to a Repository", "repository_not_found"),
    (r"dial tcp|connection refused|timeout|network", "network_error"),
)


def _environment(token: str, home: str) -> dict[str, str]:
    """`gh`가 필요로 하는 최소 환경만 하위 프로세스에 전달한다."""
    # 토큰은 명령행이 아니라 환경으로만 넘긴다 — 명령행은 프로세스 목록에 노출된다.
    return {
        "GH_TOKEN": token,
        "GH_CONFIG_DIR": home,
        "HOME": home,
        "TMPDIR": home,
        "PATH": os.environ.get("PATH", ""),
        "GH_NO_UPDATE_NOTIFIER": "1",
        "GH_PROMPT_DISABLED": "1",
    }


def _classify(stderr: str) -> str:
    for pattern, reason in _REASON_PATTERNS:
        if re.search(pattern, stderr, re.IGNORECASE):
            return reason
    return "unclassified"


def _terminate(process: object) -> None:
    """`gh`와 같은 세션의 하위 프로세스를 함께 회수한다."""
    pid = getattr(process, "pid", None)
    if os.name == "posix" and pid is not None:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return


async def _run_gh(settings: _Settings, arguments: tuple[str, ...]) -> str:
    """`gh`를 격리 실행하고 stdout을 반환한다."""
    with TemporaryDirectory(prefix="agent-orchestration-gh-") as home:
        try:
            process = await asyncio.create_subprocess_exec(
                "gh",
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_environment(settings.github_token, home),
                start_new_session=True,
            )
        except OSError as error:
            raise GitHubIssueError("gh_unavailable", str(error)) from error

        task = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(task), timeout=settings.gh_timeout_sec
            )
        except TimeoutError as error:
            _terminate(process)
            await asyncio.gather(task, return_exceptions=True)
            raise GitHubIssueError("timeout") from error

        if process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise GitHubIssueError(_classify(message), message)
        return stdout.decode("utf-8", errors="replace").strip()


def _parse_issue_url(url: str, repository: str) -> IssueRef:
    """URL이 설정된 저장소를 가리킬 때만 이슈 번호를 인정한다."""
    match = re.fullmatch(
        r"https://github\.com/([^/]+/[^/]+)/issues/([1-9][0-9]*)", url.strip()
    )
    if match is None:
        raise GitHubIssueError("unparsable_output", url)
    if match.group(1) != repository:
        raise GitHubIssueError("unexpected_repository", url)
    return IssueRef(number=int(match.group(2)), url=url.strip())


async def create_issue(
    settings: _Settings,
    *,
    title: str,
    body: str,
    labels: tuple[str, ...],
) -> IssueRef:
    """본문과 label로 이슈를 발행하고 그 좌표를 반환한다."""
    with TemporaryDirectory(prefix="agent-orchestration-body-") as workdir:
        body_path = os.path.join(workdir, "body.md")
        with open(body_path, "w", encoding="utf-8") as handle:
            handle.write(body)
        arguments = [
            "issue",
            "create",
            "--repo",
            settings.github_repository,
            "--title",
            title,
            "--body-file",
            body_path,
        ]
        for label in labels:
            arguments.extend(["--label", label])
        stdout = await _run_gh(settings, tuple(arguments))
    return _parse_issue_url(stdout.splitlines()[-1] if stdout else "", settings.github_repository)


async def find_issue_by_marker(settings: _Settings, *, marker: str) -> IssueRef | None:
    """본문 marker로 이미 발행된 이슈를 찾는다(발행 후 DB 쓰기 실패 복구용)."""
    stdout = await _run_gh(
        settings,
        (
            "issue",
            "list",
            "--repo",
            settings.github_repository,
            "--state",
            "all",
            "--search",
            marker,
            "--json",
            "number,url",
            "--limit",
            "5",
        ),
    )
    try:
        rows = json.loads(stdout or "[]")
    except json.JSONDecodeError as error:
        raise GitHubIssueError("unparsable_output", stdout) from error
    if not rows:
        return None
    return _parse_issue_url(str(rows[0]["url"]), settings.github_repository)
