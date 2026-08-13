"""Agent Orchestration LLM 백엔드 경계.

[파이프라인]
오케스트레이션 실험 API가 프롬프트를 받고 영속화하기 전의 LLM 추론 구간을
담당한다. 로컬 Codex CLI, 비공개 Codex Runner, OpenAI API를 같은 응답 계약으로
정규화한다.

[기능]
공통 Codex 실행 경계 또는 비공개 Runner HTTP·OpenAI Responses API를 호출하고,
텍스트·모델명·토큰 사용량을 API 계층이 저장 가능한 결과로 반환한다.

[비책임]
HTTP 라우팅과 상태 코드 변환(main.py), PostgreSQL 스키마·저장(db.py),
사용자 인증·OAuth 로그인 및 자격 증명 저장, Runner 동시성 제어
(applications.experiment_platform.runner.app).
"""

from __future__ import annotations

import logging

import httpx
from openai import AsyncOpenAI, OpenAIError

from applications.experiment_platform.api.config import ServiceSettings
from applications.experiment_platform.shared.contracts import (
    LLMBackendError,
    LLMBackendOverloadedError,
    LLMResult,
)


logger = logging.getLogger(__name__)


async def generate_response(settings: ServiceSettings, prompt: str) -> LLMResult:
    """선택한 백엔드로 프롬프트를 전송해 저장 가능한 최종 응답을 반환한다."""
    if settings.llm_backend == "codex_cli":
        return await _generate_codex_cli(settings, prompt)
    if settings.llm_backend == "codex_runner":
        return await _generate_codex_runner(settings, prompt)
    if settings.llm_backend == "openai":
        return await _generate_openai(settings, prompt)
    raise LLMBackendError("Unsupported LLM backend.")


async def _generate_codex_cli(settings: ServiceSettings, prompt: str) -> LLMResult:
    """기존 로컬 Codex CLI 백엔드를 공통 실행 경계로 위임한다."""
    # GKE API 이미지는 Runner로만 Codex 실행을 위임하므로, Runner 전용 실행 모듈을
    # 앱 import 시점에 적재하지 않는다. 로컬 codex_cli 백엔드만 이 의존성을 읽는다.
    from applications.experiment_platform.shared.codex import CodexSettings, generate_codex_response

    return await generate_codex_response(
        CodexSettings(
            cli_path=settings.codex_cli_path,
            home=settings.codex_home,
            model=settings.codex_model,
            timeout_sec=settings.codex_timeout_sec,
        ),
        prompt,
    )


async def _generate_codex_runner(settings: ServiceSettings, prompt: str) -> LLMResult:
    """비공개 Runner HTTP 계약을 공통 LLM 결과 계약으로 정규화한다."""
    if not settings.codex_runner_url or not settings.codex_runner_token:
        raise LLMBackendError("Codex runner call failed.")

    try:
        async with httpx.AsyncClient(
            timeout=settings.codex_runner_timeout_sec,
            trust_env=False,
        ) as client:
            response = await client.post(
                f"{settings.codex_runner_url.rstrip('/')}/v1/generate",
                json={"prompt": prompt},
                headers={"X-Runner-Token": settings.codex_runner_token},
            )
            if response.status_code == 503:
                raise LLMBackendOverloadedError("Codex runner is overloaded.")
            response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Runner response must be an object.")
        text = payload["response"]
        model = payload["model"]
        token_count = payload["token_count"]
        if not isinstance(text, str) or not isinstance(model, str):
            raise ValueError("Runner response contains invalid text or model.")
        if token_count is not None and (
            not isinstance(token_count, int) or isinstance(token_count, bool)
        ):
            raise ValueError("Runner response contains invalid token count.")
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        raise LLMBackendError("Codex runner call failed.") from None

    return LLMResult(text=text, model=model, token_count=token_count)


async def _generate_openai(settings: ServiceSettings, prompt: str) -> LLMResult:
    """향후 API 전환을 위해 OpenAI Responses API를 호출한다."""
    if not settings.openai_api_key:
        raise LLMBackendError("OpenAI API key is not configured.")

    client = AsyncOpenAI(
        api_key=settings.openai_api_key,
        timeout=settings.openai_timeout_sec,
    )
    try:
        response = await client.responses.create(
            model=settings.openai_model,
            input=prompt,
            max_output_tokens=settings.openai_max_tokens,
        )
    except OpenAIError as error:
        raise LLMBackendError("OpenAI API call failed.") from error
    finally:
        await client.close()

    if response.status != "completed":
        logger.warning("OpenAI Responses API returned incomplete status=%s", response.status)
        raise LLMBackendError("OpenAI API response did not complete.")
    if not response.output_text:
        raise LLMBackendError("OpenAI API returned empty output.")
    return LLMResult(
        text=response.output_text,
        model=settings.openai_model,
        token_count=response.usage.total_tokens if response.usage else None,
    )
