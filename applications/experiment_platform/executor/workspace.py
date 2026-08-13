"""Executor의 raw 이슈 전달과 credential-free workspace 준비 경계.

[파이프라인]
branch-creator가 원격 exp ref를 보장한 뒤, Codex가 수정하기 전에 GitHub 이슈 원문과
정확한 branch checkout을 workspace emptyDir에 준비하는 구간이다.

[기능]
Issue Form 내용을 해석하지 않고 일회성 GIT_ASKPASS와 clean remote URL로 checkout한다.
성공 결과는 별도 state volume에 0400 state로 전달한다.

[비책임]
branch 생성(`branch_creator.py`), Codex 실행(Stage 3), candidate diff 검증·commit·push
(Stage 4/5), Secret·volume mount 정책(Autoresearch-infra)은 담당하지 않는다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import stat
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Protocol
import uuid

from applications.experiment_platform.executor.github_issues import GitHubIssueSnapshot
from applications.experiment_platform.executor.state import ExecutorWorkspaceState, write_state


_SHA_LENGTH = 40
_STATE_PATH = Path("/var/run/executor-state/state.json")
STATE_PATH = _STATE_PATH


class WorkspacePreparationError(RuntimeError):
    """workspace 준비가 신뢰·Git 경계를 만족하지 못했다."""


class IssueClient(Protocol):
    """workspace 준비에 필요한 단일 GitHub issue read contract."""

    async def get(
        self,
        repository: str,
        issue_number: int,
        token: str,
    ) -> GitHubIssueSnapshot: ...


@dataclass(frozen=True)
class WorkspacePrepareInput:
    """DB·launcher가 workspace-preparer에 전달한 실행 좌표."""

    experiment_id: uuid.UUID
    issue_number: int
    issue_branch: str
    base_dev_sha: str
    github_repository: str
    token_file: Path
    workspace: Path


@dataclass(frozen=True)
class PreparedWorkspace:
    """검증된 checkout과 Stage 3/5가 사용할 raw 이슈 입력."""

    repository: Path
    issue_body: str
    allowed_scope: tuple[str, ...]
    remote_tip: str


def _read_token(path: Path) -> str:
    """clone 전용 regular token 파일을 읽되 값 자체는 오류에 넣지 않는다."""
    try:
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode):
            raise WorkspacePreparationError("token_file_invalid")
        token = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise WorkspacePreparationError("token_file_unavailable") from error
    if not token:
        raise WorkspacePreparationError("token_file_empty")
    return token


def _clean_remote_url(repository: str) -> str:
    """token을 포함할 수 없는 GitHub HTTPS remote URL을 반환한다."""
    return f"https://github.com/{repository}.git"


async def _run_git(
    arguments: tuple[str, ...],
    *,
    environment: dict[str, str],
    allow_failure: bool = False,
) -> str:
    """argv list로만 Git을 호출하고 stderr·token을 오류로 올리지 않는다."""
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
        )
    except OSError as error:
        raise WorkspacePreparationError("git_unavailable") from error
    stdout, _stderr = await process.communicate()
    if process.returncode != 0 and not allow_failure:
        raise WorkspacePreparationError("git_failed")
    return stdout.decode("utf-8", errors="replace").strip()


def _git_environment(token: str, askpass: Path, home: str) -> dict[str, str]:
    """clone subprocess에만 필요한 token과 빈 Git config home을 전달한다."""
    return {
        "GIT_ASKPASS": str(askpass),
        "GITHUB_TOKEN": token,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": home,
        "XDG_CONFIG_HOME": home,
        "PATH": os.environ.get("PATH", ""),
    }


def _askpass_file(directory: Path) -> Path:
    """token은 환경에서만 읽는 일회성 askpass script를 생성한다."""
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=directory, prefix="git-askpass-", delete=False
    ) as handle:
        path = Path(handle.name)
        handle.write(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
            "  *) printf '%s\\n' \"$GITHUB_TOKEN\" ;;\n"
            "esac\n"
        )
    path.chmod(0o700)
    return path


def _sha(value: str, reason: str) -> str:
    """Git stdout의 lower-case commit SHA만 신뢰한다."""
    if len(value) != _SHA_LENGTH or any(character not in "0123456789abcdef" for character in value):
        raise WorkspacePreparationError(reason)
    return value


async def _checkout(
    config: WorkspacePrepareInput,
    *,
    token: str,
) -> tuple[Path, str]:
    """clean remote에서 issue branch를 checkout하고 origin tip과 HEAD를 대조한다."""
    workspace = config.workspace.resolve()
    if not workspace.is_absolute():
        raise WorkspacePreparationError("workspace_invalid")
    repository = workspace / "repository"
    if repository.exists():
        raise WorkspacePreparationError("workspace_repository_exists")
    workspace.mkdir(parents=True, exist_ok=True)
    clean_url = _clean_remote_url(config.github_repository)
    with TemporaryDirectory(prefix="executor-git-home-") as home:
        askpass = _askpass_file(Path(home))
        environment = _git_environment(token, askpass, home)
        try:
            await _run_git(
                ("clone", "--no-checkout", "--origin", "origin", clean_url, str(repository)),
                environment=environment,
            )
            await _run_git(
                ("-C", str(repository), "config", "core.hooksPath", "/dev/null"),
                environment=environment,
            )
            remote = await _run_git(
                ("-C", str(repository), "config", "--get", "remote.origin.url"),
                environment=environment,
            )
            if remote != clean_url:
                raise WorkspacePreparationError("remote_url_invalid")
            helper = await _run_git(
                ("-C", str(repository), "config", "--get", "credential.helper"),
                environment=environment,
                allow_failure=True,
            )
            if helper:
                raise WorkspacePreparationError("credential_helper_present")
            hooks_path = await _run_git(
                ("-C", str(repository), "config", "--get", "core.hooksPath"),
                environment=environment,
            )
            if hooks_path != "/dev/null":
                raise WorkspacePreparationError("hooks_path_invalid")
            await _run_git(
                ("-C", str(repository), "checkout", "--detach", f"origin/{config.issue_branch}"),
                environment=environment,
            )
            await _run_git(
                ("-C", str(repository), "switch", "-c", config.issue_branch),
                environment=environment,
            )
            head = _sha(
                await _run_git(
                    ("-C", str(repository), "rev-parse", "HEAD"), environment=environment
                ),
                "head_invalid",
            )
            remote_tip = _sha(
                await _run_git(
                    ("-C", str(repository), "rev-parse", f"origin/{config.issue_branch}"),
                    environment=environment,
                ),
                "remote_tip_invalid",
            )
            if head != remote_tip:
                raise WorkspacePreparationError("head_remote_mismatch")
            return repository, remote_tip
        finally:
            askpass.unlink(missing_ok=True)


async def prepare_workspace(
    config: WorkspacePrepareInput,
    issues: IssueClient,
) -> PreparedWorkspace:
    """raw 이슈 본문과 안전한 checkout/state를 준비한다."""
    token = _read_token(config.token_file)
    snapshot = await issues.get(config.github_repository, config.issue_number, token)
    repository, remote_tip = await _checkout(config, token=token)
    prepared = PreparedWorkspace(
        repository=repository,
        issue_body=snapshot.body,
        allowed_scope=(),
        remote_tip=remote_tip,
    )
    write_state(
        STATE_PATH,
        ExecutorWorkspaceState(
            schema_version=1,
            repository=repository,
            issue_body=snapshot.body,
            allowed_scope=(),
            base_dev_sha=config.base_dev_sha,
            remote_tip=remote_tip,
        ),
        workspace=config.workspace,
    )
    return prepared
