"""Executor의 이슈 검증·무자격증명 workspace 준비 계약을 검증한다.

전체 파이프라인에서 branch creator 뒤와 Codex worker 앞의 경계다. 실제 GitHub 네트워크와
Codex 실행은 대체하되, 봉인 검증 전 clone 차단, clean remote, state 파일 권한과 재검증은
관찰 가능한 결과로 고정한다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import stat
import uuid

import httpx
import pytest

from applications.experiment_platform.executor.github_issues import GitHubIssueSnapshot, GitHubIssues
from applications.experiment_platform.executor.state import (
    ExecutorWorkspaceState,
    ExecutorWorkspaceStateError,
    read_state,
    write_state,
)
from applications.experiment_platform.executor.workspace import (
    WorkspacePrepareInput,
    prepare_workspace,
)


_EXPERIMENT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
_ISSUE_NUMBER = 546
_ISSUE_TITLE = "[AR] executor bootstrap"
_ISSUE_BRANCH = "exp/546-executor-bootstrap"
_BASE_SHA = "a" * 40
_TOKEN = "clone-token-must-not-leak"


def _issue_body() -> str:
    fixture = (
        Path(__file__).resolve().parents[2] / "fixtures" / "auto_research_issue_form_rendered.md"
    ).read_text(encoding="utf-8")
    return f"<!-- experiment-id: {_EXPERIMENT_ID} -->\n\n{fixture}"


def _input(tmp_path: Path) -> WorkspacePrepareInput:
    token_file = tmp_path / "clone-token"
    token_file.write_text(f"{_TOKEN}\n", encoding="utf-8")
    return WorkspacePrepareInput(
        experiment_id=_EXPERIMENT_ID,
        issue_number=_ISSUE_NUMBER,
        issue_branch=_ISSUE_BRANCH,
        base_dev_sha=_BASE_SHA,
        github_repository="SKYAHO/Autoresearch",
        token_file=token_file,
        workspace=tmp_path / "workspace",
    )


@dataclass
class _Issues:
    snapshot: GitHubIssueSnapshot

    async def get(
        self, repository: str, issue_number: int, token: str
    ) -> GitHubIssueSnapshot:
        assert repository == "SKYAHO/Autoresearch"
        assert issue_number == _ISSUE_NUMBER
        assert token == _TOKEN
        return self.snapshot


class _Process:
    def __init__(self, stdout: bytes, returncode: int = 0) -> None:
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, b""


def _patch_git(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workspace: Path,
    remote_tip: str = _BASE_SHA,
) -> tuple[list[tuple[str, ...]], list[Path]]:
    commands: list[tuple[str, ...]] = []
    askpass_files: list[Path] = []

    async def fake_exec(*command: str, **kwargs: object) -> _Process:
        commands.append(command)
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        assert _TOKEN not in " ".join(command)
        askpass_path = Path(str(environment["GIT_ASKPASS"]))
        assert _TOKEN not in askpass_path.read_text(encoding="utf-8")
        askpass_files.append(askpass_path)
        if command[1:3] == ("clone", "--no-checkout"):
            (workspace / "repository").mkdir(parents=True)
            return _Process(b"")
        if command[-2:] == ("rev-parse", "HEAD"):
            return _Process(remote_tip.encode())
        if command[-2:] == ("rev-parse", f"origin/{_ISSUE_BRANCH}"):
            return _Process(remote_tip.encode())
        if command[-2:] == ("config", "--get"):
            return _Process(b"", returncode=1)
        if command[-3:] == ("config", "--get", "remote.origin.url"):
            return _Process(b"https://github.com/SKYAHO/Autoresearch.git\n")
        if command[-3:] == ("config", "--get", "core.hooksPath"):
            return _Process(b"/dev/null\n")
        return _Process(b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return commands, askpass_files


def test_prepared_workspace_uses_clean_remote_writes_0400_state_and_removes_askpass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """token URL/helper를 남기면 이후 Codex 경계까지 자격증명이 전파될 수 있다."""
    config = _input(tmp_path)
    state_path = tmp_path / "executor-state" / "state.json"
    monkeypatch.setattr("applications.experiment_platform.executor.workspace.STATE_PATH", state_path)
    commands, askpass_files = _patch_git(monkeypatch, workspace=config.workspace)

    prepared = asyncio.run(
        prepare_workspace(
            config,
            _Issues(GitHubIssueSnapshot(title=_ISSUE_TITLE, body=_issue_body())),
        )
    )

    assert prepared.repository == config.workspace / "repository"
    assert prepared.remote_tip == _BASE_SHA
    assert prepared.allowed_scope == ()
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o400
    assert read_state(state_path, workspace=config.workspace).remote_tip == _BASE_SHA
    assert all(not path.exists() for path in askpass_files)
    assert all(_TOKEN not in " ".join(command) for command in commands)
    assert ("git", "-C", str(prepared.repository), "config", "--get", "credential.helper") in commands
    clone = commands.index(
        (
            "git",
            "clone",
            "--no-checkout",
            "--origin",
            "origin",
            "https://github.com/SKYAHO/Autoresearch.git",
            str(prepared.repository),
        )
    )
    hooks_set = commands.index(
        ("git", "-C", str(prepared.repository), "config", "core.hooksPath", "/dev/null")
    )
    remote = commands.index(
        ("git", "-C", str(prepared.repository), "config", "--get", "remote.origin.url")
    )
    helper = commands.index(
        ("git", "-C", str(prepared.repository), "config", "--get", "credential.helper")
    )
    hooks = commands.index(
        ("git", "-C", str(prepared.repository), "config", "--get", "core.hooksPath")
    )
    checkout = commands.index(
        ("git", "-C", str(prepared.repository), "checkout", "--detach", f"origin/{_ISSUE_BRANCH}")
    )
    switch = commands.index(
        ("git", "-C", str(prepared.repository), "switch", "-c", _ISSUE_BRANCH)
    )
    assert clone < hooks_set < remote < helper < hooks < checkout < switch


def test_free_form_issue_body_is_forwarded_without_semantic_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue template이 바뀌어도 본문 의미 검증 때문에 clone을 막으면 안 된다."""
    body = "NaN 또는 Infinity ctr_score를 거부하도록 코드를 수정한다."
    config = _input(tmp_path)
    state_path = tmp_path / "executor-state" / "state.json"
    monkeypatch.setattr("applications.experiment_platform.executor.workspace.STATE_PATH", state_path)
    commands, _ = _patch_git(monkeypatch, workspace=config.workspace)

    prepared = asyncio.run(
        prepare_workspace(
            config,
            _Issues(GitHubIssueSnapshot(title="자유 형식 이슈", body=body)),
        )
    )

    assert prepared.issue_body == body
    assert prepared.allowed_scope == ()
    assert any(command[1:3] == ("clone", "--no-checkout") for command in commands)
    assert read_state(state_path, workspace=config.workspace).issue_body == body


