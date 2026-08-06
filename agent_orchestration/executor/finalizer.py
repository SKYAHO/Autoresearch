"""Stage 4 승인 candidate를 Git commit·push·Experiment 보고로 수렴시키는 경계.

[파이프라인]
Codex worker와 candidate verifier가 working tree 또는 기존 remote candidate를 승인한 뒤,
평가 전에 하나의 candidate SHA를 원격 exp branch와 Experiment API에 기록하는 구간이다.

[기능]
base tip은 검증 tree와 일치할 때만 고정 identity commit으로 push하고, 같은 형식의 기존
remote candidate는 새 commit 없이 채택한다. 양쪽 경우 API 응답 SHA까지 확인한다.

[비책임]
이슈/ref의 최초 생성(`branch_creator.py`), 변경 정책·sealed test(Stage 4 `verifier.py`),
Candidate DB row lock과 상태 전이(`app/experiments/service.py`), Pod 실행(Stage 6)은
담당하지 않는다.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import os
from pathlib import Path
import re
import stat
import subprocess
from tempfile import TemporaryDirectory
from typing import Iterator
import uuid

from agent_orchestration.executor import api_client, verifier
from agent_orchestration.executor.verifier import VerificationResult


_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_EXECUTOR_NAME = "Autoresearch Experiment Executor"
_EXECUTOR_EMAIL = "experiment-executor@autoresearch.invalid"


class CandidateState(str, Enum):
    """원격 exp ref가 새 commit을 허용하는지 기존 candidate를 채택하는지 나타낸다."""

    NEW = "NEW"
    ADOPTABLE = "ADOPTABLE"


class CandidateFinalizationError(RuntimeError):
    """token·Git stderr를 포함하지 않는 finalizer 실패 사유다."""


@dataclass(frozen=True)
class FinalizeInput:
    """Stage 5가 사용해야 하는 봉인 좌표와 file-backed credential 입력이다."""

    experiment_id: uuid.UUID
    issue_number: int
    issue_branch: str
    base_dev_sha: str
    expected_remote_tip: str
    repository: Path
    github_repository: str
    push_token_file: Path
    api_url: str
    api_token_file: Path


def _clean_remote_url(repository: str) -> str:
    """token이 섞일 수 없는 GitHub HTTPS remote URL을 만든다."""
    return f"https://github.com/{repository}.git"


def _commit_message(issue_number: int) -> str:
    """candidate commit과 ADOPTABLE 재검증에 쓰는 고정 message를 만든다."""
    return f"exp: issue #{issue_number} candidate"


def _validate_sha(value: str, reason: str) -> None:
    """Git stdout/입력에서 lower-case full SHA만 신뢰한다."""
    if _SHA_PATTERN.fullmatch(value) is None:
        raise CandidateFinalizationError(reason)


def _run_git(
    repository: Path,
    *arguments: str,
    environment: dict[str, str],
    allow_failure: bool = False,
    strip_output: bool = True,
) -> str:
    """hooks를 차단한 argv Git 명령의 stdout만 반환하고 stderr는 노출하지 않는다."""
    try:
        result = subprocess.run(
            (
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "credential.helper=",
                "-C",
                str(repository),
                *arguments,
            ),
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
    except OSError as error:
        raise CandidateFinalizationError("git_unavailable") from error
    if result.returncode != 0 and not allow_failure:
        raise CandidateFinalizationError("git_failed")
    return result.stdout.strip() if strip_output else result.stdout


def _git_has_changes(
    repository: Path,
    base_dev_sha: str,
    candidate_sha: str,
    *,
    environment: dict[str, str],
) -> bool:
    """두 commit tree가 다른지 Git exit code로만 판정하고 stderr는 버린다."""
    try:
        result = subprocess.run(
            (
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "credential.helper=",
                "-C",
                str(repository),
                "diff-tree",
                "--quiet",
                base_dev_sha,
                candidate_sha,
            ),
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
    except OSError as error:
        raise CandidateFinalizationError("git_unavailable") from error
    if result.returncode not in {0, 1}:
        raise CandidateFinalizationError("git_failed")
    return result.returncode == 1


def _validate_repository(config: FinalizeInput) -> None:
    """명령 실행 전에 finalizer의 경로·봉인 SHA·이슈 좌표를 fail-closed로 확인한다."""
    if (
        not config.repository.is_absolute()
        or not config.repository.is_dir()
        or config.repository.is_symlink()
        or config.issue_number < 1
        or not config.issue_branch.startswith(f"exp/{config.issue_number}-")
    ):
        raise CandidateFinalizationError("finalize_input_invalid")
    _validate_sha(config.base_dev_sha, "base_sha_invalid")
    _validate_sha(config.expected_remote_tip, "expected_remote_tip_invalid")
    if not config.github_repository or "/" not in config.github_repository:
        raise CandidateFinalizationError("finalize_input_invalid")


def _read_push_token(path: Path) -> str:
    """push token을 regular 파일에서만 읽되 value는 예외에 넣지 않는다."""
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise CandidateFinalizationError("push_token_file_invalid")
        token = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise CandidateFinalizationError("push_token_file_unavailable") from error
    if not token:
        raise CandidateFinalizationError("push_token_file_empty")
    return token


@contextmanager
def _preflight_environment() -> Iterator[dict[str, str]]:
    """write token 없이 local Git config만 검사하는 격리 환경을 제공한다."""
    with TemporaryDirectory(prefix="executor-finalizer-preflight-") as directory:
        yield {
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": directory,
            "XDG_CONFIG_HOME": directory,
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }


@contextmanager
def _push_environment(token_file: Path) -> Iterator[dict[str, str]]:
    """file token을 일회성 GIT_ASKPASS script로만 Git subprocess에 전달한다."""
    token = _read_push_token(token_file)
    with TemporaryDirectory(prefix="executor-finalizer-") as directory:
        temporary_root = Path(directory)
        askpass = temporary_root / "git-askpass"
        askpass.write_text(
            "#!/bin/sh\n"
            'case "$1" in\n'
            "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
            "  *) printf '%s\\n' \"$GITHUB_TOKEN\" ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        environment = {
            "GIT_ASKPASS": str(askpass),
            "GITHUB_TOKEN": token,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(temporary_root),
            "XDG_CONFIG_HOME": str(temporary_root),
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        try:
            yield environment
        finally:
            askpass.unlink(missing_ok=True)


def _candidate_metadata(
    repository: Path, candidate_sha: str, *, environment: dict[str, str]
) -> tuple[str, str, str, str, str, str]:
    """candidate commit의 parent·author·committer·message를 token 없이 읽는다."""
    raw = _run_git(
        repository,
        "show",
        "-s",
        "--format=%P%x00%an%x00%ae%x00%cn%x00%ce",
        candidate_sha,
        environment=environment,
    )
    fields = raw.split("\0")
    if len(fields) != 5:
        raise CandidateFinalizationError("remote_tip_conflict")
    commit_data = _run_git(
        repository,
        "cat-file",
        "commit",
        candidate_sha,
        environment=environment,
        strip_output=False,
    )
    _headers, separator, message = commit_data.partition("\n\n")
    if not separator:
        raise CandidateFinalizationError("remote_tip_conflict")
    return (*fields, message)


def _preflight_repository(config: FinalizeInput, remote_url: str) -> None:
    """token-bearing Git 전에 origin·hooks·credential helper를 공통으로 fail-close 한다."""
    with _preflight_environment() as environment:
        remote = _run_git(
            config.repository,
            "config",
            "--local",
            "--get",
            "remote.origin.url",
            environment=environment,
        )
        if remote != remote_url:
            raise CandidateFinalizationError("remote_url_invalid")
        helper = _run_git(
            config.repository,
            "config",
            "--local",
            "--get-all",
            "credential.helper",
            environment=environment,
            allow_failure=True,
        )
        if helper:
            raise CandidateFinalizationError("credential_helper_present")
        hooks_path = _run_git(
            config.repository,
            "config",
            "--local",
            "--get",
            "core.hooksPath",
            environment=environment,
        )
        if hooks_path != "/dev/null":
            raise CandidateFinalizationError("hooks_path_invalid")


def classify_candidate_state(
    repository: Path,
    *,
    base_dev_sha: str,
    issue_number: int,
    remote_tip: str,
) -> CandidateState:
    """base 또는 고정 executor commit 하나만 Stage 5 state로 분류한다."""
    _validate_sha(base_dev_sha, "base_sha_invalid")
    _validate_sha(remote_tip, "remote_tip_invalid")
    if issue_number < 1:
        raise CandidateFinalizationError("issue_number_invalid")
    if remote_tip == base_dev_sha:
        return CandidateState.NEW
    environment = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": "/nonexistent",
        "XDG_CONFIG_HOME": "/nonexistent",
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    parent, author_name, author_email, committer_name, committer_email, message = (
        _candidate_metadata(repository, remote_tip, environment=environment)
    )
    has_changes = _git_has_changes(
        repository,
        base_dev_sha,
        remote_tip,
        environment=environment,
    )
    if (
        parent != base_dev_sha
        or author_name != _EXECUTOR_NAME
        or author_email != _EXECUTOR_EMAIL
        or committer_name != _EXECUTOR_NAME
        or committer_email != _EXECUTOR_EMAIL
        or message != f"{_commit_message(issue_number)}\n"
        or not has_changes
    ):
        raise CandidateFinalizationError("remote_tip_conflict")
    return CandidateState.ADOPTABLE


def _remote_tip(
    repository: Path, remote_url: str, issue_branch: str, *, environment: dict[str, str]
) -> str:
    """정확한 exp branch ref의 원격 SHA 하나만 읽는다."""
    raw = _run_git(
        repository,
        "ls-remote",
        "--refs",
        remote_url,
        f"refs/heads/{issue_branch}",
        environment=environment,
    )
    fields = raw.split()
    if len(fields) != 2 or fields[1] != f"refs/heads/{issue_branch}":
        raise CandidateFinalizationError("remote_tip_invalid")
    _validate_sha(fields[0], "remote_tip_invalid")
    return fields[0]


def _assert_local_head_base(
    config: FinalizeInput, *, environment: dict[str, str]
) -> None:
    """NEW가 base checkout에서만 local ref를 변경하게 한다."""
    if (
        _run_git(config.repository, "rev-parse", "HEAD", environment=environment)
        != config.base_dev_sha
    ):
        raise CandidateFinalizationError("head_changed")


def _assert_remote_base_before_commit(
    config: FinalizeInput, remote_url: str, *, environment: dict[str, str]
) -> None:
    """NEW commit 직전에 exp ref가 여전히 봉인 base인지를 재조회한다."""
    if (
        _remote_tip(
            config.repository,
            remote_url,
            config.issue_branch,
            environment=environment,
        )
        != config.base_dev_sha
    ):
        raise CandidateFinalizationError("remote_tip_changed")


def _push_candidate(
    repository: Path, remote_url: str, issue_branch: str, *, environment: dict[str, str]
) -> None:
    """force 없이 HEAD만 정확한 exp branch refspec으로 push한다."""
    try:
        result = subprocess.run(
            (
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "credential.helper=",
                "-C",
                str(repository),
                "push",
                remote_url,
                f"HEAD:refs/heads/{issue_branch}",
            ),
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
    except OSError as error:
        raise CandidateFinalizationError("push_failed") from error
    if result.returncode != 0:
        raise CandidateFinalizationError("push_failed")


def _commit_verified_tree(
    config: FinalizeInput,
    verified_tree_oid: str,
    *,
    environment: dict[str, str],
) -> str:
    """mutable index가 아닌 검증 tree OID로 한 candidate commit과 local CAS ref를 만든다."""
    candidate_sha = _run_git(
        config.repository,
        "-c",
        f"user.name={_EXECUTOR_NAME}",
        "-c",
        f"user.email={_EXECUTOR_EMAIL}",
        "commit-tree",
        verified_tree_oid,
        "-p",
        config.base_dev_sha,
        "-m",
        _commit_message(config.issue_number),
        environment=environment,
    )
    _validate_sha(candidate_sha, "candidate_sha_invalid")
    if (
        _run_git(
            config.repository,
            "rev-parse",
            f"{candidate_sha}^{{tree}}",
            environment=environment,
        )
        != verified_tree_oid
    ):
        raise CandidateFinalizationError("committed_tree_mismatch")
    try:
        _run_git(
            config.repository,
            "update-ref",
            f"refs/heads/{config.issue_branch}",
            candidate_sha,
            config.base_dev_sha,
            environment=environment,
        )
    except CandidateFinalizationError as error:
        raise CandidateFinalizationError("local_ref_update_failed") from error
    if (
        _run_git(config.repository, "rev-parse", "HEAD", environment=environment)
        != candidate_sha
    ):
        raise CandidateFinalizationError("local_ref_update_failed")
    return candidate_sha


def _commit_new_candidate(
    config: FinalizeInput,
    verification: VerificationResult,
    remote_url: str,
    *,
    environment: dict[str, str],
) -> str:
    """Stage 4 handoff와 같은 tree만 한 번 commit하고 non-force push한다."""
    changed_paths, content_fingerprint = verifier.current_working_tree_verification(
        config.repository, config.base_dev_sha
    )
    if (
        changed_paths != verification.changed_paths
        or content_fingerprint != verification.content_fingerprint
    ):
        raise CandidateFinalizationError("content_fingerprint_mismatch")
    _assert_local_head_base(config, environment=environment)
    _run_git(config.repository, "add", "--all", environment=environment)
    if (
        verifier.write_staged_tree_oid(config.repository)
        != verification.verified_tree_oid
    ):
        raise CandidateFinalizationError("verified_tree_mismatch")
    _assert_remote_base_before_commit(config, remote_url, environment=environment)
    candidate_sha = _commit_verified_tree(
        config,
        verification.verified_tree_oid,
        environment=environment,
    )
    _push_candidate(
        config.repository, remote_url, config.issue_branch, environment=environment
    )
    if (
        _remote_tip(
            config.repository, remote_url, config.issue_branch, environment=environment
        )
        != candidate_sha
    ):
        raise CandidateFinalizationError("remote_sha_mismatch")
    return candidate_sha


def finalize_candidate(config: FinalizeInput, verification: VerificationResult) -> str:
    """원격 exp ref와 Candidate API를 동일 SHA로 수렴시키고 그 SHA를 반환한다."""
    _validate_repository(config)
    remote_url = _clean_remote_url(config.github_repository)
    _preflight_repository(config, remote_url)
    with _push_environment(config.push_token_file) as environment:
        remote_tip = _remote_tip(
            config.repository, remote_url, config.issue_branch, environment=environment
        )
        if remote_tip != config.expected_remote_tip:
            raise CandidateFinalizationError("remote_tip_changed")
        state = classify_candidate_state(
            config.repository,
            base_dev_sha=config.base_dev_sha,
            issue_number=config.issue_number,
            remote_tip=remote_tip,
        )
        if state is CandidateState.NEW:
            candidate_sha = _commit_new_candidate(
                config, verification, remote_url, environment=environment
            )
        else:
            candidate_tree = _run_git(
                config.repository,
                "rev-parse",
                f"{remote_tip}^{{tree}}",
                environment=environment,
            )
            if candidate_tree != verification.verified_tree_oid:
                raise CandidateFinalizationError("verified_tree_mismatch")
            candidate_sha = remote_tip
    try:
        api_client.report_candidate(
            api_url=config.api_url,
            token_file=config.api_token_file,
            experiment_id=config.experiment_id,
            issue_number=config.issue_number,
            issue_branch=config.issue_branch,
            base_dev_sha=config.base_dev_sha,
            candidate_sha=candidate_sha,
        )
    except api_client.CandidateApiError as error:
        raise CandidateFinalizationError(str(error)) from error
    return candidate_sha
