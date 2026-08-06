"""실험 Job launcher의 환경 설정 검증 경계.

[파이프라인] CronJob container가 시작된 뒤 DB 선점과 executor Job 조립 전에 외부 설정을
검증된 불변 값으로 바꾸는 구간을 담당한다.

[기능] DB·namespace·executor image/identity/node pool·GitHub App 좌표·candidate API·Codex
Secret·workspace·동시 실행 상한과 Job 수명 설정을 환경 변수에서 읽고, executor image와
scheduling 좌표 및 Codex 실행 상한과 Job 전체 상한의 순서를 검증한다.

[비책임] 설정의 Kubernetes 주입과 Secret 값 관리(Autoresearch-infra), DB 연결·Job API
호출은 담당하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re


_DIGEST_IMAGE_PATTERN = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_NODE_POOL_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_POSITIVE_INTEGER_PATTERN = re.compile(r"^[1-9][0-9]*$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class LauncherConfigError(ValueError):
    """launcher 환경 설정이 누락됐거나 안전 계약에 맞지 않는다."""


def _required_environment(name: str) -> str:
    """비어 있지 않은 환경 변수 값을 반환한다."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise LauncherConfigError(f"missing {name}")
    return value


def _positive_integer_environment(name: str) -> int:
    """환경 변수에서 양의 십진 정수를 읽는다."""
    value = _required_environment(name)
    if _POSITIVE_INTEGER_PATTERN.fullmatch(value) is None:
        raise LauncherConfigError(f"invalid {name}")
    return int(value)


@dataclass(frozen=True)
class LauncherSettings:
    """한 launcher tick과 생성할 executor Job의 불변 설정."""

    database_url: str
    job_namespace: str
    executor_image: str
    executor_service_account: str
    executor_node_pool: str
    github_app_secret_name: str
    github_app_id: int
    github_app_installation_id: int
    github_repository: str
    max_concurrent_experiments: int
    executor_api_url: str
    executor_api_token_secret_name: str
    codex_home_secret_name: str
    workspace_size_limit: str
    codex_timeout_sec: int
    active_deadline_sec: int
    ttl_after_finished_sec: int = 30

    def __post_init__(self) -> None:
        """빈 좌표·가변 image·비양수 상한을 fail-closed로 거부한다."""
        required_strings = {
            "database_url": self.database_url,
            "job_namespace": self.job_namespace,
            "executor_service_account": self.executor_service_account,
            "github_app_secret_name": self.github_app_secret_name,
            "executor_api_url": self.executor_api_url,
            "executor_api_token_secret_name": self.executor_api_token_secret_name,
            "codex_home_secret_name": self.codex_home_secret_name,
            "workspace_size_limit": self.workspace_size_limit,
        }
        for name, value in required_strings.items():
            if not value.strip():
                raise LauncherConfigError(f"invalid {name}")
        if _DIGEST_IMAGE_PATTERN.fullmatch(self.executor_image) is None:
            raise LauncherConfigError("invalid executor_image")
        if _NODE_POOL_PATTERN.fullmatch(self.executor_node_pool) is None:
            raise LauncherConfigError("invalid executor_node_pool")
        if _REPOSITORY_PATTERN.fullmatch(self.github_repository) is None:
            raise LauncherConfigError("invalid github_repository")
        positive_integers = {
            "github_app_id": self.github_app_id,
            "github_app_installation_id": self.github_app_installation_id,
            "max_concurrent_experiments": self.max_concurrent_experiments,
            "active_deadline_sec": self.active_deadline_sec,
            "ttl_after_finished_sec": self.ttl_after_finished_sec,
            "codex_timeout_sec": self.codex_timeout_sec,
        }
        for name, value in positive_integers.items():
            if value <= 0:
                raise LauncherConfigError(f"invalid {name}")
        if self.codex_timeout_sec >= self.active_deadline_sec:
            raise LauncherConfigError(
                "codex_timeout_sec must be less than active_deadline_sec"
            )

    @classmethod
    def from_environment(cls) -> LauncherSettings:
        """CronJob 환경에서 필수 설정과 Job 수명을 읽는다."""
        return cls(
            database_url=_required_environment("ORCH_DATABASE_URL"),
            job_namespace=_required_environment("ORCH_JOB_NAMESPACE"),
            executor_image=_required_environment("ORCH_EXECUTOR_IMAGE"),
            executor_service_account=_required_environment(
                "ORCH_EXECUTOR_SERVICE_ACCOUNT"
            ),
            executor_node_pool=_required_environment("ORCH_EXECUTOR_NODE_POOL"),
            github_app_secret_name=_required_environment("ORCH_GITHUB_APP_SECRET_NAME"),
            github_app_id=_positive_integer_environment("ORCH_GITHUB_APP_ID"),
            github_app_installation_id=_positive_integer_environment(
                "ORCH_GITHUB_APP_INSTALLATION_ID"
            ),
            github_repository=_required_environment("ORCH_GITHUB_REPOSITORY"),
            max_concurrent_experiments=_positive_integer_environment(
                "ORCH_MAX_CONCURRENT_EXPERIMENTS"
            ),
            executor_api_url=_required_environment("ORCH_EXECUTOR_API_URL"),
            executor_api_token_secret_name=_required_environment(
                "ORCH_EXECUTOR_API_TOKEN_SECRET_NAME"
            ),
            codex_home_secret_name=_required_environment(
                "ORCH_CODEX_HOME_SECRET_NAME"
            ),
            workspace_size_limit=_required_environment(
                "ORCH_EXECUTOR_WORKSPACE_SIZE_LIMIT"
            ),
            codex_timeout_sec=_positive_integer_environment("ORCH_CODEX_TIMEOUT_SEC"),
            active_deadline_sec=_positive_integer_environment(
                "ORCH_ACTIVE_DEADLINE_SEC"
            ),
        )
