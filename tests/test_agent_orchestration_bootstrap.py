"""Agent Orchestration API·Runner 시크릿 부트스트랩 경계 테스트."""

from __future__ import annotations

import logging
from pathlib import Path
import stat

import pytest

from agent_orchestration import bootstrap_secrets as bootstrap_module
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


def test_runner_bootstrap_preserves_existing_auth_file_without_reading_secret(
    tmp_path: Path,
) -> None:
    """기본 Runner bootstrap은 갱신된 OAuth 파일과 Secret Manager 조회를 보존한다."""
    settings = _runner_settings(tmp_path)
    calls: list[str] = []
    auth_path = settings.codex_home / "auth.json"
    auth_path.parent.mkdir()
    refreshed_contents = bytes((82,))
    auth_path.write_bytes(refreshed_contents)

    def reader(secret_id: str) -> bytes:
        calls.append(secret_id)
        return bytes((123, 125))

    bootstrap_runner_codex_auth(settings, reader)

    assert calls == []
    assert auth_path.read_bytes() == refreshed_contents
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600


def test_runner_bootstrap_replace_existing_replaces_auth_file_with_private_mode(
    tmp_path: Path,
) -> None:
    """명시 opt-in Runner bootstrap만 OAuth 시크릿으로 기존 파일을 원자 교체한다."""
    settings = _runner_settings(tmp_path)
    auth_path = settings.codex_home / "auth.json"
    auth_path.parent.mkdir()
    auth_path.write_bytes(b"refreshed-auth")
    auth_path.chmod(0o644)
    replacement_payload = b"replacement-auth"
    calls: list[str] = []

    def reader(secret_id: str) -> bytes:
        calls.append(secret_id)
        return replacement_payload

    bootstrap_runner_codex_auth(settings, reader, replace_existing=True)

    assert calls == [settings.codex_auth_secret_id]
    assert auth_path.read_bytes() == replacement_payload
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600


@pytest.mark.parametrize("replace_existing", (False, True))
def test_runner_bootstrap_rejects_dangling_auth_symlink_without_reading_secret(
    tmp_path: Path,
    replace_existing: bool,
) -> None:
    """기본·복구 Runner bootstrap은 dangling auth symlink를 Secret 조회 전에 거부한다."""
    settings = _runner_settings(tmp_path)
    auth_path = settings.codex_home / "auth.json"
    auth_path.parent.mkdir()
    auth_path.symlink_to(tmp_path / "missing-auth.json")
    calls: list[str] = []

    def reader(secret_id: str) -> bytes:
        calls.append(secret_id)
        return b"replacement-auth"

    with pytest.raises(RuntimeError, match="must be a regular file"):
        bootstrap_runner_codex_auth(
            settings,
            reader,
            replace_existing=replace_existing,
        )

    assert calls == []
    assert auth_path.is_symlink()


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


@pytest.mark.parametrize(
    ("role", "environment", "expected_path"),
    [
        (
            "api-database",
            {
                "ORCH_DB_PASSWORD_SECRET_ID": "projects/test/secrets/orch-db-password/versions/latest",
                "ORCH_DB_HOST": "10.0.0.15",
                "ORCH_DB_NAME": "agent_orchestration",
                "ORCH_DB_USER": "agent_orchestration_app",
            },
            "runtime/db.env",
        ),
        (
            "runner-codex-auth",
            {
                "ORCH_CODEX_AUTH_SECRET_ID": "projects/test/secrets/codex-auth/versions/latest",
            },
            "codex/auth.json",
        ),
    ],
)
def test_bootstrap_cli_role_uses_only_its_runtime_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    environment: dict[str, str],
    expected_path: str,
) -> None:
    """각 CLI 역할은 다른 워크로드 설정 없이 자기 런타임 파일만 준비한다."""
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ORCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(
        bootstrap_module,
        "read_secret_manager_secret",
        lambda _secret_id: b"placeholder",
    )

    assert bootstrap_module.main([role]) == 0
    assert (tmp_path / expected_path).is_file()


def test_bootstrap_cli_without_role_remains_api_database_compatible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """기존 인자 없는 모듈 실행은 API DB bootstrap으로 계속 동작한다."""
    monkeypatch.setenv(
        "ORCH_DB_PASSWORD_SECRET_ID",
        "projects/test/secrets/orch-db-password/versions/latest",
    )
    monkeypatch.setenv("ORCH_DB_HOST", "10.0.0.15")
    monkeypatch.setenv("ORCH_DB_NAME", "agent_orchestration")
    monkeypatch.setenv("ORCH_DB_USER", "agent_orchestration_app")
    monkeypatch.setenv("ORCH_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(
        bootstrap_module,
        "read_secret_manager_secret",
        lambda _secret_id: b"placeholder",
    )

    assert bootstrap_module.main([]) == 0
    assert (tmp_path / "runtime" / "db.env").is_file()


def test_bootstrap_cli_rejects_replace_existing_for_api_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """API DB 역할은 Runner OAuth 복구 flag를 받을 수 없다."""
    with pytest.raises(SystemExit) as error:
        bootstrap_module.main(["api-database", "--replace-existing"])

    assert error.value.code == 2
    assert (
        "--replace-existing is only valid for runner-codex-auth."
        in capsys.readouterr().err
    )


def test_bootstrap_cli_replaces_existing_runner_auth_when_opted_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner CLI의 명시 opt-in은 기존 OAuth 파일을 새 시크릿으로 교체한다."""
    monkeypatch.setenv(
        "ORCH_CODEX_AUTH_SECRET_ID",
        "projects/test/secrets/codex-auth/versions/latest",
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    auth_path = tmp_path / "codex" / "auth.json"
    auth_path.parent.mkdir()
    auth_path.write_bytes(b"refreshed-auth")
    auth_path.chmod(0o644)
    calls: list[str] = []

    def reader(secret_id: str) -> bytes:
        calls.append(secret_id)
        return b"replacement-auth"

    monkeypatch.setattr(bootstrap_module, "read_secret_manager_secret", reader)

    assert bootstrap_module.main(["runner-codex-auth", "--replace-existing"]) == 0
    assert calls == ["projects/test/secrets/codex-auth/versions/latest"]
    assert auth_path.read_bytes() == b"replacement-auth"
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600
