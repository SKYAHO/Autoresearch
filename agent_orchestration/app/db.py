"""Agent Orchestration 영속화 유틸리티.

[파이프라인]
오케스트레이션 실험에서 생성된 채팅 요청/응답 기록을 영속화 구간에
적재해 추후 분석 및 비용/품질 회귀 점검에 사용한다.

[기능]
요청·응답 테이블 스키마 생성과 저장 동작을 담당하고, 저장 결과를
응답 가능한 도메인 모델로 반환한다.

[비책임]
LLM 호출 본문 생성·라우팅, 사용자 인증, API 라우트 핸들링.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re

from psycopg import connect
from psycopg.rows import class_row


@dataclass(frozen=True)
class ChatRow:
    """저장 결과를 반환할 때 사용하는 도메인 모델."""

    id: int
    prompt: str
    response: str
    model: str
    latency_ms: int
    token_count: int | None
    created_at: datetime


def _validate_identifier(value: str) -> str:
    """테이블명으로 사용할 안전한 SQL 식별자만 허용한다."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("Invalid table name.")
    return value


def ensure_schema(database_url: str, table_name: str, connect_timeout_sec: int = 10) -> None:
    """`chat_interactions` 저장 테이블을 최초 실행 시 보장."""
    safe_table = _validate_identifier(table_name)
    with connect(database_url, connect_timeout=connect_timeout_sec) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {safe_table}
                (
                    id BIGSERIAL PRIMARY KEY,
                    prompt TEXT NOT NULL,
                    response TEXT NOT NULL,
                    model TEXT NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    token_count INTEGER,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
        connection.commit()


def save_interaction(
    database_url: str,
    table_name: str,
    prompt: str,
    response: str,
    model: str,
    latency_ms: int,
    token_count: int | None,
    connect_timeout_sec: int = 10,
) -> ChatRow:
    """채팅 결과를 DB에 저장하고 생성된 레코드를 반환."""
    safe_table = _validate_identifier(table_name)
    params = {
        "prompt": prompt,
        "response": response,
        "model": model,
        "latency_ms": latency_ms,
        "token_count": token_count,
    }
    query = f"""
        INSERT INTO {safe_table} (prompt, response, model, latency_ms, token_count)
        VALUES (%(prompt)s, %(response)s, %(model)s, %(latency_ms)s, %(token_count)s)
        RETURNING id, prompt, response, model, latency_ms, token_count, created_at
    """
    row_factory = class_row(ChatRow)
    with connect(
        database_url,
        row_factory=row_factory,
        connect_timeout=connect_timeout_sec,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, params)
            row = cursor.fetchone()
        connection.commit()
    if row is None:
        raise RuntimeError("Failed to fetch chat_interaction insert result.")
    return row