def test_existing_remote_candidate_is_prepared_for_later_adoption_without_running_codex(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """base가 아닌 tip은 충돌이 아니라 Stage 5가 재검증할 채택 후보여야 한다."""
    config = _input(tmp_path)
    state_path = tmp_path / "executor-state" / "state.json"
    monkeypatch.setattr("applications.experiment_platform.executor.workspace.STATE_PATH", state_path)
    remote_tip = "b" * 40
    _patch_git(monkeypatch, workspace=config.workspace, remote_tip=remote_tip)

    prepared = asyncio.run(
        prepare_workspace(
            config,
            _Issues(GitHubIssueSnapshot(title=_ISSUE_TITLE, body=_issue_body())),
        )
    )

    assert prepared.remote_tip == remote_tip
    assert read_state(state_path, workspace=config.workspace).remote_tip == remote_tip


def test_state_read_rejects_repository_outside_the_fixed_workspace(tmp_path: Path) -> None:
    """state를 바꿔 Codex가 임의 경로를 열게 만들면 안 된다."""
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    state_path = tmp_path / "state" / "state.json"
    state = ExecutorWorkspaceState(
        schema_version=1,
        repository=repository,
        issue_body="body",
        allowed_scope=("prod_model_contract",),
        base_dev_sha=_BASE_SHA,
        remote_tip=_BASE_SHA,
    )
    write_state(state_path, state, workspace=workspace)
    state_path.chmod(0o600)
    state_path.write_text(
        json.dumps(
            {
                **json.loads(state_path.read_text(encoding="utf-8")),
                "repository": str(tmp_path / "outside"),
            }
        ),
        encoding="utf-8",
    )
    state_path.chmod(0o400)

    with pytest.raises(ExecutorWorkspaceStateError, match="repository"):
        read_state(state_path, workspace=workspace)


@pytest.mark.parametrize("invalid_version", [True, None, "1", 1.0, 2])
def test_state_read_rejects_non_integer_schema_version(
    tmp_path: Path, invalid_version: object
) -> None:
    """JSON true는 Python에서 1과 같으므로 명시적인 타입 검증이 필요하다."""
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    state_path = tmp_path / "state" / "state.json"
    state = ExecutorWorkspaceState(
        schema_version=1,
        repository=repository,
        issue_body="body",
        allowed_scope=(),
        base_dev_sha=_BASE_SHA,
        remote_tip=_BASE_SHA,
    )
    write_state(state_path, state, workspace=workspace)
    state_path.chmod(0o600)
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["schema_version"] = invalid_version
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    state_path.chmod(0o400)

    with pytest.raises(ExecutorWorkspaceStateError, match="schema_version"):
        read_state(state_path, workspace=workspace)


class _RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.request: httpx.Request | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        return self.response


def test_github_issue_adapter_reads_only_the_sealed_issue() -> None:
    """목록·검색 API를 쓰면 본문 검증의 대상 이슈가 흔들릴 수 있다."""
    transport = _RecordingTransport(
        httpx.Response(200, json={"title": _ISSUE_TITLE, "body": _issue_body()})
    )

    snapshot = asyncio.run(
        GitHubIssues(transport=transport).get(
            "SKYAHO/Autoresearch", _ISSUE_NUMBER, _TOKEN
        )
    )

    assert snapshot.title == _ISSUE_TITLE
    assert transport.request is not None
    assert transport.request.method == "GET"
    assert transport.request.url.path == "/repos/SKYAHO/Autoresearch/issues/546"
    assert _TOKEN not in str(transport.request.url)
