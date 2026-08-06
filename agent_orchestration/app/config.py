"""Agent Orchestration 설정 로더.

[파이프라인]
실험형 오케스트레이션 서비스 입구에서 LLM 호출과 영속화를 수행하며,
본 모듈은 서비스 런타임 시작/재시작에서 필요한 환경 설정 값을 검증한다.

[기능]
공유 API 토큰, 선택한 LLM 백엔드에 필요한 Codex 또는 OpenAI 설정, 모델/타임아웃,
PostgreSQL 연결 정보 등 FastAPI 런타임의 공통 설정 값을 단일 진입점으로 정규화한다.
Runner 백엔드에서는 외부 API 인증과 API-to-Runner 내부 인증이 같은 토큰을 재사용하지
않도록 기동 전에 거부한다. 이슈 발행 경로가 쓰는 GitHub 자격·발행 대상 저장소·서버
소유 실험 기본값(`ExperimentDefaults`)도 여기서 검증해 `ServiceSettings`에 담는다.
`get_settings`는 `app.state.settings`에서 요청 단위로 이 값을 꺼내는 FastAPI
의존성이다 — 라우터가 `create_app()`의 클로저에 접근할 수 없어 필요하다.

[비책임]
실제 LLM 호출 및 PostgreSQL 스키마 생성/영속화 동작.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import urlparse

from fastapi import HTTPException, Request, status

from agent_orchestration.app.experiments.issue_authoring import ExperimentDefaults


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


def _required_positive_env_int(name: str) -> int:
    """필수 양의 정수 환경 변수를 읽는다."""
    raw_value = _require_env(name, os.getenv(name))
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"Environment variable '{name}' must be an integer.") from error
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
    github_token: str
    github_repository: str
    gh_timeout_sec: int
    issue_daily_limit: int
    experiment_defaults: ExperimentDefaults
    baseline_github_app_id: int | None = None
    baseline_github_app_installation_id: int | None = None
    baseline_github_app_private_key_path: Path | None = None
    llm_backend: str = "codex_cli"
    codex_cli_path: str = "codex"
    codex_home: str = ""
    codex_model: str | None = None
    codex_timeout_sec: int = 110
    codex_runner_url: str | None = None
    codex_runner_timeout_sec: int = 120
    codex_runner_token: str | None = None
    database_connect_timeout_sec: int = 10


def load_settings() -> ServiceSettings:
    """환경 변수에서 설정을 읽어 타입/기본값을 정합."""
    llm_backend = os.getenv("LLM_BACKEND", "codex_cli").strip().lower()
    if llm_backend not in {"codex_cli", "codex_runner", "openai"}:
        raise ValueError("LLM_BACKEND must be one of: codex_cli, codex_runner, openai.")

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
    codex_timeout_sec = _positive_env_int("CODEX_TIMEOUT_SEC", 110)
    codex_runner_timeout_sec = _positive_env_int("CODEX_RUNNER_TIMEOUT_SEC", 120)
    codex_runner_url = os.getenv("CODEX_RUNNER_URL", "").strip() or None
    codex_runner_token = os.getenv("ORCH_RUNNER_TOKEN", "").strip() or None
    if llm_backend == "codex_runner":
        codex_runner_url = _require_env("CODEX_RUNNER_URL", codex_runner_url)
        codex_runner_token = _require_env("ORCH_RUNNER_TOKEN", codex_runner_token)
        if len(codex_runner_token) < 32:
            raise ValueError("ORCH_RUNNER_TOKEN must be at least 32 characters long.")
        parsed_runner_url = urlparse(codex_runner_url)
        if parsed_runner_url.scheme not in {"http", "https"} or not parsed_runner_url.netloc:
            raise ValueError("CODEX_RUNNER_URL must be an absolute HTTP(S) URL.")
    api_token = _require_env("ORCH_API_TOKEN", os.getenv("ORCH_API_TOKEN"))
    if len(api_token) < 32:
        raise ValueError("ORCH_API_TOKEN must be at least 32 characters long.")
    if llm_backend == "codex_runner" and codex_runner_token == api_token:
        raise ValueError("ORCH_API_TOKEN and ORCH_RUNNER_TOKEN must differ.")
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

    github_token = _require_env("ORCH_GITHUB_TOKEN", os.getenv("ORCH_GITHUB_TOKEN"))
    github_repository = _require_env(
        "ORCH_GITHUB_REPOSITORY", os.getenv("ORCH_GITHUB_REPOSITORY")
    )
    # `gh issue create`의 결과 URL을 이 값과 대조해 다른 저장소에 열린 이슈를 거부한다.
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", github_repository):
        raise ValueError("ORCH_GITHUB_REPOSITORY must be 'owner/repo'.")
    baseline_github_app_id = _required_positive_env_int("ORCH_BASELINE_GITHUB_APP_ID")
    baseline_github_app_installation_id = _required_positive_env_int(
        "ORCH_BASELINE_GITHUB_APP_INSTALLATION_ID"
    )
    baseline_github_app_private_key_path = Path(
        _require_env(
            "ORCH_BASELINE_GITHUB_APP_PRIVATE_KEY_PATH",
            os.getenv("ORCH_BASELINE_GITHUB_APP_PRIVATE_KEY_PATH"),
        )
    )
    gh_timeout_sec = _positive_env_int("ORCH_GH_TIMEOUT_SEC", 30)
    issue_daily_limit = _positive_env_int("ORCH_ISSUE_DAILY_LIMIT", 20)
    experiment_defaults = ExperimentDefaults(
        dataset_source=_require_env(
            "ORCH_EXPERIMENT_DATASET_SOURCE",
            os.getenv("ORCH_EXPERIMENT_DATASET_SOURCE"),
        ),
        training_config_ref=_require_env(
            "ORCH_EXPERIMENT_TRAINING_CONFIG_REF",
            os.getenv("ORCH_EXPERIMENT_TRAINING_CONFIG_REF"),
        ),
    )
    return ServiceSettings(
        openai_api_key=openai_api_key,
        openai_model=openai_model,
        openai_max_tokens=openai_max_tokens,
        openai_timeout_sec=openai_timeout_sec,
        database_url=database_url,
        interactions_table=interactions_table,
        api_token=api_token,
        github_token=github_token,
        github_repository=github_repository,
        gh_timeout_sec=gh_timeout_sec,
        issue_daily_limit=issue_daily_limit,
        experiment_defaults=experiment_defaults,
        baseline_github_app_id=baseline_github_app_id,
        baseline_github_app_installation_id=baseline_github_app_installation_id,
        baseline_github_app_private_key_path=baseline_github_app_private_key_path,
        llm_backend=llm_backend,
        codex_cli_path=codex_cli_path,
        codex_home=codex_home,
        codex_model=codex_model,
        codex_timeout_sec=codex_timeout_sec,
        codex_runner_url=codex_runner_url,
        codex_runner_timeout_sec=codex_runner_timeout_sec,
        codex_runner_token=codex_runner_token,
        database_connect_timeout_sec=database_connect_timeout_sec,
    )


def get_settings(request: Request) -> ServiceSettings:
    """FastAPI app state에 등록된 설정을 요청 단위로 제공한다.

    `database.get_db_session`과 같은 이유다 — lifespan이 `settings`를 채우기 전의
    startup 창에서는 500 대신 503으로 구분해 응답한다.
    """
    settings: ServiceSettings | None = getattr(request.app.state, "settings", None)
    if settings is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service is unavailable.",
        )
    return settings
