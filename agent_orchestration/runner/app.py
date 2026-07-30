"""비공개 Codex Runner의 HTTP 애플리케이션.

[파이프라인]
오케스트레이션 API가 비공개 네트워크를 통해 위임한 프롬프트를 Codex CLI로
실행하고, 결과를 API 서버로 반환하는 구간이다.

[기능]
엄격한 생성 요청·응답 스키마, API 전용 내부 요청 토큰 검증, 즉시 거절하는
동시 실행 상한을 제공하고 공용 Codex 실행 경계의 결과에 Runner 처리 시간을
추가한다.

[비책임]
외부 호출자 인증·DB 저장·OpenAI API 선택(agent_orchestration.app), OAuth
자격 증명 값 관리 및 Kubernetes Service 네트워크 정책.
"""

from __future__ import annotations

import asyncio
import secrets
from time import perf_counter

from fastapi import FastAPI, Header, HTTPException, status
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


def _runner_tokens_match(provided_token: str, expected_token: str) -> bool:
    """Runner 내부 토큰을 Unicode 입력에도 안전하게 비교한다."""
    return secrets.compare_digest(
        provided_token.encode("utf-8"),
        expected_token.encode("utf-8"),
    )


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
    async def healthcheck() -> dict[str, str]:
        """배포 probe에서 Runner 설정과 동시성 제어 준비 상태를 검증한다."""
        runtime()
        return {"status": "ok"}

    @app.post("/v1/generate", response_model=GenerateResponse)
    async def generate(
        request: GenerateRequest,
        x_runner_token: str | None = Header(default=None, alias="X-Runner-Token"),
    ) -> GenerateResponse:
        """하나의 프롬프트를 동시성 상한 안에서 Codex CLI에 위임한다."""
        runtime_settings, semaphore = runtime()
        if not x_runner_token or not _runner_tokens_match(
            x_runner_token, runtime_settings.api_token
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid runner API token.",
            )
        if semaphore.locked():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Runner is temporarily overloaded.",
            )
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
