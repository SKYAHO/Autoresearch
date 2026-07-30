"""LLM 채팅 요청 수신·영속화를 제공하는 FastAPI 앱.

[파이프라인]
오케스트레이션 실험 구간에서 사용자가 입력한 프롬프트를 LLM으로 추론하고,
결과를 PostgreSQL에 보존해 다음 단계의 실험 분석 단계로 넘긴다.

[기능]
환경 설정과 DB 스키마를 준비하고 `/healthcheck`와 `/chat` 엔드포인트를 노출한다.
`/chat`은 선택된 LLM 백엔드의 응답 및 지연 지표, 토큰 사용량을 영속화 후 반환한다.

[비책임]
사용자 인증·세션 관리, OAuth 라우팅, 정책 라우팅·멀티턴 대화 상태.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter

from fastapi import FastAPI, HTTPException, status
from agent_orchestration.app.config import ServiceSettings, load_settings
from agent_orchestration.app.db import ensure_schema, save_interaction
from agent_orchestration.app.llm import LLMBackendError, generate_response
from agent_orchestration.app.schemas import ChatRequest, ChatResponse

logger = logging.getLogger(__name__)

def create_app() -> FastAPI:
    """FastAPI 앱과 의존성(설정, LLM 백엔드, DB)을 구성."""
    settings: ServiceSettings | None = None
    initialization_error: str | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal settings, initialization_error
        logger.info("agent_orchestration startup")
        try:
            settings = load_settings()
            ensure_schema(settings.database_url, settings.interactions_table)
            initialization_error = None
            logger.info(
                "agent_orchestration initialized with backend=%s table=%s",
                settings.llm_backend,
                settings.interactions_table,
            )
        except Exception as error:
            logger.exception("startup initialization failed")
            initialization_error = str(error)
            settings = None
        yield

    app = FastAPI(
        title="Autoresearch Agent Orchestration API",
        version="0.1.0",
        lifespan=lifespan,
    )

    def _require_runtime() -> ServiceSettings:
        if settings is None:
            if initialization_error:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Service is unavailable. Initialization failed.",
                )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service is unavailable.",
            )
        return settings

    @app.get("/healthcheck")
    def healthcheck() -> dict[str, str]:
        if settings is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service is unavailable.",
            )
        return {"status": "ok", "service": "agent-orchestration"}

    @app.post("/chat", response_model=ChatResponse, status_code=status.HTTP_201_CREATED)
    async def chat(request: ChatRequest) -> ChatResponse:
        """채팅 프롬프트를 LLM으로 전송 후 PostgreSQL에 저장하고 결과를 반환."""
        runtime_settings = _require_runtime()
        start = perf_counter()
        try:
            completion = await generate_response(runtime_settings, request.prompt)
        except LLMBackendError as error:
            logger.error("LLM backend call failed: %s", error)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to call LLM backend.",
            ) from error

        latency_ms = int((perf_counter() - start) * 1000)

        try:
            row = save_interaction(
                database_url=runtime_settings.database_url,
                table_name=runtime_settings.interactions_table,
                prompt=request.prompt,
                response=completion.text,
                model=completion.model,
                latency_ms=latency_ms,
                token_count=completion.token_count,
            )
        except Exception as error:
            logger.error("Persist interaction failed: %s", error)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to save chat interaction.",
            ) from error

        return ChatResponse(
            id=row.id,
            prompt=row.prompt,
            response=row.response,
            model=row.model,
            latency_ms=row.latency_ms,
            token_count=row.token_count,
            created_at=row.created_at,
        )

    return app


app = create_app()
