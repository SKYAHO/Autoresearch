"""Agent Orchestration 1단계 모듈 스켈레톤 테스트."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

from fastapi import status
from fastapi.testclient import TestClient
import pytest

from agent_orchestration.app.config import ServiceSettings, load_settings
from agent_orchestration.app.experiments.issue_authoring import ExperimentDefaults
from agent_orchestration.app import db as db_module
from agent_orchestration.app import llm as llm_module
from agent_orchestration.app import main as main_module
from agent_orchestration.app.llm import LLMBackendError, LLMResult, generate_response
from agent_orchestration import codex as codex_module


_SETTINGS_ENV_VARS = (
    "CODEX_CLI_PATH",
    "CODEX_HOME",
    "CODEX_MODEL",
    "CODEX_RUNNER_TIMEOUT_SEC",
    "CODEX_RUNNER_URL",
    "CODEX_TIMEOUT_SEC",
    "DATABASE_URL",
    "LLM_BACKEND",
    "OPENAI_API_KEY",
    "OPENAI_MAX_TOKENS",
    "OPENAI_MODEL",
    "OPENAI_TIMEOUT_SEC",
    "ORCH_API_TOKEN",
    "ORCH_EXECUTOR_API_TOKEN",
    "ORCH_RUNNER_TOKEN",
    "ORCH_DATABASE_URL",
    "ORCH_DB_CONNECT_TIMEOUT_SEC",
    "ORCH_INTERACTIONS_TABLE",
    "ORCH_GITHUB_TOKEN",
    "ORCH_GITHUB_REPOSITORY",
    "ORCH_BASELINE_GITHUB_APP_ID",
    "ORCH_BASELINE_GITHUB_APP_INSTALLATION_ID",
    "ORCH_BASELINE_GITHUB_APP_PRIVATE_KEY_PATH",
    "ORCH_GH_TIMEOUT_SEC",
    "ORCH_ISSUE_DAILY_LIMIT",
    "ORCH_EXPERIMENT_DATASET_SOURCE",
    "ORCH_EXPERIMENT_TRAINING_CONFIG_REF",
)
_TEST_API_TOKEN = "test-api-token-must-be-at-least-32-characters"


@pytest.fixture(autouse=True)
def clear_agent_orchestration_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """설정 테스트가 개발 셸의 환경 변수에 영향을 받지 않게 한다."""
    for name in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ORCH_API_TOKEN", _TEST_API_TOKEN)
    monkeypatch.setenv(
        "ORCH_EXECUTOR_API_TOKEN", "test-executor-token-must-be-at-least-32-characters"
    )
    monkeypatch.setenv("CODEX_HOME", "/tmp/test-codex-home")


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`load_settings()`가 기본 backend(codex_cli)로 성공하는 데 필요한 값을 채운다.

    `ORCH_API_TOKEN`과 `CODEX_HOME`은 `clear_agent_orchestration_environment`가 이미
    채우므로 여기서는 그 외 필수 값만 다룬다. 각 테스트는 이 호출 뒤에 자신이 검증할
    변수를 개별적으로 override하거나 delenv한다.
    """
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch:pw@localhost:5432/orch")
    monkeypatch.setenv("ORCH_GITHUB_TOKEN", "x" * 40)
    monkeypatch.setenv("ORCH_GITHUB_REPOSITORY", "SKYAHO/Autoresearch")
    monkeypatch.setenv("ORCH_BASELINE_GITHUB_APP_ID", "123")
    monkeypatch.setenv("ORCH_BASELINE_GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv(
        "ORCH_BASELINE_GITHUB_APP_PRIVATE_KEY_PATH", "/var/run/test/baseline-app.pem"
    )
    monkeypatch.setenv(
        "ORCH_EXPERIMENT_DATASET_SOURCE", "feast://feast_offline_store/ctr_training_v1"
    )
    monkeypatch.setenv("ORCH_EXPERIMENT_TRAINING_CONFIG_REF", "configs/train/x.yaml@abc")


def test_load_settings_prefers_orchestration_database_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """ORCH_DATABASE_URL이 설정되면 DATABASE_URL보다 우선 사용한다."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch:pw@localhost:5432/orch_db")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fallback:pw@localhost:5432/fallback")

    settings = load_settings()

    assert settings.openai_model == "gpt-5.3-codex-spark"
    assert settings.database_url == "postgresql://orch:pw@localhost:5432/orch_db"
    assert settings.interactions_table == "chat_interactions"
    assert settings.api_token == _TEST_API_TOKEN
    assert settings.codex_home == "/tmp/test-codex-home"


def test_load_settings_accepts_database_url_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """ORCH_DATABASE_URL 미설정 시 DATABASE_URL을 사용한다."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("ORCH_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgres://generic:pw@localhost:5432/generic_db")

    settings = load_settings()

    assert settings.database_url == "postgres://generic:pw@localhost:5432/generic_db"


def test_load_settings_allows_codex_without_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex CLI 공용 계정 모드는 OpenAI API 키를 요구하지 않는다."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("LLM_BACKEND", "codex_cli")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch:pw@localhost:5432/orch")

    settings = load_settings()

    assert settings.llm_backend == "codex_cli"
    assert settings.openai_api_key is None


