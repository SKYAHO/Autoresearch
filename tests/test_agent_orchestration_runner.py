"""Agent Orchestration API와 비공개 Codex Runner의 경계 계약 테스트."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.testclient import TestClient
import httpx
import pytest

from applications.experiment_platform.api.config import ServiceSettings
from applications.experiment_platform.api.llm import LLMBackendError, generate_response
from applications.experiment_platform.api.schemas import ChatRequest
from applications.experiment_platform.shared import codex as codex_module
from applications.experiment_platform.shared.contracts import LLMBackendOverloadedError, LLMResult
from applications.experiment_platform.runner import app as runner_app_module
from applications.experiment_platform.runner import config as runner_config_module
from applications.experiment_platform.runner.config import RunnerSettings


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
        "github_token": "x" * 40,
        "github_repository": "SKYAHO/Autoresearch",
        "gh_timeout_sec": 30,
        "issue_daily_limit": 20,
    }
    values.update(overrides)
    return ServiceSettings(**values)


def _disconnecting_http_request() -> Request:
    """실제 ASGI 연결 종료 메시지를 반환하는 HTTP 요청을 만든다."""

    async def receive() -> dict[str, str]:
        return {"type": "http.disconnect"}

    return Request(
        {"type": "http", "method": "POST", "path": "/", "headers": []},
        receive,
    )


def _connected_http_request() -> Request:
    """연결이 유지되는 동안 완료되는 직접 endpoint 테스트용 요청을 만든다."""

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {"type": "http", "method": "POST", "path": "/", "headers": []},
        receive,
    )


async def _request_then_disconnect(
    app: Any,
    *,
    path: str,
    headers: list[tuple[bytes, bytes]],
    payload: dict[str, str],
    started: asyncio.Event,
) -> list[dict[str, Any]]:
    """실제 ASGI 앱에 request body 뒤 disconnect message를 전달한다."""
    request_body = json.dumps(payload).encode("utf-8")
    receive_calls = 0
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal receive_calls
        if receive_calls == 0:
            receive_calls += 1
            return {
                "type": "http.request",
                "body": request_body,
                "more_body": False,
            }
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "state": {},
        },
        receive,
        send,
    )
    return sent


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


def test_runner_startup_fails_when_runtime_settings_are_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """잘못된 Runner 설정은 요청 전 TestClient lifespan 기동을 실패시킨다."""
    monkeypatch.setattr(
        runner_app_module,
        "load_runner_settings",
        lambda: (_ for _ in ()).throw(ValueError("invalid runner settings")),
    )

    with pytest.raises(ValueError, match="invalid runner settings"):
        with TestClient(runner_app_module.create_runner_app()):
            pass


def test_load_runner_settings_rejects_timeout_without_cleanup_headroom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex 종료 여유 5초가 없는 Runner HTTP timeout 조합은 기동 전에 거부한다."""
    monkeypatch.setenv("ORCH_RUNNER_TOKEN", "runner-token-must-be-at-least-32-characters")
    monkeypatch.setenv("CODEX_HOME", "/tmp/codex-home")
    monkeypatch.setenv("CODEX_TIMEOUT_SEC", "115")
    monkeypatch.setenv("CODEX_RUNNER_TIMEOUT_SEC", "120")

    with pytest.raises(
        ValueError,
        match=r"CODEX_TIMEOUT_SEC \+ 5 must be less than CODEX_RUNNER_TIMEOUT_SEC",
    ):
        runner_config_module.load_runner_settings()


