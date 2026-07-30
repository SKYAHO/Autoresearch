"""Agent Orchestration 설정 로더.

[파이프라인]
실험형 오케스트레이션 서비스 입구에서 LLM 호출과 영속화를 수행하며,
본 모듈은 서비스 런타임 시작/재시작에서 필요한 환경 설정 값을 검증한다.

[기능]
공유 API 토큰, 선택한 LLM 백엔드에 필요한 Codex 또는 OpenAI 설정, 모델/타임아웃,
PostgreSQL 연결 정보 등 FastAPI 런타임의 공통 설정 값을 단일 진입점으로 정규화한다.

[비책임]
실제 LLM 호출 및 PostgreSQL 스키마 생성/영속화 동작.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
import re
from urllib.parse import urlparse


def _require_env(name: str, value: str | None) -> str:
    stripped = (value or "").strip()
    if not stripped:
        raise ValueError(f"Required environment variable '{name}' is not set.")
    return stripped


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError as error:
        raise ValueError(f"Environment variable '{name}' must be an integer.") from error


def _positive_env_int(name: str, default: int) -> int:
    """양의 정수 환경 변수를 읽는다."""
    value = _env_int(name, default)
    if value < 1:
        raise ValueError(f"Environment variable '{name}' must be greater than zero.")
    return value


@dataclass(frozen=True)
class ServiceSettings:
    """서비스 실행에 필요한 설정 값."""

    openai_api_key: str | None
    openai_model: str
    openai_max_tokens: int
    openai_timeout_sec: int
    database_url: str
    interactions_table: str
    api_token: str
    llm_backend: str = "codex_cli"
    codex_cli_path: str = "codex"
    codex_home: str = ""
    codex_model: str | None = None
    codex_timeout_sec: int = 120
    database_connect_timeout_sec: int = 10


def load_settings() -> ServiceSettings:
    """환경 변수에서 설정을 읽어 타입/기본값을 정합."""
    llm_backend = os.getenv("LLM_BACKEND", "codex_cli").strip().lower()
    if llm_backend not in {"codex_cli", "openai"}:
        raise ValueError("LLM_BACKEND must be one of: codex_cli, openai.")

    openai_api_key = (
        _require_env("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY"))
        if llm_backend == "openai"
        else None
    )
    openai_model = os.getenv("OPENAI_MODEL", "").strip() or "gpt-5.3-codex-spark"
    openai_max_tokens = _positive_env_int("OPENAI_MAX_TOKENS", 1024)
    openai_timeout_sec = _positive_env_int("OPENAI_TIMEOUT_SEC", 60)
    codex_cli_path = (
        _require_env("CODEX_CLI_PATH", os.getenv("CODEX_CLI_PATH", "codex"))
        if llm_backend == "codex_cli"
        else os.getenv("CODEX_CLI_PATH", "codex").strip() or "codex"
    )
    codex_home = (
        _require_env("CODEX_HOME", os.getenv("CODEX_HOME"))
        if llm_backend == "codex_cli"
        else os.getenv("CODEX_HOME", "").strip()
    )
    codex_model = os.getenv("CODEX_MODEL", "").strip() or None
    codex_timeout_sec = _positive_env_int("CODEX_TIMEOUT_SEC", 120)
    api_token = _require_env("ORCH_API_TOKEN", os.getenv("ORCH_API_TOKEN"))
    if len(api_token) < 32:
        raise ValueError("ORCH_API_TOKEN must be at least 32 characters long.")
    database_connect_timeout_sec = _positive_env_int("ORCH_DB_CONNECT_TIMEOUT_SEC", 10)

    database_url = os.getenv("ORCH_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("Either ORCH_DATABASE_URL or DATABASE_URL is required.")
    database_url = database_url.strip()

    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise ValueError(
            f"ORCH_DATABASE_URL must start with postgresql:// or postgres://, got '{parsed.scheme}'."
        )

    interactions_table = os.getenv("ORCH_INTERACTIONS_TABLE", "chat_interactions").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", interactions_table):
        raise ValueError("ORCH_INTERACTIONS_TABLE must be a valid SQL identifier.")
    return ServiceSettings(
        openai_api_key=openai_api_key,
        openai_model=openai_model,
        openai_max_tokens=openai_max_tokens,
        openai_timeout_sec=openai_timeout_sec,
        database_url=database_url,
        interactions_table=interactions_table,
        api_token=api_token,
        llm_backend=llm_backend,
        codex_cli_path=codex_cli_path,
        codex_home=codex_home,
        codex_model=codex_model,
        codex_timeout_sec=codex_timeout_sec,
        database_connect_timeout_sec=database_connect_timeout_sec,
    )
