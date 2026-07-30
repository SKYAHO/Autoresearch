"""Agent Orchestration 배포 시크릿 부트스트랩 계약 테스트."""

from __future__ import annotations

import logging
from pathlib import Path
import stat

import pytest

from agent_orchestration.bootstrap_secrets import (
    BootstrapSettings,
    bootstrap_runtime_secrets,
)


def _settings(tmp_path: Path) -> BootstrapSettings:
    """테스트별 격리된 런타임·Codex 상태 경로를 만든다."""
    return BootstrapSettings(
        db_password_secret_id="projects/test/secrets/orch-db-password/versions/latest",
        codex_auth_secret_id="projects/test/secrets/codex-auth/versions/latest",
        db_host="10.0.0.15",
        db_name="agent_orchestration",
        db_user="agent_orchestration_app",
        runtime_dir=tmp_path / "runtime",
        codex_home=tmp_path / "codex",
    )


def test_bootstrap_writes_database_env_and_initial_auth_once(tmp_path: Path) -> None:
    """DB URL은 권한 제한 파일에 쓰고 OAuth 초기 파일은 최초 한 번만 쓴다."""
    settings = _settings(tmp_path)
    values = {
        settings.db_password_secret_id: b"db-password-with-unsafe-characters:/@",
        settings.codex_auth_secret_id: b'{"access_token":"oauth-secret"}',
    }

    bootstrap_runtime_secrets(settings, values.__getitem__)

    runtime_env = settings.runtime_dir / "db.env"
    auth_path = settings.codex_home / "auth.json"
    assert runtime_env.read_text(encoding="utf-8") == (
        "ORCH_DATABASE_URL=postgresql://agent_orchestration_app:"
        "db-password-with-unsafe-characters%3A%2F%40@10.0.0.15/agent_orchestration\n"
    )
    assert auth_path.read_bytes() == b'{"access_token":"oauth-secret"}'
    assert stat.S_IMODE(runtime_env.stat().st_mode) == 0o600
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600

    auth_path.write_bytes(b'{"access_token":"refreshed"}')
    bootstrap_runtime_secrets(settings, values.__getitem__)

    assert auth_path.read_bytes() == b'{"access_token":"refreshed"}'


def test_bootstrap_does_not_log_or_raise_secret_payload(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Secret Manager 실패 메시지에 시크릿 본문이 섞여도 외부로 전파하지 않는다."""
    settings = _settings(tmp_path)
    secret_payload = "db-password-must-not-appear"

    def failing_reader(secret_id: str) -> bytes:
        raise RuntimeError(f"reader failed while returning {secret_payload} for {secret_id}")

    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError) as error:
        bootstrap_runtime_secrets(settings, failing_reader)

    assert secret_payload not in str(error.value)
    assert secret_payload not in caplog.text
    assert settings.db_password_secret_id in str(error.value)

