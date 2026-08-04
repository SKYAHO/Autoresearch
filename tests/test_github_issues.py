"""gh CLI 경계의 출력 파싱·저장소 검증·오류 분류를 고정한다.

전체 파이프라인에서 조립된 본문이 GitHub 이슈가 되는 구간만 검증한다. 본문 조립과 DB
저장은 이 모듈의 범위가 아니다. 실제 gh를 실행하지 않고 서브프로세스를 스텁으로 대체한다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from agent_orchestration.app.experiments.github_issues import (
    GitHubIssueError,
    IssueRef,
    create_issue,
    find_issue_by_marker,
)


@dataclass(frozen=True)
class _Settings:
    github_token: str = "x" * 40
    github_repository: str = "SKYAHO/Autoresearch"
    gh_timeout_sec: int = 5


class _FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.pid = 4242

    async def communicate(self, _stdin: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def _patch_subprocess(monkeypatch: pytest.MonkeyPatch, process: _FakeProcess) -> list:
    calls: list = []

    async def fake_exec(*command: str, **kwargs: object) -> _FakeProcess:
        calls.append(command)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls


def test_create_issue_parses_the_issue_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """gh issue create에는 --json이 없어 stdout URL을 파싱해야 한다."""
    _patch_subprocess(
        monkeypatch,
        _FakeProcess(b"https://github.com/SKYAHO/Autoresearch/issues/520\n", b"", 0),
    )

    ref = asyncio.run(
        create_issue(_Settings(), title="[AR] t", body="b", labels=("auto-experiment",))
    )

    assert ref == IssueRef(number=520, url="https://github.com/SKYAHO/Autoresearch/issues/520")


def test_create_issue_rejects_a_url_from_another_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """설정과 다른 저장소에 열린 이슈를 성공으로 기록하면 안 된다."""
    _patch_subprocess(
        monkeypatch,
        _FakeProcess(b"https://github.com/other/repo/issues/1\n", b"", 0),
    )

    with pytest.raises(GitHubIssueError, match="unexpected_repository"):
        asyncio.run(
            create_issue(_Settings(), title="[AR] t", body="b", labels=("auto-experiment",))
        )


def test_create_issue_passes_the_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """label이 빠지면 워크플로가 실패가 아니라 skip되어 흔적이 남지 않는다."""
    calls = _patch_subprocess(
        monkeypatch,
        _FakeProcess(b"https://github.com/SKYAHO/Autoresearch/issues/520\n", b"", 0),
    )

    asyncio.run(
        create_issue(_Settings(), title="[AR] t", body="b", labels=("auto-experiment",))
    )

    assert "--label" in calls[0]
    assert "auto-experiment" in calls[0]


def test_create_issue_classifies_authentication_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """사유를 분류해야 호출자가 무엇을 고쳐야 하는지 알 수 있다."""
    _patch_subprocess(
        monkeypatch, _FakeProcess(b"", b"gh: Bad credentials (HTTP 401)\n", 1)
    )

    with pytest.raises(GitHubIssueError, match="authentication_failed"):
        asyncio.run(
            create_issue(_Settings(), title="[AR] t", body="b", labels=("auto-experiment",))
        )


def test_create_issue_classifies_unknown_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """분류하지 못한 실패도 삼키지 않는다."""
    _patch_subprocess(monkeypatch, _FakeProcess(b"", b"something odd\n", 1))

    with pytest.raises(GitHubIssueError, match="unclassified"):
        asyncio.run(
            create_issue(_Settings(), title="[AR] t", body="b", labels=("auto-experiment",))
        )


def test_create_issue_separates_rate_limit_from_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub은 rate limit도 403으로 답한다. 둘을 묶으면 호출자가 오해한다.

    영구적 권한 문제를 `rate_limited`로 알리면 "기다리면 풀린다"로 읽힌다.
    """
    _patch_subprocess(
        monkeypatch,
        _FakeProcess(b"", b"gh: API rate limit exceeded (HTTP 403)\n", 1),
    )
    with pytest.raises(GitHubIssueError, match="rate_limited"):
        asyncio.run(
            create_issue(_Settings(), title="[AR] t", body="b", labels=("auto-experiment",))
        )

    _patch_subprocess(
        monkeypatch,
        _FakeProcess(b"", b"gh: Resource not accessible by integration (HTTP 403)\n", 1),
    )
    with pytest.raises(GitHubIssueError, match="permission_denied"):
        asyncio.run(
            create_issue(_Settings(), title="[AR] t", body="b", labels=("auto-experiment",))
        )


def test_cancellation_reclaims_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """상위 취소 시 `gh` 프로세스를 회수해야 한다.

    회수하지 않으면 shield된 task가 참조 없이 남고, 임시 디렉터리가 실행 중인 `gh`보다
    먼저 지워진다.
    """
    reclaimed: list[object] = []

    class _HangingProcess:
        returncode = None
        pid = 4242

        async def communicate(self, _stdin: bytes | None = None) -> tuple[bytes, bytes]:
            await asyncio.sleep(3600)
            return b"", b""

    process = _HangingProcess()
    _patch_subprocess(monkeypatch, process)
    monkeypatch.setattr(
        "agent_orchestration.app.experiments.github_issues._terminate_process_group",
        reclaimed.append,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            create_issue(
                _Settings(), title="[AR] t", body="b", labels=("auto-experiment",)
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert reclaimed == [process]


def test_find_issue_by_marker_returns_none_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """marker가 없으면 아직 발행되지 않은 것이다."""
    _patch_subprocess(monkeypatch, _FakeProcess(b"[]\n", b"", 0))

    found = asyncio.run(find_issue_by_marker(_Settings(), marker="<!-- experiment-id: x -->"))

    assert found is None


def test_find_issue_by_marker_returns_the_existing_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """발행 후 DB 쓰기가 실패한 경우의 복구 경로다."""
    _patch_subprocess(
        monkeypatch,
        _FakeProcess(
            b'[{"number": 520, "url": "https://github.com/SKYAHO/Autoresearch/issues/520"}]',
            b"",
            0,
        ),
    )

    found = asyncio.run(find_issue_by_marker(_Settings(), marker="<!-- experiment-id: x -->"))

    assert found == IssueRef(number=520, url="https://github.com/SKYAHO/Autoresearch/issues/520")


def test_token_is_not_passed_as_a_command_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """토큰이 명령행에 실리면 프로세스 목록에 노출된다."""
    calls = _patch_subprocess(
        monkeypatch,
        _FakeProcess(b"https://github.com/SKYAHO/Autoresearch/issues/520\n", b"", 0),
    )

    asyncio.run(
        create_issue(_Settings(), title="[AR] t", body="b", labels=("auto-experiment",))
    )

    assert not any("x" * 40 in argument for argument in calls[0])
