"""Agent Orchestration API와 비공개 Codex Runner의 경계 계약 테스트."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import HTTPException, status
from fastapi.testclient import TestClient
import httpx
import pytest

from agent_orchestration.app.config import ServiceSettings
from agent_orchestration.app.llm import LLMBackendError, generate_response
from agent_orchestration import codex as codex_module
from agent_orchestration.contracts import LLMBackendOverloadedError, LLMResult
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
        status_code = 200

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
        called["headers"] = kwargs["headers"]
        return FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    settings = make_settings(
        codex_runner_token="runner-token-must-be-at-least-32-characters"
    )

    result = asyncio.run(generate_response(settings, "hello"))

    assert result == LLMResult(text="runner answer", model="codex-cli", token_count=None)
    assert called == {
        "url": "http://runner:8080/v1/generate",
        "json": {"prompt": "hello"},
        "headers": {"X-Runner-Token": "runner-token-must-be-at-least-32-characters"},
    }


@pytest.mark.parametrize(
    "failure_kind",
    ("timeout", "http_status", "malformed_json", "missing_field", "invalid_field_type"),
)
def test_generate_response_hides_private_runner_failure_details(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    """Runner 호출의 전송·HTTP·응답 오류는 같은 안전한 백엔드 오류로 정규화한다."""
    class FakeResponse:
        status_code = 500 if failure_kind == "http_status" else 200

        def raise_for_status(self) -> None:
            if failure_kind == "http_status":
                request = httpx.Request("POST", "http://runner:8080/v1/generate")
                raise httpx.HTTPStatusError(
                    "private runner returned diagnostic detail",
                    request=request,
                    response=httpx.Response(500, request=request),
                )

        def json(self) -> object:
            if failure_kind == "malformed_json":
                raise json.JSONDecodeError("invalid private runner response", "{", 1)
            if failure_kind == "missing_field":
                return {"model": "codex-cli", "token_count": None}
            if failure_kind == "invalid_field_type":
                return {"response": 1, "model": "codex-cli", "token_count": None}
            return {"response": "unused", "model": "codex-cli", "token_count": None}

    async def fake_post(
        self: httpx.AsyncClient,
        url: str,
        **kwargs: object,
    ) -> FakeResponse:
        del self, url, kwargs
        if failure_kind == "timeout":
            raise httpx.TimeoutException("private runner timeout detail")
        return FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with pytest.raises(LLMBackendError) as error:
        asyncio.run(
            generate_response(
                make_settings(
                    codex_runner_token="runner-token-must-be-at-least-32-characters"
                ),
                "hello",
            )
        )

    assert str(error.value) == "Codex runner call failed."
    assert error.value.__cause__ is None


def test_runner_rejects_unknown_request_fields() -> None:
    """Runner는 모델 주입 등 계약 밖 필드를 요청 단계에서 거부한다."""
    response = TestClient(runner_app_module.create_runner_app()).post(
        "/v1/generate",
        json={"prompt": "x", "model": "x"},
    )

    assert response.status_code == 422


def test_runner_rejects_request_without_private_api_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner는 NetworkPolicy 외에도 API 전용 토큰을 요구한다."""
    codex_settings = codex_module.CodexSettings(
        cli_path="codex",
        home="/tmp/codex-home",
        model=None,
        timeout_sec=110,
    )
    settings = RunnerSettings(
        codex=codex_settings,
        max_concurrency=1,
        api_token="runner-token-must-be-at-least-32-characters",
    )

    async def fake_generate(
        _settings: codex_module.CodexSettings,
        _prompt: str,
    ) -> LLMResult:
        return LLMResult(text="unused", model="codex-cli", token_count=None)

    monkeypatch.setattr(runner_app_module, "generate_codex_response", fake_generate)

    response = TestClient(runner_app_module.create_runner_app(settings)).post(
        "/v1/generate",
        json={"prompt": "runner prompt"},
    )

    assert response.status_code == 401


def test_runner_rejects_request_when_concurrency_limit_is_reached() -> None:
    """Runner는 실행 슬롯이 모두 사용 중이면 요청을 대기시키지 않고 503을 반환한다."""
    codex_settings = codex_module.CodexSettings(
        cli_path="codex",
        home="/tmp/codex-home",
        model=None,
        timeout_sec=110,
    )
    settings = RunnerSettings(
        codex=codex_settings,
        max_concurrency=1,
        api_token="runner-token-must-be-at-least-32-characters",
    )

    class FullExecutionSlots:
        def get_nowait(self) -> object:
            raise asyncio.QueueEmpty

    app = runner_app_module.create_runner_app(settings)
    app.state.execution_slots = FullExecutionSlots()

    response = TestClient(app).post(
        "/v1/generate",
        headers={"X-Runner-Token": "runner-token-must-be-at-least-32-characters"},
        json={"prompt": "runner prompt"},
    )

    assert response.status_code == 503


