"""비공개 Codex Runner의 HTTP 애플리케이션.

[파이프라인]
오케스트레이션 API가 비공개 네트워크를 통해 위임한 프롬프트를 Codex CLI로
실행하고, 결과를 API 서버로 반환하는 구간이다.

[기능]
엄격한 생성 요청·응답 스키마와 동시 실행 상한을 제공하고, 공용 Codex 실행
경계의 결과에 Runner 처리 시간을 추가한다.

[비책임]
외부 호출자 인증·DB 저장·OpenAI API 선택(agent_orchestration.app), OAuth
자격 증명 값 관리 및 Kubernetes Service 네트워크 정책.
"""

from __future__ import annotations

import asyncio
from time import perf_counter

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from agent_orchestration.codex import generate_codex_response
from agent_orchestration.runner.config import RunnerSettings, load_runner_settings


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


def create_runner_app(settings: RunnerSettings | None = None) -> FastAPI:
    """Runner FastAPI 앱을 생성한다.

    설정을 넘기지 않으면 유효한 요청 처리 시점에 Runner 전용 환경에서 로드한다.
    따라서 입력 스키마 오류는 Codex 자격 증명 설정과 독립적으로 422를 반환한다.
    """
    app = FastAPI()
    app.state.settings = settings
    app.state.semaphore = (
        asyncio.Semaphore(settings.max_concurrency) if settings is not None else None
    )

    def runtime() -> tuple[RunnerSettings, asyncio.Semaphore]:
        runtime_settings = app.state.settings
        semaphore = app.state.semaphore
        if runtime_settings is None:
            runtime_settings = load_runner_settings()
            semaphore = asyncio.Semaphore(runtime_settings.max_concurrency)
            app.state.settings = runtime_settings
            app.state.semaphore = semaphore
        return runtime_settings, semaphore

    @app.get("/healthcheck")
    def healthcheck() -> dict[str, str]:
        """배포 probe에서 Runner 설정과 동시성 제어 준비 상태를 검증한다."""
        runtime()
        return {"status": "ok"}

    @app.post("/v1/generate", response_model=GenerateResponse)
    async def generate(request: GenerateRequest) -> GenerateResponse:
        """하나의 프롬프트를 동시성 상한 안에서 Codex CLI에 위임한다."""
        runtime_settings, semaphore = runtime()
        started_at = perf_counter()
        async with semaphore:
            result = await generate_codex_response(runtime_settings.codex, request.prompt)
        return GenerateResponse(
            response=result.text,
            model=result.model,
            latency_ms=int((perf_counter() - started_at) * 1000),
            token_count=result.token_count,
        )

    return app


app = create_runner_app()
