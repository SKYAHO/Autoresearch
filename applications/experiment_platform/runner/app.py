"""비공개 Codex Runner의 HTTP 애플리케이션.

[파이프라인]
오케스트레이션 API가 비공개 네트워크를 통해 위임한 프롬프트를 Codex CLI로
실행하고, 결과를 API 서버로 반환하는 구간이다.

[기능]
엄격한 생성 요청·응답 스키마, API 전용 내부 요청 토큰 검증, 비대기 용량 토큰으로
즉시 거절하는 동시 실행 상한을 제공한다. lifespan startup에서 설정을 검증하고, 연결
종료 시 공용 Codex 실행 task를 취소·회수하며 결과에 Runner 처리 시간을 추가한다.

[비책임]
외부 호출자 인증·DB 저장·OpenAI API 선택(applications.experiment_platform.api), OAuth
자격 증명 값 관리 및 Kubernetes Service 네트워크 정책.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter
from typing import TypeVar

from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict
from starlette.responses import Response

from applications.experiment_platform.shared.codex import generate_codex_response
from applications.experiment_platform.runner.config import RunnerSettings, load_runner_settings


TaskResult = TypeVar("TaskResult")
_REQUEST_DISCONNECT_POLL_INTERVAL_SEC = 0.1


class GenerateRequest(BaseModel):
    """Runner가 허용하는 최소 생성 요청."""

    model_config = ConfigDict(extra="forbid")

    prompt: str


class GenerateResponse(BaseModel):
    """Runner가 API 서버에 반환하는 생성 결과."""

    model_config = ConfigDict(extra="forbid")

    response: str
    model: str
    latency_ms: int
    token_count: int | None


def _runner_tokens_match(provided_token: str, expected_token: str) -> bool:
    """Runner 내부 토큰을 Unicode 입력에도 안전하게 비교한다."""
    return secrets.compare_digest(
        provided_token.encode("utf-8"),
        expected_token.encode("utf-8"),
    )


def _create_execution_slots(max_concurrency: int) -> asyncio.Queue[object]:
    """대기열 없이 즉시 획득할 수 있는 Runner 실행 용량 토큰을 만든다."""
    slots: asyncio.Queue[object] = asyncio.Queue(maxsize=max_concurrency)
    for _ in range(max_concurrency):
        slots.put_nowait(object())
    return slots


async def _await_request_task(
    http_request: Request,
    task: asyncio.Task[TaskResult],
) -> TaskResult | None:
    """연결 종료 시 작업을 취소·회수하고, 처리된 disconnect에는 None을 반환한다."""
    try:
        while not task.done():
            done, _ = await asyncio.wait(
                {task},
                timeout=_REQUEST_DISCONNECT_POLL_INTERVAL_SEC,
            )
            if task in done:
                break
            if await http_request.is_disconnected():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                return None
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def create_runner_app(settings: RunnerSettings | None = None) -> FastAPI:
    """Runner FastAPI 앱을 생성한다.

    설정을 넘기지 않으면 lifespan startup에서 Runner 전용 환경을 검증한다. 설정이
    잘못되면 Ready 상태가 되지 않으며, 입력 스키마 오류는 여전히 422를 반환한다.
    """

    def runtime() -> tuple[RunnerSettings, asyncio.Queue[object]]:
        runtime_settings = app.state.settings
        execution_slots = app.state.execution_slots
        if runtime_settings is None or execution_slots is None:
            runtime_settings = load_runner_settings()
            execution_slots = _create_execution_slots(runtime_settings.max_concurrency)
            app.state.settings = runtime_settings
            app.state.execution_slots = execution_slots
        return runtime_settings, execution_slots

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        runtime()
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.execution_slots = (
        _create_execution_slots(settings.max_concurrency) if settings is not None else None
    )

    @app.get("/healthcheck")
    async def healthcheck() -> dict[str, str]:
        """배포 probe에서 Runner 설정과 동시성 제어 준비 상태를 검증한다."""
        runtime()
        return {"status": "ok"}

    @app.post("/v1/generate", response_model=GenerateResponse)
    async def generate(
        http_request: Request,
        request: GenerateRequest,
        x_runner_token: str | None = Header(default=None, alias="X-Runner-Token"),
    ) -> GenerateResponse | Response:
        """하나의 프롬프트를 동시성 상한 안에서 Codex CLI에 위임한다."""
        runtime_settings, execution_slots = runtime()
        if not x_runner_token or not _runner_tokens_match(
            x_runner_token, runtime_settings.api_token
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid runner API token.",
            )
        try:
            slot = execution_slots.get_nowait()
        except asyncio.QueueEmpty:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Runner is temporarily overloaded.",
            ) from None
        started_at = perf_counter()
        try:
            codex_task = asyncio.create_task(
                generate_codex_response(runtime_settings.codex, request.prompt)
            )
            result = await _await_request_task(http_request, codex_task)
        finally:
            execution_slots.put_nowait(slot)
        if result is None:
            return Response(status_code=499)
        return GenerateResponse(
            response=result.text,
            model=result.model,
            latency_ms=int((perf_counter() - started_at) * 1000),
            token_count=result.token_count,
        )

    return app


app = create_runner_app()