def test_load_settings_openai_does_not_require_codex_runtime_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI 모드에서는 Codex 실행 파일과 홈이 없어도 기동한다."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch:pw@localhost:5432/orch")
    monkeypatch.setenv("CODEX_CLI_PATH", "   ")
    monkeypatch.delenv("CODEX_HOME", raising=False)

    settings = load_settings()

    assert settings.llm_backend == "openai"
    assert settings.codex_cli_path == "codex"
    assert settings.codex_home == ""


def test_load_settings_codex_runner_uses_private_url_without_local_codex_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner 백엔드는 API 프로세스의 로컬 Codex 홈을 요구하지 않는다."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("LLM_BACKEND", "codex_runner")
    monkeypatch.setenv("CODEX_RUNNER_URL", "http://runner:8080")
    monkeypatch.setenv("ORCH_RUNNER_TOKEN", "runner-token-must-be-at-least-32-characters")
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch@localhost:5432/orch")
    monkeypatch.setenv("CODEX_CLI_PATH", "   ")
    monkeypatch.delenv("CODEX_HOME", raising=False)

    settings = load_settings()

    assert settings.llm_backend == "codex_runner"
    assert settings.codex_runner_url == "http://runner:8080"
    assert settings.codex_runner_timeout_sec == 120
    assert settings.codex_runner_token == "runner-token-must-be-at-least-32-characters"
    assert settings.codex_home == ""


def test_load_settings_codex_runner_requires_private_api_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner 백엔드는 API가 전송할 별도 내부 토큰 없이는 기동하지 않는다."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("LLM_BACKEND", "codex_runner")
    monkeypatch.setenv("CODEX_RUNNER_URL", "http://runner:8080")
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch@localhost:5432/orch")
    monkeypatch.delenv("ORCH_RUNNER_TOKEN", raising=False)

    with pytest.raises(ValueError, match="ORCH_RUNNER_TOKEN"):
        load_settings()


def test_load_settings_codex_runner_rejects_shared_api_and_runner_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """서로 다른 인증 경계를 같은 토큰으로 합치면 API 기동을 거부한다."""
    shared_token = "test-shared-token-must-be-at-least-32-characters"
    _set_required_env(monkeypatch)
    monkeypatch.setenv("LLM_BACKEND", "codex_runner")
    monkeypatch.setenv("CODEX_RUNNER_URL", "http://runner:8080")
    monkeypatch.setenv("ORCH_API_TOKEN", shared_token)
    monkeypatch.setenv("ORCH_RUNNER_TOKEN", shared_token)
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch@localhost:5432/orch")

    with pytest.raises(ValueError, match="must differ"):
        load_settings()


def test_load_settings_requires_dedicated_executor_api_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """executor 내부 API는 일반 API와 별도의 필수 토큰 없이는 기동하지 않는다."""
    _set_required_env(monkeypatch)
    monkeypatch.delenv("ORCH_EXECUTOR_API_TOKEN")

    with pytest.raises(ValueError, match="ORCH_EXECUTOR_API_TOKEN"):
        load_settings()


@pytest.mark.parametrize("shared_with", ("ORCH_API_TOKEN", "ORCH_RUNNER_TOKEN"))
def test_load_settings_rejects_executor_token_shared_with_other_api_boundary(
    monkeypatch: pytest.MonkeyPatch,
    shared_with: str,
) -> None:
    """일반 API·Runner·executor 인증 경계를 같은 토큰으로 합치지 않는다."""
    shared_token = "test-shared-token-must-be-at-least-32-characters"
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ORCH_EXECUTOR_API_TOKEN", shared_token)
    if shared_with == "ORCH_API_TOKEN":
        monkeypatch.setenv("ORCH_API_TOKEN", shared_token)
    else:
        monkeypatch.setenv("LLM_BACKEND", "codex_runner")
        monkeypatch.setenv("CODEX_RUNNER_URL", "http://runner:8080")
        monkeypatch.setenv("ORCH_RUNNER_TOKEN", shared_token)

    with pytest.raises(ValueError, match="must differ"):
        load_settings()


@pytest.mark.parametrize("runner_url", ("", "runner:8080", "/v1/generate"))
def test_load_settings_codex_runner_requires_absolute_url(
    monkeypatch: pytest.MonkeyPatch,
    runner_url: str,
) -> None:
    """API가 private Runner에 요청을 위임하려면 절대 URL이 필요하다."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("LLM_BACKEND", "codex_runner")
    monkeypatch.setenv("CODEX_RUNNER_URL", runner_url)
    monkeypatch.setenv("ORCH_RUNNER_TOKEN", "runner-token-must-be-at-least-32-characters")
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch@localhost:5432/orch")

    with pytest.raises(ValueError, match="CODEX_RUNNER_URL"):
        load_settings()


def test_load_settings_uses_default_for_blank_openai_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """빈 OPENAI_MODEL은 요청 시점 실패 대신 안전한 기본 모델로 정규화한다."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "   ")
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch:pw@localhost:5432/orch")

    settings = load_settings()

    assert settings.openai_model == "gpt-5.3-codex-spark"