def test_runner_returns_immediate_503_when_all_real_slots_are_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """실행 중인 요청이 상한을 채우면 다음 요청은 대기하지 않고 즉시 거절한다."""
    codex_settings = codex_module.CodexSettings(
        cli_path="codex",
        home="/tmp/codex-home",
        model=None,
        timeout_sec=110,
    )
    settings = RunnerSettings(
        codex=codex_settings,
        max_concurrency=2,
        api_token="runner-token-must-be-at-least-32-characters",
    )
    started_count = 0
    all_slots_busy = asyncio.Event()
    release = asyncio.Event()

    async def blocking_generate(
        _settings: codex_module.CodexSettings,
        _prompt: str,
    ) -> LLMResult:
        nonlocal started_count
        started_count += 1
        if started_count == settings.max_concurrency:
            all_slots_busy.set()
        await release.wait()
        return LLMResult(text="runner answer", model="codex-cli", token_count=None)

    monkeypatch.setattr(runner_app_module, "generate_codex_response", blocking_generate)
    app = runner_app_module.create_runner_app(settings)
    generate = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/v1/generate"
    )

    async def exercise() -> None:
        first = asyncio.create_task(
            generate(
                runner_app_module.GenerateRequest(prompt="first"),
                "runner-token-must-be-at-least-32-characters",
            )
        )
        second = asyncio.create_task(
            generate(
                runner_app_module.GenerateRequest(prompt="second"),
                "runner-token-must-be-at-least-32-characters",
            )
        )
        await all_slots_busy.wait()
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(
                generate(
                    runner_app_module.GenerateRequest(prompt="third"),
                    "runner-token-must-be-at-least-32-characters",
                ),
                timeout=0.1,
            )
        assert error.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        release.set()
        await asyncio.gather(first, second)

    asyncio.run(exercise())


def test_runner_returns_slot_after_codex_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex 오류 뒤에도 반환된 용량 토큰으로 다음 요청을 수용한다."""
    codex_settings = codex_module.CodexSettings(
        cli_path="codex",
        home="/tmp/codex-home",
        model=None,
        timeout_sec=110,
    )
    settings = RunnerSettings(
        codex=codex_settings,
        max_concurrency=1,
        api_token="runner-token-must-be-at-least-32-characters",
    )
    attempts = 0

    async def fail_once(
        _settings: codex_module.CodexSettings,
        _prompt: str,
    ) -> LLMResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise LLMBackendError("Codex CLI failed.")
        return LLMResult(text="runner answer", model="codex-cli", token_count=None)

    monkeypatch.setattr(runner_app_module, "generate_codex_response", fail_once)
    app = runner_app_module.create_runner_app(settings)
    generate = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/v1/generate"
    )

    async def exercise() -> None:
        with pytest.raises(LLMBackendError, match="Codex CLI failed"):
            await generate(
                runner_app_module.GenerateRequest(prompt="first"),
                "runner-token-must-be-at-least-32-characters",
            )
        response = await generate(
            runner_app_module.GenerateRequest(prompt="second"),
            "runner-token-must-be-at-least-32-characters",
        )
        assert response.response == "runner answer"

    asyncio.run(exercise())


def test_runner_returns_slot_after_request_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """취소된 Codex 요청도 용량 토큰을 반환해 다음 요청을 수용한다."""
    codex_settings = codex_module.CodexSettings(
        cli_path="codex",
        home="/tmp/codex-home",
        model=None,
        timeout_sec=110,
    )
    settings = RunnerSettings(
        codex=codex_settings,
        max_concurrency=1,
        api_token="runner-token-must-be-at-least-32-characters",
    )
    started = asyncio.Event()
    attempts = 0

    async def block_once(
        _settings: codex_module.CodexSettings,
        _prompt: str,
    ) -> LLMResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            started.set()
            await asyncio.Event().wait()
        return LLMResult(text="runner answer", model="codex-cli", token_count=None)

    monkeypatch.setattr(runner_app_module, "generate_codex_response", block_once)
    app = runner_app_module.create_runner_app(settings)
    generate = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/v1/generate"
    )

    async def exercise() -> None:
        first = asyncio.create_task(
            generate(
                runner_app_module.GenerateRequest(prompt="first"),
                "runner-token-must-be-at-least-32-characters",
            )
        )
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        response = await generate(
            runner_app_module.GenerateRequest(prompt="second"),
            "runner-token-must-be-at-least-32-characters",
        )
        assert response.response == "runner answer"

    asyncio.run(exercise())


def test_generate_response_preserves_runner_overload_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner의 503은 일반 gateway 오류가 아닌 과부하 오류로 구분한다."""
    class FakeResponse:
        status_code = 503

        def raise_for_status(self) -> None:
            raise AssertionError("503 must be normalized before raise_for_status")

    async def fake_post(
        _self: httpx.AsyncClient,
        _url: str,
        **_kwargs: object,
    ) -> FakeResponse:
        return FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    with pytest.raises(LLMBackendOverloadedError, match="overloaded"):
        asyncio.run(
            generate_response(
                make_settings(
                    codex_runner_token="runner-token-must-be-at-least-32-characters"
                ),
                "hello",
            )
        )


def test_runner_healthcheck_loads_runtime_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner 헬스체크는 첫 생성 요청 전에 런타임 설정을 검증한다."""
    codex_settings = codex_module.CodexSettings(
        cli_path="codex",
        home="/tmp/codex-home",
        model=None,
        timeout_sec=120,
    )
    expected_settings = RunnerSettings(
        codex=codex_settings,
        max_concurrency=1,
        api_token="runner-token-must-be-at-least-32-characters",
    )
    monkeypatch.setattr(
        runner_app_module,
        "load_runner_settings",
        lambda: expected_settings,
    )

    response = TestClient(runner_app_module.create_runner_app()).get("/healthcheck")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
            RunnerSettings(
                codex=codex_settings,
                max_concurrency=1,
                api_token="runner-token-must-be-at-least-32-characters",
            )
        )
    ).post(
        "/v1/generate",
        headers={"X-Runner-Token": "runner-token-must-be-at-least-32-characters"},
        json={"prompt": "runner prompt"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["response"] == "runner answer"
    assert payload["model"] == "codex-cli"
    assert payload["token_count"] is None
    assert isinstance(payload["latency_ms"], int)
    assert payload["latency_ms"] >= 0
