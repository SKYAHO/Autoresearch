"""Agent Orchestration API와 비공개 Codex Runner의 경계 계약 테스트."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

from agent_orchestration.app.config import ServiceSettings
from agent_orchestration.app.llm import generate_response
from agent_orchestration import codex as codex_module
from agent_orchestration.contracts import LLMResult
from agent_orchestration.runner import app as runner_app_module
from agent_orchestration.runner.config import RunnerSettings


def make_settings(**overrides: Any) -> ServiceSettings:
    """Runner API 경로 테스트에 필요한 서비스 설정을 만든다."""
    values: dict[str, Any] = {
        "openai_api_key": None,
        "openai_model": "gpt-5.3-codex-spark",
        "openai_max_tokens": 1024,
        "openai_timeout_sec": 60,
        "database_url": "postgresql://orch@localhost:5432/orch",
        "interactions_table": "chat_interactions",
        "api_token": "test-api-token-must-be-at-least-32-characters",
        "llm_backend": "codex_runner",
        "codex_runner_url": "http://runner:8080",
        "codex_runner_timeout_sec": 30,
    }
    values.update(overrides)
    return ServiceSettings(**values)


def test_generate_response_uses_private_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """API의 Runner 백엔드는 비공개 Runner 응답을 공통 결과 계약으로 반환한다."""
    called: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "response": "runner answer",
                "model": "codex-cli",
                "latency_ms": 5,
                "token_count": None,
            }

    async def fake_post(
        self: httpx.AsyncClient,
        url: str,
        **kwargs: object,
    ) -> FakeResponse:
        called["url"] = url
        called["json"] = kwargs["json"]
        return FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    result = asyncio.run(generate_response(make_settings(), "hello"))

    assert result == LLMResult(text="runner answer", model="codex-cli", token_count=None)
    assert called == {"url": "http://runner:8080/v1/generate", "json": {"prompt": "hello"}}


def test_runner_rejects_unknown_request_fields() -> None:
    """Runner는 모델 주입 등 계약 밖 필드를 요청 단계에서 거부한다."""
    response = TestClient(runner_app_module.create_runner_app()).post(
        "/v1/generate",
        json={"prompt": "x", "model": "x"},
    )

    assert response.status_code == 422


def test_runner_returns_codex_result_with_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runner는 공용 Codex 결과를 엄격한 HTTP 응답 계약으로 변환한다."""
    codex_settings = codex_module.CodexSettings(
        cli_path="codex",
        home="/tmp/codex-home",
        model=None,
        timeout_sec=120,
    )

    async def fake_generate(
        settings: codex_module.CodexSettings,
        prompt: str,
    ) -> LLMResult:
        assert settings == codex_settings
        assert prompt == "runner prompt"
        return LLMResult(text="runner answer", model="codex-cli", token_count=None)

    monkeypatch.setattr(runner_app_module, "generate_codex_response", fake_generate)

    response = TestClient(
        runner_app_module.create_runner_app(
            RunnerSettings(codex=codex_settings, max_concurrency=1)
        )
    ).post("/v1/generate", json={"prompt": "runner prompt"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["response"] == "runner answer"
    assert payload["model"] == "codex-cli"
    assert payload["token_count"] is None
    assert isinstance(payload["latency_ms"], int)
    assert payload["latency_ms"] >= 0