@pytest.mark.parametrize("table_name", ("", "chat-interactions"))
def test_load_settings_rejects_invalid_interactions_table(
    monkeypatch: pytest.MonkeyPatch,
    table_name: str,
) -> None:
    """테이블 설정 오류는 DB 기동 전 환경 검증 단계에서 거부한다."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch:pw@localhost:5432/orch")
    monkeypatch.setenv("ORCH_INTERACTIONS_TABLE", table_name)

    with pytest.raises(ValueError, match="ORCH_INTERACTIONS_TABLE"):
        load_settings()


@pytest.mark.parametrize(
    ("name", "attribute", "default"),
    [
        ("CODEX_TIMEOUT_SEC", "codex_timeout_sec", 110),
        ("CODEX_RUNNER_TIMEOUT_SEC", "codex_runner_timeout_sec", 120),
        ("OPENAI_MAX_TOKENS", "openai_max_tokens", 1024),
        ("OPENAI_TIMEOUT_SEC", "openai_timeout_sec", 60),
        ("ORCH_DB_CONNECT_TIMEOUT_SEC", "database_connect_timeout_sec", 10),
    ],
)
def test_load_settings_uses_default_for_blank_numeric_value(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    attribute: str,
    default: int,
) -> None:
    """빈 선택 환경 변수는 .env.example의 기본값 계약을 따른다."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch:pw@localhost:5432/orch")
    monkeypatch.setenv(name, "")

    settings = load_settings()

    assert getattr(settings, attribute) == default


def test_api_entrypoint_reports_missing_runtime_dir() -> None:
    """API 컨테이너는 bootstrap runtime 경로 누락 원인을 직접 출력한다."""
    result = subprocess.run(
        ["/bin/sh", "agent_orchestration/entrypoint.sh"],
        capture_output=True,
        check=False,
        env={"PATH": os.environ["PATH"]},
        text=True,
    )

    assert result.returncode != 0
    assert "ORCH_RUNTIME_DIR is required" in result.stderr


def test_api_entrypoint_reads_database_url_without_shell_evaluation(tmp_path: Path) -> None:
    """DB runtime 파일의 URL 값은 셸 명령으로 해석하지 않는다."""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    marker = tmp_path / "must-not-exist"
    database_url = f"postgresql://orch:pw@$(touch {marker})/orch"
    (runtime_dir / "db.env").write_text(
        f"ORCH_DATABASE_URL={database_url}\n",
        encoding="utf-8",
    )
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    uvicorn = executable_dir / "uvicorn"
    uvicorn.write_text('#!/bin/sh\nprintf "%s" "$ORCH_DATABASE_URL"\n', encoding="utf-8")
    uvicorn.chmod(0o755)

    result = subprocess.run(
        ["/bin/sh", "agent_orchestration/entrypoint.sh"],
        capture_output=True,
        check=False,
        env={
            "ORCH_RUNTIME_DIR": str(runtime_dir),
            "PATH": str(executable_dir) + os.pathsep + os.environ["PATH"],
        },
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout == database_url
    assert not marker.exists()


def test_load_settings_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """필수 DB URL이 없으면 명확한 오류를 던진다."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("ORCH_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="Either ORCH_DATABASE_URL or DATABASE_URL is required"):
        load_settings()


def test_load_settings_requires_api_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """무인증 LLM 호출을 막기 위해 공유 API 토큰을 필수로 요구한다."""
    _set_required_env(monkeypatch)
    monkeypatch.delenv("ORCH_API_TOKEN", raising=False)
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch:pw@localhost:5432/orch")

    with pytest.raises(ValueError, match="ORCH_API_TOKEN"):
        load_settings()


def test_load_settings_requires_long_enough_api_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """공유 토큰이 문서 계약보다 짧으면 기동을 거부한다."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ORCH_API_TOKEN", "too-short")
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch:pw@localhost:5432/orch")

    with pytest.raises(ValueError, match="ORCH_API_TOKEN"):
        load_settings()


@pytest.mark.parametrize("name", ("CODEX_CLI_PATH", "CODEX_HOME"))
def test_load_settings_rejects_whitespace_only_required_values(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """공백뿐인 필수 설정은 기동 시점에 거부한다."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch:pw@localhost:5432/orch")
    monkeypatch.setenv(name, "   ")

    with pytest.raises(ValueError, match=name):
        load_settings()


@pytest.mark.parametrize(
    "name",
    (
        "CODEX_TIMEOUT_SEC",
        "CODEX_RUNNER_TIMEOUT_SEC",
        "OPENAI_MAX_TOKENS",
        "OPENAI_TIMEOUT_SEC",
        "ORCH_DB_CONNECT_TIMEOUT_SEC",
    ),
)
@pytest.mark.parametrize("value", ("0", "-1"))
def test_load_settings_rejects_non_positive_numeric_limits(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    """0 이하의 요청 제한값은 기동 시점에 거부한다."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch:pw@localhost:5432/orch")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        load_settings()


