"""Agent Orchestration API·Runner 시크릿 부트스트랩 경계 테스트."""

from __future__ import annotations

import logging
from pathlib import Path
import stat

import pytest

from agent_orchestration.bootstrap_secrets import (
    DatabaseBootstrapSettings,
    RunnerAuthBootstrapSettings,
    bootstrap_api_database,
    bootstrap_runner_codex_auth,
)


def _database_settings(tmp_path: Path) -> DatabaseBootstrapSettings:
    """테스트별 격리된 API DB 런타임 경로를 만든다."""
    return DatabaseBootstrapSettings(
        db_password_secret_id="projects/test/secrets/orch-db-password/versions/latest",
        db_host="10.0.0.15",
        db_name="agent_orchestration",
        db_user="agent_orchestration_app",
        runtime_dir=tmp_path / "runtime",
    )


def _runner_settings(tmp_path: Path) -> RunnerAuthBootstrapSettings:
    """테스트별 격리된 Runner Codex 상태 경로를 만든다."""
    return RunnerAuthBootstrapSettings(
        codex_auth_secret_id="projects/test/secrets/codex-auth/versions/latest",
        codex_home=tmp_path / "codex",
    )


def test_api_bootstrap_never_reads_codex_auth_secret(tmp_path: Path) -> None:
    """API DB 초기화는 DB 시크릿만 읽고 권한 제한 파일만 작성한다."""
    settings = _database_settings(tmp_path)
    calls: list[str] = []

    def reader(secret_id: str) -> bytes:
        calls.append(secret_id)
        return bytes((120,))

    bootstrap_api_database(settings, reader)

    runtime_env = settings.runtime_dir / "db.env"
    assert calls == [settings.db_password_secret_id]
    assert runtime_env.is_file()
    assert stat.S_IMODE(runtime_env.stat().st_mode) == 0o600


def test_api_bootstrap_does_not_change_existing_volume_root_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fsGroup로 쓰기 가능한 볼륨 루트는 비루트 init container가 chmod하지 않는다."""
    settings = _database_settings(tmp_path)
    settings.runtime_dir.mkdir()
    original_chmod = Path.chmod

    def reject_volume_root_chmod(path: Path, mode: int) -> None:
        if path == settings.runtime_dir:
            raise PermissionError("volume root is owned by kubelet")
        original_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", reject_volume_root_chmod)

    bootstrap_api_database(settings, lambda _secret_id: b"password")

    assert (settings.runtime_dir / "db.env").is_file()


def test_runner_bootstrap_preserves_refreshed_auth_file(tmp_path: Path) -> None:
    """Runner는 최초 OAuth 파일만 쓰고 이후 Codex 갱신본을 보존한다."""
    settings = _runner_settings(tmp_path)
    calls: list[str] = []

    def reader(secret_id: str) -> bytes:
        calls.append(secret_id)
        return bytes((123, 125))

    bootstrap_runner_codex_auth(settings, reader)

    auth_path = settings.codex_home / "auth.json"
    refreshed_contents = bytes((82,))
    auth_path.write_bytes(refreshed_contents)
    bootstrap_runner_codex_auth(settings, reader)

    assert calls == [settings.codex_auth_secret_id]
    assert auth_path.read_bytes() == refreshed_contents
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600


def test_bootstrap_does_not_log_or_raise_secret_payload(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Secret Manager 오류의 본문은 API 오류와 로그에서 제외한다."""
    settings = _database_settings(tmp_path)
    sensitive_payload = "sensitive-" + "payload"

    def failing_reader(secret_id: str) -> bytes:
        raise RuntimeError(f"reader failed while returning {sensitive_payload} for {secret_id}")

    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError) as error:
        bootstrap_api_database(settings, failing_reader)

    assert sensitive_payload not in str(error.value)
    assert sensitive_payload not in caplog.text
    assert settings.db_password_secret_id in str(error.value)
