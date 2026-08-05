"""Executor Pod의 봉인 좌표 검증·token 전달·exp ref 생성 계약을 검증한다.

전체 파이프라인에서 launcher가 전달한 실험 좌표를 executor Pod가 검증하고 봉인된
``base_dev_sha``에 exp branch를 만드는 실행 경계를 담당한다. Kubernetes Job 조립,
launcher 상태 전이와 GitHub App private key 배포는 검증하지 않는다.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
import os
from pathlib import Path
import stat
import uuid

import pytest

from agent_orchestration.executor.config import (
    BranchBootstrapInput,
    ExecutorConfigError,
)
from agent_orchestration.executor.main import (
    BranchConflictError,
    bootstrap_branch,
    main as executor_main,
)
from agent_orchestration.executor.token_minter import (
    main as token_minter_main,
    write_installation_token,
)
from agent_orchestration.github_app import (
    GitHubAppCredentials,
    InstallationToken,
)
from agent_orchestration.github_refs import GitHubRefError


_EXPERIMENT_ID = "12345678-1234-5678-1234-567812345678"
_ISSUE_BRANCH = "exp/546-executor-bootstrap"
_BASE_SHA = "a" * 40


def _set_valid_executor_environment(
    monkeypatch: pytest.MonkeyPatch,
    token_file: Path,
) -> None:
    token_file.write_text("secret-token\n", encoding="utf-8")
    monkeypatch.setenv("ORCH_EXPERIMENT_ID", _EXPERIMENT_ID)
    monkeypatch.setenv("ORCH_ISSUE_NUMBER", "546")
    monkeypatch.setenv("ORCH_ISSUE_BRANCH", _ISSUE_BRANCH)
    monkeypatch.setenv("ORCH_BASE_DEV_SHA", _BASE_SHA)
    monkeypatch.setenv("ORCH_GITHUB_REPOSITORY", "SKYAHO/Autoresearch")
    monkeypatch.setenv("ORCH_GITHUB_TOKEN_FILE", str(token_file))


def _valid_input(base_sha: str = _BASE_SHA) -> BranchBootstrapInput:
    return BranchBootstrapInput(
        experiment_id=uuid.UUID(_EXPERIMENT_ID),
        issue_number=546,
        issue_branch=_ISSUE_BRANCH,
        base_dev_sha=base_sha,
        github_repository="SKYAHO/Autoresearch",
        token_file=Path("/var/run/github-token/token"),
    )


class FakeRefs:
    """ref 호출을 기록하고 준비된 조회·생성 결과를 반환한다."""

    def __init__(
        self,
        existing_sha: str | None = None,
        *,
        create_error: GitHubRefError | None = None,
        race_sha: str | None = None,
        get_error: GitHubRefError | None = None,
    ) -> None:
        self.existing_sha = existing_sha
        self.create_error = create_error
        self.race_sha = race_sha
        self.get_error = get_error
        self.get_sha_calls: list[tuple[str, str, str]] = []
        self.create_calls: list[tuple[str, str, str, str]] = []

    async def get_sha(self, repository: str, ref: str, token: str) -> str | None:
        self.get_sha_calls.append((repository, ref, token))
        if self.get_error is not None:
            raise self.get_error
        if len(self.get_sha_calls) > 1:
            return self.race_sha
        return self.existing_sha

    async def create(
        self,
        repository: str,
        ref: str,
        sha: str,
        token: str,
    ) -> str:
        self.create_calls.append((repository, ref, sha, token))
        if self.create_error is not None:
            raise self.create_error
        return sha


def _test_credentials(tmp_path: Path) -> GitHubAppCredentials:
    key_path = tmp_path / "private-key.pem"
    key_path.write_text("test-only-private-key", encoding="utf-8")
    return GitHubAppCredentials(
        app_id=123,
        installation_id=456,
        private_key_path=key_path,
    )


async def _fake_token_factory(
    credentials: GitHubAppCredentials,
    *,
    permissions: dict[str, str],
) -> InstallationToken:
    assert credentials.app_id == 123
    assert permissions == {"contents": "write"}
    return InstallationToken(
        value="secret-token",
        expires_at=datetime(2026, 8, 5, 1, 0, tzinfo=UTC),
    )


def test_executor_rejects_missing_base_sha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_valid_executor_environment(monkeypatch, tmp_path / "token")
    monkeypatch.delenv("ORCH_BASE_DEV_SHA")

    with pytest.raises(ExecutorConfigError, match="ORCH_BASE_DEV_SHA"):
        BranchBootstrapInput.from_environment()


@pytest.mark.parametrize(
    ("environment_name", "invalid_value"),
    [
        ("ORCH_EXPERIMENT_ID", "not-a-uuid"),
        ("ORCH_ISSUE_NUMBER", "0"),
        ("ORCH_ISSUE_BRANCH", "exp/546_Invalid"),
        ("ORCH_BASE_DEV_SHA", "ABC123"),
        ("ORCH_GITHUB_REPOSITORY", "SKYAHO/Autoresearch/extra"),
    ],
)
def test_executor_rejects_invalid_sealed_coordinates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    environment_name: str,
    invalid_value: str,
) -> None:
    _set_valid_executor_environment(monkeypatch, tmp_path / "token")
    monkeypatch.setenv(environment_name, invalid_value)

    with pytest.raises(ExecutorConfigError, match=environment_name):
        BranchBootstrapInput.from_environment()


def test_executor_rejects_non_regular_token_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_directory = tmp_path / "token-directory"
    token_directory.mkdir()
    _set_valid_executor_environment(monkeypatch, tmp_path / "token")
    monkeypatch.setenv("ORCH_GITHUB_TOKEN_FILE", str(token_directory))

    with pytest.raises(ExecutorConfigError, match="ORCH_GITHUB_TOKEN_FILE"):
        BranchBootstrapInput.from_environment()


def test_executor_parses_all_sealed_coordinates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_file = tmp_path / "token"
    _set_valid_executor_environment(monkeypatch, token_file)

    result = BranchBootstrapInput.from_environment()

    assert result == BranchBootstrapInput(
        experiment_id=uuid.UUID(_EXPERIMENT_ID),
        issue_number=546,
        issue_branch=_ISSUE_BRANCH,
        base_dev_sha=_BASE_SHA,
        github_repository="SKYAHO/Autoresearch",
        token_file=token_file,
    )


def test_existing_ref_at_same_sha_is_success() -> None:
    refs = FakeRefs(existing_sha=_BASE_SHA)

    result = asyncio.run(bootstrap_branch(_valid_input(), refs, "token"))

    assert result.created is False
    assert refs.create_calls == []


def test_existing_ref_at_different_sha_never_updates() -> None:
    refs = FakeRefs(existing_sha="b" * 40)

    with pytest.raises(BranchConflictError):
        asyncio.run(bootstrap_branch(_valid_input(), refs, "token"))

    assert refs.create_calls == []


def test_missing_ref_is_created_at_the_frozen_sha() -> None:
    refs = FakeRefs()

    result = asyncio.run(bootstrap_branch(_valid_input(), refs, "token"))

    assert result.created is True
    assert refs.get_sha_calls == [
        ("SKYAHO/Autoresearch", f"heads/{_ISSUE_BRANCH}", "token")
    ]
    assert refs.create_calls == [
        (
            "SKYAHO/Autoresearch",
            f"heads/{_ISSUE_BRANCH}",
            _BASE_SHA,
            "token",
        )
    ]


def test_create_422_race_is_success_only_after_same_sha_recheck() -> None:
    refs = FakeRefs(
        create_error=GitHubRefError("create_failed", status_code=422),
        race_sha=_BASE_SHA,
    )

    result = asyncio.run(bootstrap_branch(_valid_input(), refs, "token"))

    assert result.created is False
    assert len(refs.get_sha_calls) == 2
    assert len(refs.create_calls) == 1


def test_create_422_race_at_different_sha_fails_closed() -> None:
    refs = FakeRefs(
        create_error=GitHubRefError("create_failed", status_code=422),
        race_sha="b" * 40,
    )

    with pytest.raises(BranchConflictError):
        asyncio.run(bootstrap_branch(_valid_input(), refs, "token"))

    assert len(refs.get_sha_calls) == 2
    assert len(refs.create_calls) == 1


def test_create_422_without_a_ref_preserves_the_github_error() -> None:
    refs = FakeRefs(
        create_error=GitHubRefError("create_failed", status_code=422),
        race_sha=None,
    )

    with pytest.raises(GitHubRefError) as captured:
        asyncio.run(bootstrap_branch(_valid_input(), refs, "token"))

    assert captured.value.status_code == 422
    assert len(refs.get_sha_calls) == 2


def test_unauthorized_ref_lookup_is_not_retried() -> None:
    refs = FakeRefs(get_error=GitHubRefError("get_failed", status_code=401))

    with pytest.raises(GitHubRefError) as captured:
        asyncio.run(bootstrap_branch(_valid_input(), refs, "token"))

    assert captured.value.status_code == 401
    assert len(refs.get_sha_calls) == 1
    assert refs.create_calls == []


def test_token_minter_writes_0400_atomically_without_printing_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "token"
    output.write_text("stale-token", encoding="utf-8")
    output.chmod(0o644)
    replace_calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        assert source_path.parent == output.parent
        assert stat.S_IMODE(source_path.stat().st_mode) == 0o400
        replace_calls.append((source_path, destination_path))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", recording_replace)

    asyncio.run(
        write_installation_token(
            credentials=_test_credentials(tmp_path),
            output=output,
            permissions={"contents": "write"},
            token_factory=_fake_token_factory,
        )
    )

    assert replace_calls and replace_calls[0][1] == output
    assert stat.S_IMODE(output.stat().st_mode) == 0o400
    assert output.read_text(encoding="utf-8") == "secret-token"
    captured = capsys.readouterr()
    assert "secret-token" not in captured.out + captured.err


def test_executor_main_reads_only_the_token_file_and_logs_safe_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token_file = tmp_path / "token"
    _set_valid_executor_environment(monkeypatch, token_file)
    monkeypatch.setenv(
        "ORCH_GITHUB_APP_PRIVATE_KEY_FILE",
        str(tmp_path / "executor-must-not-read-this-key"),
    )
    refs = FakeRefs(existing_sha=_BASE_SHA)
    caplog.set_level(logging.INFO)

    exit_code = executor_main(refs=refs)

    assert exit_code == 0
    assert refs.get_sha_calls == [
        ("SKYAHO/Autoresearch", f"heads/{_ISSUE_BRANCH}", "secret-token")
    ]
    assert "secret-token" not in caplog.text
    assert _EXPERIMENT_ID in caplog.text
    assert "issue_number=546" in caplog.text
    assert f"branch={_ISSUE_BRANCH}" in caplog.text
    assert f"base_sha={_BASE_SHA}" in caplog.text
    assert "created=False" in caplog.text


def test_executor_main_returns_one_without_exposing_token_on_github_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _set_valid_executor_environment(monkeypatch, tmp_path / "token")
    refs = FakeRefs(get_error=GitHubRefError("get_failed", status_code=401))
    caplog.set_level(logging.ERROR)

    exit_code = executor_main(refs=refs)

    assert exit_code == 1
    assert len(refs.get_sha_calls) == 1
    assert "secret-token" not in caplog.text


def test_token_minter_main_reads_app_coordinates_and_writes_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    key_path = tmp_path / "private-key.pem"
    key_path.write_text("test-only-private-key", encoding="utf-8")
    output = tmp_path / "github-token" / "token"
    output.parent.mkdir()
    monkeypatch.setenv("ORCH_GITHUB_APP_ID", "123")
    monkeypatch.setenv("ORCH_GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv("ORCH_GITHUB_APP_PRIVATE_KEY_FILE", str(key_path))
    monkeypatch.setenv("ORCH_GITHUB_TOKEN_FILE", str(output))

    exit_code = token_minter_main(token_factory=_fake_token_factory)

    assert exit_code == 0
    assert output.read_text(encoding="utf-8") == "secret-token"
    assert stat.S_IMODE(output.stat().st_mode) == 0o400
    captured = capsys.readouterr()
    assert "secret-token" not in captured.out + captured.err
