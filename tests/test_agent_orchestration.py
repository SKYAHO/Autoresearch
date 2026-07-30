"""Agent Orchestration 1단계 모듈 스켈레톤 테스트."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi import status
from fastapi.testclient import TestClient
from openai import OpenAIError
import pytest

from agent_orchestration.app.config import ServiceSettings, load_settings
from agent_orchestration.app import db as db_module
from agent_orchestration.app import llm as llm_module
from agent_orchestration.app import main as main_module
from agent_orchestration.app.llm import LLMResult, generate_response


def test_load_settings_prefers_orchestration_database_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """ORCH_DATABASE_URL이 설정되면 DATABASE_URL보다 우선 사용한다."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch:pw@localhost:5432/orch_db")
    monkeypatch.setenv("DATABASE_URL", "postgresql://fallback:pw@localhost:5432/fallback")

    settings = load_settings()

    assert settings.openai_model == "gpt-5.3-codex-spark"
    assert settings.database_url == "postgresql://orch:pw@localhost:5432/orch_db"
    assert settings.interactions_table == "chat_interactions"


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


def test_generate_response_uses_read_only_ephemeral_codex_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex 공용 계정 호출은 저장소와 분리된 읽기 전용 일회성 프로세스다."""
    command: tuple[str, ...] = ()
    stdin_payload: bytes | None = None

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
        return FakeProcess()

    settings = ServiceSettings(
        openai_api_key=None,
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
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


def test_db_validate_table_name() -> None:
    """잘못된 테이블 이름은 저장 시점에서 거부한다."""
    with pytest.raises(ValueError, match="Invalid table name"):
        db_module._quote_identifier("chat-interactions")


def test_ensure_schema_executes_ddl_with_expected_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """스키마 보장 호출은 안전한 쿼리를 실행한다."""
    executed: list[str] = []

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

    def connect_factory(_db_url: str) -> FakeConnection:
        return FakeConnection()

    monkeypatch.setattr(db_module, "connect", connect_factory)
    db_module.ensure_schema("postgresql://example", "chat_interactions")

    assert any("CREATE TABLE IF NOT EXISTS chat_interactions" in query for query in executed)


def test_main_healthcheck_unavailable_without_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """초기화 실패 시 healthcheck는 503을 반환한다."""
    monkeypatch.setattr(
        main_module,
        "load_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("bootstrap failed")),
    )

    app = main_module.create_app()
    with TestClient(app) as client:
        response = client.get("/healthcheck")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_main_chat_succeeds_after_mocked_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """정상 경로에서 201과 저장된 레코드가 그대로 반환된다."""

    settings = ServiceSettings(
        openai_api_key="test-key",
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
    )

    class FakeResponses:
        async def create(self, **kwargs):
            return SimpleNamespace(
                output_text="안녕하세요",
                usage=SimpleNamespace(total_tokens=7),
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(main_module, "ensure_schema", lambda database_url, table_name: None)
    monkeypatch.setattr(main_module, "AsyncOpenAI", FakeOpenAI)
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
        response = client.post("/chat", json={"prompt": "테스트"})

    assert response.status_code == status.HTTP_201_CREATED
    body = response.json()
    assert body["id"] == 12
    assert body["response"] == "안녕하세요"
    assert body["token_count"] == 7


def test_main_chat_uses_responses_api_for_codex_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex 모델 요청은 Responses API의 입력·출력 계약으로 처리한다."""
    settings = ServiceSettings(
        openai_api_key="test-key",
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
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

    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(main_module, "ensure_schema", lambda database_url, table_name: None)
    monkeypatch.setattr(main_module, "AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr(
        main_module,
        "save_interaction",
        lambda **kwargs: SimpleNamespace(
            id=13,
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
        response = client.post("/chat", json={"prompt": "Responses API로 호출"})

    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["response"] == "Responses API 응답"
    assert received_request == {
        "model": "gpt-5.3-codex-spark",
        "input": "Responses API로 호출",
        "max_output_tokens": 1024,
    }


def test_main_chat_returns_bad_gateway_when_openai_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAI 호출 실패는 502로 변환한다."""
    settings = ServiceSettings(
        openai_api_key="test-key",
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
    )

    class FakeResponses:
        async def create(self, **kwargs):
            raise OpenAIError("boom")

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(main_module, "ensure_schema", lambda database_url, table_name: None)
    monkeypatch.setattr(main_module, "AsyncOpenAI", FakeOpenAI)
    monkeypatch.setattr(main_module, "save_interaction", lambda **kwargs: SimpleNamespace())

    app = main_module.create_app()
    with TestClient(app) as client:
        response = client.post("/chat", json={"prompt": "테스트"})

    assert response.status_code == status.HTTP_502_BAD_GATEWAY