def test_load_runner_settings_requires_shared_runner_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner는 ConfigMap의 공통 HTTP timeout이 없으면 fail-close한다."""
    monkeypatch.setenv("ORCH_RUNNER_TOKEN", "runner-token-must-be-at-least-32-characters")
    monkeypatch.setenv("CODEX_HOME", "/tmp/codex-home")
    monkeypatch.delenv("CODEX_RUNNER_TIMEOUT_SEC", raising=False)

    with pytest.raises(
        ValueError,
        match="Required environment variable 'CODEX_RUNNER_TIMEOUT_SEC' is not set",
    ):
        runner_config_module.load_runner_settings()


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
                http_request=_connected_http_request(),
                request=runner_app_module.GenerateRequest(prompt="first"),
                x_runner_token="runner-token-must-be-at-least-32-characters",
            )
        )
        second = asyncio.create_task(
            generate(
                http_request=_connected_http_request(),
                request=runner_app_module.GenerateRequest(prompt="second"),
                x_runner_token="runner-token-must-be-at-least-32-characters",
            )
        )
        await all_slots_busy.wait()
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(
                generate(
                    http_request=_connected_http_request(),
                    request=runner_app_module.GenerateRequest(prompt="third"),
                    x_runner_token="runner-token-must-be-at-least-32-characters",
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
                http_request=_connected_http_request(),
                request=runner_app_module.GenerateRequest(prompt="first"),
                x_runner_token="runner-token-must-be-at-least-32-characters",
            )
        response = await generate(
            http_request=_connected_http_request(),
            request=runner_app_module.GenerateRequest(prompt="second"),
            x_runner_token="runner-token-must-be-at-least-32-characters",
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
                http_request=_connected_http_request(),
                request=runner_app_module.GenerateRequest(prompt="first"),
                x_runner_token="runner-token-must-be-at-least-32-characters",
            )
        )
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        response = await generate(
            http_request=_connected_http_request(),
            request=runner_app_module.GenerateRequest(prompt="second"),
            x_runner_token="runner-token-must-be-at-least-32-characters",
        )
        assert response.response == "runner answer"

    asyncio.run(exercise())


def test_runner_returns_499_after_http_disconnect_and_recovers_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASGI 연결 종료는 진행 중인 Codex task를 취소하고 실행 슬롯을 회수한다."""
    settings = RunnerSettings(
        codex=codex_module.CodexSettings(
            cli_path="codex",
            home="/tmp/codex-home",
            model=None,
            timeout_sec=110,
        ),
        max_concurrency=1,
        api_token="runner-token-must-be-at-least-32-characters",
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()
    attempts = 0

    async def blocking_generate(
        _settings: codex_module.CodexSettings,
        _prompt: str,
    ) -> LLMResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return LLMResult(text="runner answer", model="codex-cli", token_count=None)

    monkeypatch.setattr(runner_app_module, "generate_codex_response", blocking_generate)
    app = runner_app_module.create_runner_app(settings)
    generate = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/v1/generate"
    )

    async def exercise() -> None:
        disconnected = asyncio.create_task(
            generate(
                request=runner_app_module.GenerateRequest(prompt="first"),
                x_runner_token="runner-token-must-be-at-least-32-characters",
                http_request=_disconnecting_http_request(),
            )
        )
        await started.wait()
        response = await disconnected
        assert response.status_code == 499
        await cancelled.wait()

        response = await generate(
            request=runner_app_module.GenerateRequest(prompt="second"),
            x_runner_token="runner-token-must-be-at-least-32-characters",
            http_request=_disconnecting_http_request(),
        )

        assert response.response == "runner answer"
        assert app.state.execution_slots.qsize() == 1

    asyncio.run(exercise())


def test_runner_asgi_request_body_disconnect_cancels_codex_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASGI body parsing 뒤 disconnect도 499·Codex 취소·slot 회수로 처리한다."""
    settings = RunnerSettings(
        codex=codex_module.CodexSettings(
            cli_path="codex",
            home="/tmp/codex-home",
            model=None,
            timeout_sec=110,
        ),
        max_concurrency=1,
        api_token="runner-token-must-be-at-least-32-characters",
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking_generate(
        _settings: codex_module.CodexSettings,
        _prompt: str,
    ) -> LLMResult:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(runner_app_module, "generate_codex_response", blocking_generate)
    app = runner_app_module.create_runner_app(settings)

    async def exercise() -> None:
        sent = await _request_then_disconnect(
            app,
            path="/v1/generate",
            headers=[
                (b"content-type", b"application/json"),
                (b"x-runner-token", settings.api_token.encode("ascii")),
            ],
            payload={"prompt": "runner prompt"},
            started=started,
        )

        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 499
        await cancelled.wait()
        assert app.state.execution_slots.qsize() == 1

    asyncio.run(exercise())


def test_api_returns_499_after_http_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """외부 ASGI 연결 종료는 API의 진행 중인 Runner HTTP 요청을 취소한다."""
    settings = make_settings(
        codex_runner_token="runner-token-must-be-at-least-32-characters"
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking_generate_response(
        _settings: ServiceSettings,
        _prompt: str,
    ) -> LLMResult:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    from applications.experiment_platform.api import main as main_module

    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(main_module, "ensure_schema", lambda *_args: None)
    monkeypatch.setattr(main_module, "generate_response", blocking_generate_response)
    app = main_module.create_app()
    chat = next(
        route.endpoint for route in app.routes if getattr(route, "path", None) == "/chat"
    )

    async def exercise() -> None:
        disconnected = asyncio.create_task(
            chat(
                request=ChatRequest(prompt="runner prompt"),
                x_orch_token=settings.api_token,
                http_request=_disconnecting_http_request(),
            )
        )
        await started.wait()
        response = await disconnected
        assert response.status_code == 499
        await cancelled.wait()

    with TestClient(app):
        asyncio.run(exercise())


def test_api_asgi_disconnect_cancels_runner_http_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASGI body parsing 뒤 API 연결 종료는 실제 Runner HTTPX task를 취소한다."""
    settings = make_settings(
        codex_runner_token="runner-token-must-be-at-least-32-characters"
    )
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking_post(
        _self: httpx.AsyncClient,
        _url: str,
        **_kwargs: object,
    ) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    from applications.experiment_platform.api import main as main_module

    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(main_module, "ensure_schema", lambda *_args: None)
    monkeypatch.setattr(httpx.AsyncClient, "post", blocking_post)
    app = main_module.create_app()

    async def exercise() -> None:
        async with app.router.lifespan_context(app):
            sent = await _request_then_disconnect(
                app,
                path="/chat",
                headers=[
                    (b"content-type", b"application/json"),
                    (b"x-orch-token", settings.api_token.encode("ascii")),
                ],
                payload={"prompt": "runner prompt"},
                started=started,
            )

        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 499
        await cancelled.wait()

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
