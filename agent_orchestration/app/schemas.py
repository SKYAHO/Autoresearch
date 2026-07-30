"""Agent Orchestration API 스키마.

[파이프라인]
오케스트레이션 실험 API의 요청/응답 경계에서 프롬프트 입력과 LLM 저장
결과를 상호 계약으로 검증한다.

[기능]
`/chat` 요청의 입력 제한과, 응답으로 노출되는 저장 메타데이터 구조를
정의한다.

[비책임]
DB 접근, LLM 추론, 인증/세션 정책.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """단일 채팅 요청."""

    prompt: str = Field(
        min_length=1,
        max_length=8192,
        description="LLM 백엔드에 전달할 사용자 프롬프트",
    )


class ChatResponse(BaseModel):
    """LLM 응답 저장 후 반환 payload."""

    id: int
    prompt: str
    response: str
    model: str
    latency_ms: int
    token_count: int | None
    created_at: datetime
