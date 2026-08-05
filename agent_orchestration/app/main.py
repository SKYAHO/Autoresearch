"""LLM 채팅 요청 수신·영속화를 제공하는 FastAPI 앱.

[파이프라인]
오케스트레이션 실험 구간에서 사용자가 입력한 프롬프트를 LLM으로 추론하고,
결과를 PostgreSQL에 보존해 다음 단계의 실험 분석 단계로 넘긴다.

[기능]
환경 설정과 DB 스키마를 준비하고 `/healthcheck`, `/chat`, 실험 워크벤치 endpoint를
노출한다.
`/chat`은 외부 연결 종료 시 진행 중인 LLM task를 취소하고, 선택된 LLM 백엔드의 응답
및 지연 지표, 토큰 사용량을 영속화 후 반환한다.

[비책임]
OAuth 라우팅, 정책 라우팅·멀티턴 대화 상태와 실험 도메인 상태 전이 판단.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter
from typing import Annotated, TypeVar

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from starlette.responses import JSONResponse, Response

from agent_orchestration.app.config import ServiceSettings, load_settings
from agent_orchestration.app.database import (
    create_database_engine,
    create_session_factory,
)
from agent_orchestration.app.db import ensure_schema, save_interaction
from agent_orchestration.app.experiments.exceptions import (
    ExperimentNotFoundError,
    ExperimentStepNotFoundError,
    IdempotencyConflictError,
    InvalidCursorError,
    IssuePublicationLimitError,
    PromotionRequiresDedicatedEndpointError,
    StepAlreadyFinalizedError,
)
from agent_orchestration.app.experiments.github_issues import GitHubIssueError
from agent_orchestration.app.experiments.router import router as experiment_router
from agent_orchestration.app.experiments.transition_service import InvalidTransitionError
from agent_orchestration.app.llm import LLMBackendError, generate_response
from agent_orchestration.contracts import LLMBackendOverloadedError
from agent_orchestration.app.schemas import ChatRequest, ChatResponse, ErrorResponse

logger = logging.getLogger(__name__)


TaskResult = TypeVar("TaskResult")
_REQUEST_DISCONNECT_POLL_INTERVAL_SEC = 0.1


def _api_tokens_match(provided_token: str, expected_token: str) -> bool:
    """HTTP 헤더의 비 ASCII 값도 예외 없이 안전하게 비교한다."""
    return secrets.compare_digest(
        provided_token.encode("utf-8"),
        expected_token.encode("utf-8"),
    )


async def _await_request_task(
    http_request: Request,
    task: asyncio.Task[TaskResult],
) -> TaskResult | None:
    """연결 종료 시 백엔드 task를 취소·회수하고, 처리된 disconnect에는 None을 반환한다."""
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


def create_app() -> FastAPI:
    """FastAPI 앱과 의존성(설정, LLM 백엔드, DB)을 구성."""
    settings: ServiceSettings | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal settings
        logger.info("agent_orchestration startup")
        settings = load_settings()
        # 라우터는 `create_app()`의 클로저에 접근할 수 없으므로, 요청 단위로 설정을
        # 꺼내는 `config.get_settings` 의존성이 여기서 읽는다.
        app.state.settings = settings
        await asyncio.to_thread(
            ensure_schema,
            settings.database_url,
            settings.interactions_table,
            settings.database_connect_timeout_sec,
        )
        experiment_engine = create_database_engine(
            settings.database_url,
            settings.database_connect_timeout_sec,
        )
        app.state.experiment_session_factory = create_session_factory(experiment_engine)
        logger.info(
            "agent_orchestration initialized with backend=%s table=%s",
            settings.llm_backend,
            settings.interactions_table,
        )
        try:
            yield
        finally:
            experiment_engine.dispose()

    app = FastAPI(
        title="Autoresearch Agent Orchestration API",
        version="0.1.0",
        lifespan=lifespan,
    )

    def _require_runtime() -> ServiceSettings:
        if settings is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service is unavailable.",
            )
        return settings

    def _require_orchestration_token(
        x_orch_token: Annotated[
            str | None,
            Header(alias="X-Orch-Token", description="공유 오케스트레이션 API 토큰"),
        ] = None,
    ) -> None:
        """기존 `/chat`과 같은 공유 API 토큰을 실험 endpoint에 적용한다."""
        runtime_settings = _require_runtime()
        if not x_orch_token or not _api_tokens_match(x_orch_token, runtime_settings.api_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid orchestration API token.",
            )

    @app.exception_handler(ExperimentNotFoundError)
    @app.exception_handler(ExperimentStepNotFoundError)
    @app.exception_handler(InvalidCursorError)
    def handle_experiment_not_found(
        _request: Request,
        error: ExperimentNotFoundError | ExperimentStepNotFoundError | InvalidCursorError,
    ) -> JSONResponse:
        """도메인 not-found와 존재하지 않는 polling cursor를 공개 404 detail로 변환한다."""
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(error)})

    @app.exception_handler(InvalidTransitionError)
    @app.exception_handler(IdempotencyConflictError)
    @app.exception_handler(PromotionRequiresDedicatedEndpointError)
    @app.exception_handler(StepAlreadyFinalizedError)
    def handle_experiment_conflict(
        _request: Request,
        error: InvalidTransitionError
        | IdempotencyConflictError
        | PromotionRequiresDedicatedEndpointError
        | StepAlreadyFinalizedError,
    ) -> JSONResponse:
        """상태 전이·멱등성·승격 우회·Step 확정 충돌을 공개 409 detail로 변환한다."""
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(error)})

    @app.exception_handler(IssuePublicationLimitError)
    async def handle_publication_limit(
        _request: Request, error: IssuePublicationLimitError
    ) -> JSONResponse:
        """발행 상한 초과를 429로 변환한다."""
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": str(error)},
        )

    @app.exception_handler(GitHubIssueError)
    async def handle_github_issue_error(
        _request: Request, error: GitHubIssueError
    ) -> JSONResponse:
        """`gh` 실패를 502로 변환하되 사유만 노출한다."""
        logger.error("Issue publication failed: %s", error)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": f"Failed to publish issue: {error.reason}"},
        )

    @app.exception_handler(LLMBackendOverloadedError)
    async def handle_llm_backend_overloaded(
        _request: Request, error: LLMBackendOverloadedError
    ) -> JSONResponse:
        """LLM 백엔드 과부하를 503으로 변환한다.

        `/chat`은 함수 내부 try/except가 이 예외를 먼저 잡아 같은 503 응답으로
        바꾸므로 이 전역 handler에 도달하지 않는다 — 이슈 발행처럼 내부에 별도
        except가 없는 호출 경로를 위한 것이다.
        """
        logger.error("LLM backend is overloaded: %s", error)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "LLM backend is temporarily overloaded."},
        )

    @app.exception_handler(LLMBackendError)
    async def handle_llm_backend_error(
        _request: Request, error: LLMBackendError
    ) -> JSONResponse:
        """LLM 백엔드 호출 실패를 502로 변환한다.

        `LLMBackendOverloadedError`는 `LLMBackendError`의 하위 클래스이지만,
        Starlette가 예외의 MRO를 훑어 더 구체적으로 등록된 handler를 먼저 찾으므로
        위 handler와 충돌하지 않는다. `/chat`은 위 handler와 같은 이유로 영향받지
        않는다.
        """
        logger.error("LLM backend call failed: %s", error)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": "Failed to call LLM backend."},
        )

    @app.exception_handler(ValueError)
    async def handle_issue_body_assembly_error(
        _request: Request, error: ValueError
    ) -> JSONResponse:
        """이슈 본문 조립 단계의 실패(LLM 출력 드리프트 포함)를 502로 변환한다.

        `parse_llm_fields`/`build_issue_body`/`_branch_slug`가 내는 `ValueError`가
        여기로 온다 — LLM이 계약과 다른 값을 냈다는 뜻이며, 서버 결함(500)과 구분해야
        호출자가 "재생성해야 한다"를 알 수 있다.

        `IdempotencyConflictError`/`PromotionRequiresDedicatedEndpointError`도
        `ValueError`의 하위 클래스이지만, 위와 같은 이유로 이 handler와 충돌하지
        않는다 — Starlette가 더 구체적으로 등록된 409 handler를 먼저 찾는다. 다른
        경로의 `ValueError`(`config.py`의 설정 검증 등)는 요청 처리 중에 발생하지
        않으므로 이 handler에 도달하지 않는다.
        """
        logger.error("Issue body assembly failed: %s", error)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": f"Failed to author or publish the issue: {error}"},
        )

    @app.get("/healthcheck")
    def healthcheck() -> dict[str, str]:
        if settings is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service is unavailable.",
            )
        return {"status": "ok", "service": "agent-orchestration"}

    @app.post(
        "/chat",
        response_model=ChatResponse,
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_401_UNAUTHORIZED: {
                "description": "Invalid orchestration API token.",
                "model": ErrorResponse,
            },
            status.HTTP_500_INTERNAL_SERVER_ERROR: {
                "description": "Failed to save chat interaction.",
                "model": ErrorResponse,
            },
            status.HTTP_502_BAD_GATEWAY: {
                "description": "Failed to call LLM backend.",
                "model": ErrorResponse,
            },
            status.HTTP_503_SERVICE_UNAVAILABLE: {
                "description": "LLM backend is temporarily overloaded.",
                "model": ErrorResponse,
            },
        },
    )
    async def chat(
        request: ChatRequest,
        http_request: Request,
        x_orch_token: Annotated[
            str | None,
            Header(alias="X-Orch-Token", description="공유 오케스트레이션 API 토큰"),
        ] = None,
    ) -> ChatResponse | Response:
        """채팅 프롬프트를 LLM으로 전송 후 PostgreSQL에 저장하고 결과를 반환."""
        runtime_settings = _require_runtime()
        if not x_orch_token or not _api_tokens_match(x_orch_token, runtime_settings.api_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid orchestration API token.",
            )
        start = perf_counter()
        try:
            completion_task = asyncio.create_task(
                generate_response(runtime_settings, request.prompt)
            )
            completion = await _await_request_task(http_request, completion_task)
            if completion is None:
                return Response(status_code=499)
        except LLMBackendOverloadedError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="LLM backend is temporarily overloaded.",
            ) from error
        except LLMBackendError as error:
            logger.error("LLM backend call failed: %s", error)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Failed to call LLM backend.",
            ) from error

        latency_ms = int((perf_counter() - start) * 1000)

        try:
            row = await asyncio.to_thread(
                save_interaction,
                database_url=runtime_settings.database_url,
                table_name=runtime_settings.interactions_table,
                prompt=request.prompt,
                response=completion.text,
                model=completion.model,
                latency_ms=latency_ms,
                token_count=completion.token_count,
                connect_timeout_sec=runtime_settings.database_connect_timeout_sec,
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

    app.include_router(
        experiment_router,
        dependencies=[Depends(_require_orchestration_token)],
    )

    default_openapi = app.openapi

    def documented_openapi() -> dict:
        """누락 헤더도 401로 처리하는 인증 계약을 Swagger에 명시한다."""
        schema = default_openapi()
        for path_item in schema["paths"].values():
            for operation in path_item.values():
                if not isinstance(operation, dict):
                    continue
                for parameter in operation.get("parameters", []):
                    if parameter["name"] == "X-Orch-Token" and parameter["in"] == "header":
                        parameter["required"] = True
                        parameter["schema"] = {"type": "string"}
        return schema

    app.openapi = documented_openapi

    return app


app = create_app()
