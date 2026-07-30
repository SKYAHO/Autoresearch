"""Agent Orchestration 1단계 모듈 스켈레톤 테스트."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

from fastapi import status
from fastapi.testclient import TestClient
import pytest

from agent_orchestration.app.config import ServiceSettings, load_settings
from agent_orchestration.app import db as db_module
from agent_orchestration.app import llm as llm_module
from agent_orchestration.app import main as main_module
from agent_orchestration.app.llm import LLMBackendError, LLMResult, generate_response


_SETTINGS_ENV_VARS = (
    "CODEX_CLI_PATH",
    "CODEX_HOME",
    "CODEX_MODEL",
    "CODEX_TIMEOUT_SEC",
    "DATABASE_URL",
    "LLM_BACKEND",
    "OPENAI_API_KEY",
    "OPENAI_MAX_TOKENS",
    "OPENAI_MODEL",
    "OPENAI_TIMEOUT_SEC",
    "ORCH_API_TOKEN",
    "ORCH_DATABASE_URL",
    "ORCH_DB_CONNECT_TIMEOUT_SEC",
    "ORCH_INTERACTIONS_TABLE",
)


@pytest.fixture(autouse=True)
def clear_agent_orchestration_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """설정 테스트가 개발 셸의 환경 변수에 영향을 받지 않게 한다."""
    for name in _SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ORCH_API_TOKEN", "test-api-token")
    monkeypatch.setenv("CODEX_HOME", "/tmp/test-codex-home")


def test_load_settings_prefers_orchestration_database_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """ORCH_DATABASE_URL이 설정되면 DATABASE_URL보다 우선 사용한다."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch:pw@localhost:5432/orch_db")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fallback:pw@localhost:5432/fallback")

    settings = load_settings()

    assert settings.openai_model == "gpt-5.3-codex-spark"
    assert settings.database_url == "postgresql://orch:pw@localhost:5432/orch_db"
    assert settings.interactions_table == "chat_interactions"
    assert settings.api_token == "test-api-token"
    assert settings.codex_home == "/tmp/test-codex-home"


def test_load_settings_accepts_database_url_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """ORCH_DATABASE_URL 미설정 시 DATABASE_URL을 사용한다."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("ORCH_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgres://generic:pw@localhost:5432/generic_db")

    settings = load_settings()

    assert settings.database_url == "postgres://generic:pw@localhost:5432/generic_db"


def test_load_settings_allows_codex_without_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex CLI 공용 계정 모드는 OpenAI API 키를 요구하지 않는다."""
    monkeypatch.setenv("LLM_BACKEND", "codex_cli")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch:pw@localhost:5432/orch")

    settings = load_settings()

    assert settings.llm_backend == "codex_cli"
    assert settings.openai_api_key is None


def test_load_settings_requires_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """필수 DB URL이 없으면 명확한 오류를 던진다."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("ORCH_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="Either ORCH_DATABASE_URL or DATABASE_URL is required"):
        load_settings()


def test_load_settings_requires_api_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """무인증 LLM 호출을 막기 위해 공유 API 토큰을 필수로 요구한다."""
    monkeypatch.delenv("ORCH_API_TOKEN", raising=False)
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch:pw@localhost:5432/orch")

    with pytest.raises(ValueError, match="ORCH_API_TOKEN"):
        load_settings()


@pytest.mark.parametrize(
    "name",
    (
        "CODEX_TIMEOUT_SEC",
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
    )
    monkeypatch.setattr(llm_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = asyncio.run(generate_response(settings, "질문"))

    assert result == LLMResult(text="local answer", model="codex-cli", token_count=None)
    assert command[:2] == ("codex", "exec")
    assert "--sandbox" in command
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in command
    assert "--skip-git-repo-check" in command
    assert stdin_payload == "질문".encode()
    assert received_kwargs["start_new_session"] is True
    assert received_kwargs["env"] == {
        "CODEX_HOME": "/tmp/test-codex-home",
        "HOME": "/tmp/test-codex-home",
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
    )
    monkeypatch.setattr(llm_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

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
    )
    monkeypatch.setattr(llm_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    with pytest.raises(LLMBackendError, match=expected_message):
        asyncio.run(generate_response(settings, "질문"))


def test_generate_codex_cli_terminates_process_group_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시간 초과 시 Codex와 같은 세션의 자식 프로세스까지 종료한다."""
    process_group_terminated = False

    class FakeProcess:
        returncode = -9
        pid = 123

        async def communicate(self, input: bytes) -> tuple[bytes, bytes]:
            await asyncio.sleep(1)
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
        codex_timeout_sec=0,
    )
    monkeypatch.setattr(llm_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(llm_module, "_terminate_process_group", fake_terminate_process_group)

    with pytest.raises(LLMBackendError, match="Codex CLI timed out."):
        asyncio.run(generate_response(settings, "질문"))

    assert process_group_terminated


def test_generate_codex_cli_logs_redacted_stderr_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """시간 초과 후 회수한 stderr는 프롬프트를 제거한 서버 로그로 남긴다."""
    prompt = "비밀 프롬프트"

    class FakeProcess:
        returncode = -9
        pid = 123

        async def communicate(self, input: bytes) -> tuple[bytes, bytes]:
            await asyncio.sleep(0.01)
            return b"", f"failed for {input.decode()}".encode()

    async def fake_create_subprocess_exec(*_args: str, **_kwargs: object) -> FakeProcess:
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
        codex_timeout_sec=0,
    )
    monkeypatch.setattr(llm_module.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(llm_module, "_terminate_process_group", lambda _process: None)
    caplog.set_level(logging.WARNING, logger=llm_module.__name__)

    with pytest.raises(LLMBackendError, match="Codex CLI timed out."):
        asyncio.run(generate_response(settings, prompt))

    assert "[REDACTED_PROMPT]" in caplog.text
    assert prompt not in caplog.text


def test_redact_stderr_removes_credential_like_values() -> None:
    """운영 stderr 로그에서 자격 증명 값은 남기지 않는다."""
    redacted = llm_module._redact_stderr(
        b"refresh_token=shared-oauth-secret\nauthorization: Bearer another-secret",
        "unrelated prompt",
    )

    assert "shared-oauth-secret" not in redacted
    assert "another-secret" not in redacted
    assert redacted.count("[REDACTED_SECRET]") == 2


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

        def execute(self, query: str) -> None:
            executed.append(query)

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

    assert any("CREATE TABLE IF NOT EXISTS chat_interactions" in query for query in executed)
    assert connect_kwargs == {"connect_timeout": 7}


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
    )
    received_request: dict[str, object] = {}

    class FakeResponses:
        async def create(self, **kwargs):
            received_request.update(kwargs)
            return SimpleNamespace(
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
