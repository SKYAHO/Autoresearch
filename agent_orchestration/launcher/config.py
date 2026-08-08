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
# content-addressed 스냅샷 prefix. 마지막 segment가 CSV 내용의 SHA-256이다(#530).
_TRAINING_DATASET_URI_PATTERN = re.compile(
    r"^gs://[a-z0-9][a-z0-9._-]*/(?:[^\s/]+/)*by-hash/[0-9a-f]{64}/?$"
)


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


def _optional_positive_integer_environment(name: str, *, default: int) -> int:
    """선택 환경 변수의 양의 십진 정수 또는 미설정 기본값을 반환한다."""
    value = os.environ.get(name)
    if value is None:
        return default
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
    # 학습은 opt-in이다(#605). URI가 비어 있으면 executor가 clone → Codex → verify →
    # push 경로만 돌고 학습 단계를 건너뛴다.
    training_dataset_uri: str = ""
    training_timeout_sec: int = 1800
    training_download_timeout_sec: int = 600
    uv_sync_timeout_sec: int = 900
    # 학습이 MLflow run을 기록할 tracking server 좌표다(#624). 비어 있으면 executor에
    # 아무것도 붙지 않고 학습은 Pod 로컬 file store로 떨어진다 — run이 Pod과 함께
    # 사라져 paired 비교가 artifact를 내려받을 대상을 잃는다.
    mlflow_tracking_uri: str = ""

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
            "training_timeout_sec": self.training_timeout_sec,
            "training_download_timeout_sec": self.training_download_timeout_sec,
            "uv_sync_timeout_sec": self.uv_sync_timeout_sec,
        }
        for name, value in positive_integers.items():
            if value <= 0:
                raise LauncherConfigError(f"invalid {name}")
        if self.codex_timeout_sec >= self.active_deadline_sec:
            raise LauncherConfigError(
                "codex_timeout_sec must be less than active_deadline_sec"
            )
        # 형식 오류를 Pod까지 끌고 가지 않는다 — 8개 container를 띄운 뒤 학습 직전에
        # 실패하면 원인이 로그 깊숙이 묻힌다. by-hash 주소는 CSV 내용의 SHA-256이며
        # executor가 받은 바이트를 이 값과 대조한다(#530, #605).
        if self.training_dataset_uri and (
            _TRAINING_DATASET_URI_PATTERN.fullmatch(self.training_dataset_uri) is None
        ):
            raise LauncherConfigError("invalid training_dataset_uri")

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
            ttl_after_finished_sec=_optional_positive_integer_environment(
                "ORCH_TTL_AFTER_FINISHED_SEC",
                default=30,
            ),
            training_dataset_uri=os.environ.get("ORCH_TRAINING_DATASET_URI", "").strip(),
            training_timeout_sec=_optional_positive_integer_environment(
                "ORCH_TRAINING_TIMEOUT_SEC",
                default=1800,
            ),
            training_download_timeout_sec=_optional_positive_integer_environment(
                "ORCH_TRAINING_DOWNLOAD_TIMEOUT_SEC",
                default=600,
            ),
            uv_sync_timeout_sec=_optional_positive_integer_environment(
                "ORCH_UV_SYNC_TIMEOUT_SEC",
                default=900,
            ),
            # launcher가 **받는** 이름에는 `ORCH_` 접두사가 붙지만, executor에
            # **내보내는** 이름은 `MLFLOW_TRACKING_URI`다(`jobs.py`). 두 이름이 다르다 —
            # `src/pipeline/train.py`가 접두사 없는 표준 이름으로 읽기 때문이다.
            mlflow_tracking_uri=os.environ.get(
                "ORCH_MLFLOW_TRACKING_URI", ""
            ).strip(),
        )
