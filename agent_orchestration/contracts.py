"""Agent Orchestration LLM 결과 계약.

[파이프라인]
오케스트레이션 API와 비공개 Codex Runner가 프롬프트 추론 결과를 PostgreSQL에
저장하기 전 공유하는 경계에 위치한다.

[기능]
모든 LLM 백엔드가 반환하는 텍스트·모델·토큰 사용량과, API 계층이 502로 변환할
백엔드 오류를 하나의 불변 계약으로 정의한다.

[비책임]
Codex CLI 실행(agent_orchestration.codex), Runner HTTP 라우팅
(agent_orchestration.runner.app), OpenAI API 호출(agent_orchestration.app.llm).
"""

from __future__ import annotations

from dataclasses import dataclass


class LLMBackendError(RuntimeError):
    """외부 LLM 백엔드 호출을 안전하게 API 계층으로 전달하는 오류."""


@dataclass(frozen=True)
class LLMResult:
    """LLM 백엔드가 반환한 저장용 최종 응답."""

    text: str
    model: str
    token_count: int | None
