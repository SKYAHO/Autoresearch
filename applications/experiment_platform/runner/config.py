"""비공개 Codex Runner 설정 로더.

[파이프라인]
비공개 Runner가 API 요청을 Codex CLI로 실행하기 직전 필요한 런타임 설정을
검증하는 구간이다.

[기능]
Codex 실행 경계 설정, Runner 동시성 상한, API가 전달한 내부 요청 토큰을 환경
변수에서 읽어 정규화하고, Codex 하위 프로세스 정리 시간까지 포함한 API-to-Runner
HTTP timeout 관계를 기동 전에 검증한다.

[비책임]
API 공유 토큰·데이터베이스·OpenAI 설정(applications.experiment_platform.api.config),
OAuth 자격 증명 값의 수집·기록·저장.
"""

from __future__ import annotations

from dataclasses import dataclass
import os

from applications.experiment_platform.shared.codex import CodexSettings


def _require_env(name: str, value: str | None) -> str:
    """공백을 제외한 필수 환경 변수를 반환한다."""
    stripped = (value or "").strip()
    if not stripped:
        raise ValueError(f"Required environment variable '{name}' is not set.")
    return stripped


def _positive_env_int(name: str, default: int | None) -> int:
    """양의 정수 환경 변수를 읽는다."""
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        if default is None:
            raise ValueError(f"Required environment variable '{name}' is not set.")
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"Environment variable '{name}' must be an integer.") from error
    if value < 1:
        raise ValueError(f"Environment variable '{name}' must be greater than zero.")
    return value


@dataclass(frozen=True)
class RunnerSettings:
    """Runner가 소유하는 Codex 실행·동시성·내부 요청 인증 설정."""

    codex: CodexSettings
    max_concurrency: int
    api_token: str


def load_runner_settings() -> RunnerSettings:
    """Runner 전용 환경 변수만 읽어 런타임 설정을 생성한다."""
    api_token = _require_env("ORCH_RUNNER_TOKEN", os.getenv("ORCH_RUNNER_TOKEN"))
    if len(api_token) < 32:
        raise ValueError("ORCH_RUNNER_TOKEN must be at least 32 characters long.")
    codex_timeout_sec = _positive_env_int("CODEX_TIMEOUT_SEC", 110)
    runner_timeout_sec = _positive_env_int("CODEX_RUNNER_TIMEOUT_SEC", None)
    if codex_timeout_sec + 5 >= runner_timeout_sec:
        raise ValueError(
            "CODEX_TIMEOUT_SEC + 5 must be less than CODEX_RUNNER_TIMEOUT_SEC."
        )
    return RunnerSettings(
        codex=CodexSettings(
            cli_path=_require_env("CODEX_CLI_PATH", os.getenv("CODEX_CLI_PATH", "codex")),
            home=_require_env("CODEX_HOME", os.getenv("CODEX_HOME")),
            model=os.getenv("CODEX_MODEL", "").strip() or None,
            timeout_sec=codex_timeout_sec,
        ),
        max_concurrency=_positive_env_int("RUNNER_MAX_CONCURRENCY", 1),
        api_token=api_token,
    )