def test_api_token_comparison_handles_non_ascii_values() -> None:
    """비 ASCII 토큰은 500 없이 불일치 인증으로 처리한다."""
    assert not main_module._api_tokens_match("잘못된 토큰", "정상 토큰")


def test_generate_response_uses_read_only_ephemeral_codex_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex 호출은 전용 홈·정제된 환경의 읽기 전용 일회성 프로세스다."""
    command: tuple[str, ...] = ()
    stdin_payload: bytes | None = None
    received_kwargs: dict[str, object] = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, input: bytes) -> tuple[bytes, bytes]:
            nonlocal stdin_payload
            stdin_payload = input
            output_path = Path(command[command.index("-o") + 1])
            output_path.write_text("local answer\n", encoding="utf-8")
            return b"", b""

    async def fake_create_subprocess_exec(*args: str, **kwargs: object) -> FakeProcess:
        nonlocal command
        command = args
        received_kwargs.update(kwargs)
        return FakeProcess()

    settings = ServiceSettings(
        openai_api_key=None,
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
        api_token="test-api-token",
        codex_home="/tmp/test-codex-home",
        github_token="x" * 40,
        github_repository="SKYAHO/Autoresearch",
        gh_timeout_sec=30,
        issue_daily_limit=20,
        experiment_defaults=ExperimentDefaults(
            dataset_source="feast://feast_offline_store/ctr_training_v1",
            training_config_ref="configs/train/x.yaml@abc",
        ),
    )
    monkeypatch.setattr(codex_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = asyncio.run(generate_response(settings, "질문"))

    assert result == LLMResult(text="local answer", model="codex-cli", token_count=None)
    assert command[:2] == ("codex", "exec")
    assert "--sandbox" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in command
    assert "--skip-git-repo-check" in command
    assert stdin_payload == "질문".encode()
    assert received_kwargs["start_new_session"] is True
    codex_workdir = command[command.index("-C") + 1]
    assert received_kwargs["env"] == {
        "CODEX_HOME": "/tmp/test-codex-home",
        "HOME": codex_workdir,
        "TMPDIR": codex_workdir,
        "XDG_CACHE_HOME": codex_workdir,
        "XDG_STATE_HOME": codex_workdir,
        "PATH": received_kwargs["env"]["PATH"],
    }


def test_generate_codex_cli_passes_configured_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """CODEX_MODEL은 CLI 모델 인자와 저장 모델명에 그대로 반영한다."""
    command: tuple[str, ...] = ()

    class FakeProcess:
        returncode = 0

        async def communicate(self, input: bytes) -> tuple[bytes, bytes]:
            output_path = Path(command[command.index("-o") + 1])
            output_path.write_text("model answer", encoding="utf-8")
            return b"", b""

    async def fake_create_subprocess_exec(*args: str, **_kwargs: object) -> FakeProcess:
        nonlocal command
        command = args
        return FakeProcess()

    settings = ServiceSettings(
        openai_api_key=None,
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
        api_token="test-api-token",
        codex_home="/tmp/test-codex-home",
        codex_model="gpt-5.3-codex-spark",
        github_token="x" * 40,
        github_repository="SKYAHO/Autoresearch",
        gh_timeout_sec=30,
        issue_daily_limit=20,
        experiment_defaults=ExperimentDefaults(
            dataset_source="feast://feast_offline_store/ctr_training_v1",
            training_config_ref="configs/train/x.yaml@abc",
        ),
    )
    monkeypatch.setattr(codex_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = asyncio.run(generate_response(settings, "질문"))

    assert command[command.index("-m") + 1] == "gpt-5.3-codex-spark"
    assert result.model == "gpt-5.3-codex-spark"


@pytest.mark.parametrize(
    ("returncode", "output", "expected_message"),
    [
        (1, None, "Codex CLI failed."),
        (0, None, "Codex CLI returned no output."),
        (0, "  \n", "Codex CLI returned empty output."),
    ],
)
def test_generate_codex_cli_rejects_invalid_process_output(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    output: str | None,
    expected_message: str,
) -> None:
    """비정상 종료·출력 누락·공백 출력은 LLM 오류로 정규화한다."""
    command: tuple[str, ...] = ()

    class FakeProcess:
        def __init__(self) -> None:
            self.returncode = returncode

        async def communicate(self, input: bytes) -> tuple[bytes, bytes]:
            if output is not None:
                output_path = Path(command[command.index("-o") + 1])
                output_path.write_text(output, encoding="utf-8")
            return b"", b"Codex CLI diagnostic"

    async def fake_create_subprocess_exec(*args: str, **_kwargs: object) -> FakeProcess:
        nonlocal command
        command = args
        return FakeProcess()

    settings = ServiceSettings(
        openai_api_key=None,
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
        api_token="test-api-token",
        codex_home="/tmp/test-codex-home",
        github_token="x" * 40,
        github_repository="SKYAHO/Autoresearch",
        gh_timeout_sec=30,
        issue_daily_limit=20,
        experiment_defaults=ExperimentDefaults(
            dataset_source="feast://feast_offline_store/ctr_training_v1",
            training_config_ref="configs/train/x.yaml@abc",
        ),
    )
    monkeypatch.setattr(codex_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(LLMBackendError, match=expected_message):
        asyncio.run(generate_response(settings, "질문"))


def test_generate_codex_cli_terminates_process_group_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시간 초과 시 Codex와 같은 세션의 자식 프로세스까지 종료한다."""
    process_group_terminated = False

    class FakeProcess:
        returncode = None
        pid = 123

        async def communicate(self, input: bytes) -> tuple[bytes, bytes]:
            await asyncio.sleep(2)
            return b"", b""

    async def fake_create_subprocess_exec(*_args: str, **_kwargs: object) -> FakeProcess:
        return FakeProcess()

    def fake_terminate_process_group(_process: FakeProcess) -> None:
        nonlocal process_group_terminated
        process_group_terminated = True

    settings = ServiceSettings(
        openai_api_key=None,
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
        api_token="test-api-token",
        codex_home="/tmp/test-codex-home",
        codex_timeout_sec=1,
        github_token="x" * 40,
        github_repository="SKYAHO/Autoresearch",
        gh_timeout_sec=30,
        issue_daily_limit=20,
        experiment_defaults=ExperimentDefaults(
            dataset_source="feast://feast_offline_store/ctr_training_v1",
            training_config_ref="configs/train/x.yaml@abc",
        ),
    )
    monkeypatch.setattr(codex_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(codex_module, "_terminate_process_group", fake_terminate_process_group)

    with pytest.raises(LLMBackendError, match="Codex CLI timed out."):
        asyncio.run(generate_response(settings, "질문"))

    assert process_group_terminated


@pytest.mark.skipif(os.name != "posix", reason="process group is POSIX-specific")
def test_terminate_process_group_targets_dedicated_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex가 만든 새 세션의 PGID로 종료 신호를 보낸다."""
    killed: list[tuple[int, signal.Signals]] = []
    process = SimpleNamespace(returncode=None, pid=12345)

    monkeypatch.setattr(codex_module.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    codex_module._terminate_process_group(process)

    assert killed == [(12345, signal.SIGKILL)]


def test_generate_codex_cli_omits_stderr_from_timeout_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """시간 초과 stderr 원문은 프롬프트·자격 증명과 함께 로그에서 제외한다."""
    prompt = "비밀 프롬프트"
    opaque_credential = "unstructured oauth credential material"
    subprocess_options: dict[str, object] = {}

    class FakeProcess:
        returncode = -9
        pid = 123

        async def communicate(self, input: bytes) -> tuple[bytes, bytes]:
            await asyncio.sleep(2)
            return b"", f"{opaque_credential}\nfailed for {input.decode()}".encode()

    async def fake_create_subprocess_exec(*_args: str, **kwargs: object) -> FakeProcess:
        subprocess_options.update(kwargs)
        return FakeProcess()

    settings = ServiceSettings(
        openai_api_key=None,
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
        api_token="test-api-token",
        codex_home="/tmp/test-codex-home",
        codex_timeout_sec=1,
        github_token="x" * 40,
        github_repository="SKYAHO/Autoresearch",
        gh_timeout_sec=30,
        issue_daily_limit=20,
        experiment_defaults=ExperimentDefaults(
            dataset_source="feast://feast_offline_store/ctr_training_v1",
            training_config_ref="configs/train/x.yaml@abc",
        ),
    )
    monkeypatch.setattr(codex_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(codex_module, "_terminate_process_group", lambda _process: None)
    caplog.set_level(logging.WARNING, logger=codex_module.__name__)

    with pytest.raises(LLMBackendError, match="Codex CLI timed out."):
        asyncio.run(generate_response(settings, prompt))

    assert prompt not in caplog.text
    assert opaque_credential not in caplog.text
    assert subprocess_options["stderr"] is asyncio.subprocess.DEVNULL


def test_generate_codex_cli_terminates_process_group_when_request_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """요청 취소는 실행 중인 Codex 프로세스 그룹을 즉시 정리한다."""
    process_group_terminated = False
    started = asyncio.Event()
    terminated = asyncio.Event()

    class FakeProcess:
        returncode = None
        pid = 123

        async def communicate(self, _input: bytes) -> tuple[bytes, bytes]:
            started.set()
            await terminated.wait()
            return b"", b""

    async def fake_create_subprocess_exec(*_args: str, **_kwargs: object) -> FakeProcess:
        return FakeProcess()

    def fake_terminate_process_group(_process: FakeProcess) -> None:
        nonlocal process_group_terminated
        process_group_terminated = True
        terminated.set()

    settings = ServiceSettings(
        openai_api_key=None,
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
        api_token="test-api-token",
        codex_home="/tmp/test-codex-home",
        github_token="x" * 40,
        github_repository="SKYAHO/Autoresearch",
        gh_timeout_sec=30,
        issue_daily_limit=20,
        experiment_defaults=ExperimentDefaults(
            dataset_source="feast://feast_offline_store/ctr_training_v1",
            training_config_ref="configs/train/x.yaml@abc",
        ),
    )
    monkeypatch.setattr(codex_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(codex_module, "_terminate_process_group", fake_terminate_process_group)

    async def cancel_request() -> None:
        request_task = asyncio.create_task(generate_response(settings, "질문"))
        await started.wait()
        request_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request_task

    asyncio.run(cancel_request())

    assert process_group_terminated


def test_db_validate_table_name() -> None:
    """잘못된 테이블 이름은 저장 시점에서 거부한다."""
    with pytest.raises(ValueError, match="Invalid table name"):
        db_module._validate_identifier("chat-interactions")


def test_ensure_schema_executes_ddl_with_expected_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """스키마 보장 호출은 안전한 쿼리를 실행한다."""
    executed: list[str] = []
    connect_kwargs: dict[str, object] = {}

    class FakeCursor:
        def __enter__(self) -> "FakeCursor":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def execute(self, query: object, params: object = None) -> None:
            rendered = query.as_string(None) if hasattr(query, "as_string") else str(query)
            executed.append(rendered)

    class FakeConnection:
        def __enter__(self) -> "FakeConnection":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def __getattr__(self, name: str):
            if name == "cursor":
                return lambda: FakeCursor()
            raise AttributeError(name)

        def commit(self) -> None:
            return None

    def connect_factory(_db_url: str, **kwargs: object) -> FakeConnection:
        connect_kwargs.update(kwargs)
        return FakeConnection()

    monkeypatch.setattr(db_module, "connect", connect_factory)
    db_module.ensure_schema("postgresql://example", "chat_interactions", connect_timeout_sec=7)

    assert any("pg_advisory_xact_lock" in query for query in executed)
    assert any('CREATE TABLE IF NOT EXISTS "chat_interactions"' in query for query in executed)
    assert connect_kwargs == {"connect_timeout": 7}


def test_db_quotes_reserved_table_name_in_ddl_and_insert(monkeypatch: pytest.MonkeyPatch) -> None:
    """예약어 테이블명도 DDL과 INSERT에서 동일하게 식별자로 인용한다."""
    executed: list[str] = []

    class FakeCursor:
        def __enter__(self) -> "FakeCursor":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def execute(self, query: object, params: object = None) -> None:
            rendered = query.as_string(None) if hasattr(query, "as_string") else str(query)
            executed.append(rendered)

        def fetchone(self) -> db_module.ChatRow:
            return db_module.ChatRow(
                id=1,
                prompt="prompt",
                response="response",
                model="model",
                latency_ms=1,
                token_count=None,
                created_at="2026-07-30T00:00:00Z",  # type: ignore[arg-type]
            )

    class FakeConnection:
        def __enter__(self) -> "FakeConnection":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def commit(self) -> None:
            return None

    monkeypatch.setattr(db_module, "connect", lambda *_args, **_kwargs: FakeConnection())

    db_module.ensure_schema("postgresql://example", "order")
    db_module.save_interaction(
        "postgresql://example",
        "order",
        "prompt",
        "response",
        "model",
        1,
        None,
    )

    assert any('CREATE TABLE IF NOT EXISTS "order"' in query for query in executed)
    assert any('INSERT INTO "order"' in query for query in executed)


def test_main_startup_fails_when_runtime_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DB 또는 설정 초기화 실패는 프로세스를 기동하지 않고 종료한다."""
    monkeypatch.setattr(
        main_module,
        "load_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("bootstrap failed")),
    )

    app = main_module.create_app()
    with pytest.raises(RuntimeError, match="bootstrap failed"):
        with TestClient(app):
            pass


def test_main_chat_succeeds_after_mocked_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """정상 경로에서 201과 저장된 레코드가 그대로 반환된다."""

    settings = ServiceSettings(
        openai_api_key=None,
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
        api_token="test-api-token",
        github_token="x" * 40,
        github_repository="SKYAHO/Autoresearch",
        gh_timeout_sec=30,
        issue_daily_limit=20,
        experiment_defaults=ExperimentDefaults(
            dataset_source="feast://feast_offline_store/ctr_training_v1",
            training_config_ref="configs/train/x.yaml@abc",
        ),
    )

    async def fake_generate_response(*_args: object, **_kwargs: object) -> LLMResult:
        return LLMResult(text="안녕하세요", model="codex-cli", token_count=None)

    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(main_module, "ensure_schema", lambda *_args: None)
    monkeypatch.setattr(main_module, "generate_response", fake_generate_response)
    monkeypatch.setattr(
        main_module,
        "save_interaction",
        lambda **kwargs: SimpleNamespace(
            id=12,
            prompt=kwargs["prompt"],
            response=kwargs["response"],
            model=kwargs["model"],
            latency_ms=kwargs["latency_ms"],
            token_count=kwargs["token_count"],
            created_at="2026-07-30T00:00:00Z",
        ),
    )

    app = main_module.create_app()
    with TestClient(app) as client:
        response = client.post(
            "/chat",
            headers={"X-Orch-Token": "test-api-token"},
            json={"prompt": "테스트"},
        )

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["id"] == 12
    assert body["response"] == "안녕하세요"
    assert body["model"] == "codex-cli"
    assert body["token_count"] is None


def test_main_chat_rejects_missing_api_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """공유 API 토큰이 없는 요청은 LLM 호출 전에 거부한다."""
    settings = ServiceSettings(
        openai_api_key=None,
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
        api_token="test-api-token",
        github_token="x" * 40,
        github_repository="SKYAHO/Autoresearch",
        gh_timeout_sec=30,
        issue_daily_limit=20,
        experiment_defaults=ExperimentDefaults(
            dataset_source="feast://feast_offline_store/ctr_training_v1",
            training_config_ref="configs/train/x.yaml@abc",
        ),
    )

    async def unexpected_generate_response(*_args: object, **_kwargs: object) -> LLMResult:
        raise AssertionError("LLM must not be called without an API token")

    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(main_module, "ensure_schema", lambda *_args: None)
    monkeypatch.setattr(main_module, "generate_response", unexpected_generate_response)

    app = main_module.create_app()
    with TestClient(app) as client:
        response = client.post("/chat", json={"prompt": "테스트"})

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_main_chat_rejects_unknown_request_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """현재 계약에 없는 model_key는 조용히 무시하지 않고 거부한다."""
    settings = ServiceSettings(
        openai_api_key=None,
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
        api_token="test-api-token",
        github_token="x" * 40,
        github_repository="SKYAHO/Autoresearch",
        gh_timeout_sec=30,
        issue_daily_limit=20,
        experiment_defaults=ExperimentDefaults(
            dataset_source="feast://feast_offline_store/ctr_training_v1",
            training_config_ref="configs/train/x.yaml@abc",
        ),
    )

    async def fake_generate_response(*_args: object, **_kwargs: object) -> LLMResult:
        return LLMResult(text="unused", model="unused", token_count=None)

    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(main_module, "ensure_schema", lambda *_args: None)
    monkeypatch.setattr(main_module, "generate_response", fake_generate_response)
    monkeypatch.setattr(
        main_module,
        "save_interaction",
        lambda **kwargs: SimpleNamespace(
            id=1,
            prompt=kwargs["prompt"],
            response=kwargs["response"],
            model=kwargs["model"],
            latency_ms=kwargs["latency_ms"],
            token_count=kwargs["token_count"],
            created_at="2026-07-30T00:00:00Z",
        ),
    )

    app = main_module.create_app()
    with TestClient(app) as client:
        response = client.post(
            "/chat",
            headers={"X-Orch-Token": "test-api-token"},
            json={"prompt": "테스트", "model_key": "fast"},
        )

    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT


def test_chat_openapi_documents_required_token_and_error_responses() -> None:
    """Swagger 계약은 실제 인증 헤더와 오류 상태 코드를 노출한다."""
    operation = main_module.create_app().openapi()["paths"]["/chat"]["post"]
    token_parameters = [
        parameter
        for parameter in operation["parameters"]
        if parameter["name"] == "X-Orch-Token" and parameter["in"] == "header"
    ]

    assert len(token_parameters) == 1
    token_parameter = token_parameters[0]
    assert token_parameter["required"] is True
    assert token_parameter["schema"] == {"type": "string"}
    assert "401" in operation["responses"]
    assert "500" in operation["responses"]
    assert "502" in operation["responses"]
    assert "503" in operation["responses"]
    assert operation["responses"]["401"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }


def test_generate_response_uses_responses_api_for_openai_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """향후 OpenAI API 백엔드는 Responses API의 입력·출력 계약으로 처리한다."""
    settings = ServiceSettings(
        openai_api_key="test-key",
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
        api_token="test-api-token",
        llm_backend="openai",
        github_token="x" * 40,
        github_repository="SKYAHO/Autoresearch",
        gh_timeout_sec=30,
        issue_daily_limit=20,
        experiment_defaults=ExperimentDefaults(
            dataset_source="feast://feast_offline_store/ctr_training_v1",
            training_config_ref="configs/train/x.yaml@abc",
        ),
    )
    received_request: dict[str, object] = {}

    class FakeResponses:
        async def create(self, **kwargs):
            received_request.update(kwargs)
            return SimpleNamespace(
                status="completed",
                output_text="Responses API 응답",
                usage=SimpleNamespace(total_tokens=11),
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(llm_module, "AsyncOpenAI", FakeOpenAI)

    result = asyncio.run(generate_response(settings, "Responses API로 호출"))

    assert result == LLMResult(
        text="Responses API 응답",
        model="gpt-5.3-codex-spark",
        token_count=11,
    )
    assert received_request == {
        "model": "gpt-5.3-codex-spark",
        "input": "Responses API로 호출",
        "max_output_tokens": 1024,
    }


def test_generate_openai_response_rejects_incomplete_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """출력 토큰 제한 등으로 끝나지 않은 OpenAI 응답은 저장하지 않는다."""
    settings = ServiceSettings(
        openai_api_key="test-key",
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
        api_token="test-api-token",
        llm_backend="openai",
        github_token="x" * 40,
        github_repository="SKYAHO/Autoresearch",
        gh_timeout_sec=30,
        issue_daily_limit=20,
        experiment_defaults=ExperimentDefaults(
            dataset_source="feast://feast_offline_store/ctr_training_v1",
            training_config_ref="configs/train/x.yaml@abc",
        ),
    )

    class FakeResponses:
        async def create(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(
                status="incomplete",
                output_text="잘린 응답",
                usage=SimpleNamespace(total_tokens=11),
            )

    class FakeOpenAI:
        def __init__(self, **_kwargs: object) -> None:
            self.responses = FakeResponses()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(llm_module, "AsyncOpenAI", FakeOpenAI)

    with pytest.raises(LLMBackendError, match="did not complete"):
        asyncio.run(generate_response(settings, "Responses API로 호출"))


def test_app_package_does_not_eagerly_import_fastapi_application() -> None:
    """패키지 import는 환경을 읽는 FastAPI 앱 생성을 유발하지 않는다."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import agent_orchestration.app; import sys; "
            "assert 'agent_orchestration.app.main' not in sys.modules",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_main_chat_returns_bad_gateway_when_codex_cli_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex CLI 실패는 공통 LLM 백엔드 오류로 502를 반환한다."""
    settings = ServiceSettings(
        openai_api_key=None,
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
        api_token="test-api-token",
        github_token="x" * 40,
        github_repository="SKYAHO/Autoresearch",
        gh_timeout_sec=30,
        issue_daily_limit=20,
        experiment_defaults=ExperimentDefaults(
            dataset_source="feast://feast_offline_store/ctr_training_v1",
            training_config_ref="configs/train/x.yaml@abc",
        ),
    )

    async def failing_generate_response(*_args: object, **_kwargs: object) -> LLMResult:
        raise LLMBackendError("Codex CLI failed")

    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(main_module, "ensure_schema", lambda *_args: None)
    monkeypatch.setattr(main_module, "generate_response", failing_generate_response)

    app = main_module.create_app()
    with TestClient(app) as client:
        response = client.post(
            "/chat",
            headers={"X-Orch-Token": "test-api-token"},
            json={"prompt": "테스트"},
        )

    assert response.status_code == status.HTTP_502_BAD_GATEWAY


def test_main_chat_returns_service_unavailable_when_runner_is_overloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner 과부하는 API가 502가 아닌 503으로 호출자에게 전달한다."""
    from agent_orchestration.contracts import LLMBackendOverloadedError

    settings = ServiceSettings(
        openai_api_key=None,
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
        api_token="test-api-token",
        github_token="x" * 40,
        github_repository="SKYAHO/Autoresearch",
        gh_timeout_sec=30,
        issue_daily_limit=20,
        experiment_defaults=ExperimentDefaults(
            dataset_source="feast://feast_offline_store/ctr_training_v1",
            training_config_ref="configs/train/x.yaml@abc",
        ),
    )

    async def overloaded_generate_response(*_args: object, **_kwargs: object) -> LLMResult:
        raise LLMBackendOverloadedError("Codex runner is overloaded.")

    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(main_module, "ensure_schema", lambda *_args: None)
    monkeypatch.setattr(main_module, "generate_response", overloaded_generate_response)

    app = main_module.create_app()
    with TestClient(app) as client:
        response = client.post(
            "/chat",
            headers={"X-Orch-Token": "test-api-token"},
            json={"prompt": "테스트"},
        )

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_issue_publication_settings_are_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """발행 경로가 요구하는 설정이 누락되면 기동 시점에 드러나야 한다."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ORCH_GITHUB_TOKEN", "x" * 40)
    monkeypatch.setenv("ORCH_GITHUB_REPOSITORY", "SKYAHO/Autoresearch")
    monkeypatch.setenv(
        "ORCH_EXPERIMENT_DATASET_SOURCE", "feast://feast_offline_store/ctr_training_v1"
    )
    monkeypatch.setenv("ORCH_EXPERIMENT_TRAINING_CONFIG_REF", "configs/train/x.yaml@abc")

    settings = load_settings()

    assert settings.github_repository == "SKYAHO/Autoresearch"
    assert settings.baseline_github_app_id == 123
    assert settings.baseline_github_app_installation_id == 456
    assert settings.baseline_github_app_private_key_path == Path(
        "/var/run/test/baseline-app.pem"
    )
    assert settings.gh_timeout_sec == 30
    assert settings.issue_daily_limit == 20
    assert (
        settings.experiment_defaults.dataset_source
        == "feast://feast_offline_store/ctr_training_v1"
    )


def test_github_repository_must_be_owner_slash_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """발행 대상 저장소를 잘못 두면 다른 저장소에 이슈가 열린다."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ORCH_GITHUB_TOKEN", "x" * 40)
    monkeypatch.setenv("ORCH_GITHUB_REPOSITORY", "Autoresearch")
    monkeypatch.setenv(
        "ORCH_EXPERIMENT_DATASET_SOURCE", "feast://feast_offline_store/ctr_training_v1"
    )
    monkeypatch.setenv("ORCH_EXPERIMENT_TRAINING_CONFIG_REF", "configs/train/x.yaml@abc")

    with pytest.raises(ValueError, match="ORCH_GITHUB_REPOSITORY"):
        load_settings()
