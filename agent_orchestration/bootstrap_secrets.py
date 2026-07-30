"""Agent Orchestration GKE 런타임 시크릿 부트스트랩.

[파이프라인]
내부 Agent Orchestration API의 GKE 기동 구간에서 Secret Manager 인증을
FastAPI·Codex CLI가 사용할 권한 제한 런타임 파일로 준비한다.

[기능]
API용 DB 연결 파일과 Runner용 최초 Codex OAuth 파일을 각각 독립적으로
준비한다. ``api-database``와 ``runner-codex-auth`` 명시 CLI 역할은 각자 필요한
환경 변수와 Secret Manager reader만 사용하며, Runner 역할의 명시적 복구 opt-in만
기존 OAuth 파일을 새 시크릿 값으로 교체한다. 인자 없는 기존 모듈 실행은 API DB
bootstrap과 호환된다. OAuth 파일은 regular file만 허용하며 dangling symlink도
Secret Manager 조회 전에 거부한다. 시크릿 원문은 로그, 환경 변수, 예외 메시지에
노출하지 않는다.

[비책임]
FastAPI 요청 처리와 PostgreSQL 저장은 ``agent_orchestration.app``이 담당하며,
Secret Manager·Kubernetes 리소스 생성은 Autoresearch-infra 저장소가 담당한다.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import secrets
from urllib.parse import quote


LOGGER = logging.getLogger(__name__)
SecretReader = Callable[[str], bytes]


@dataclass(frozen=True)
class DatabaseBootstrapSettings:
    """API init container가 사용할 DB 시크릿 식별자와 런타임 경로."""

    db_password_secret_id: str
    db_host: str
    db_name: str
    db_user: str
    runtime_dir: Path


@dataclass(frozen=True)
class RunnerAuthBootstrapSettings:
    """Runner init container가 사용할 OAuth 시크릿 식별자와 Codex 경로."""

    codex_auth_secret_id: str
    codex_home: Path


def _require_value(name: str, value: str | None) -> str:
    """공백이 아닌 필수 부트스트랩 환경 값을 검증한다."""
    normalized = (value or "").strip()
    if not normalized:
        raise ValueError(f"Required environment variable '{name}' is not set.")
    return normalized


def load_api_database_bootstrap_settings(
    environ: Mapping[str, str] | None = None,
) -> DatabaseBootstrapSettings:
    """API init container 환경 변수에서 DB 부트스트랩 설정만 읽는다."""
    values = os.environ if environ is None else environ
    return DatabaseBootstrapSettings(
        db_password_secret_id=_require_value(
            "ORCH_DB_PASSWORD_SECRET_ID", values.get("ORCH_DB_PASSWORD_SECRET_ID")
        ),
        db_host=_require_value("ORCH_DB_HOST", values.get("ORCH_DB_HOST")),
        db_name=_require_value("ORCH_DB_NAME", values.get("ORCH_DB_NAME")),
        db_user=_require_value("ORCH_DB_USER", values.get("ORCH_DB_USER")),
        runtime_dir=Path(
            _require_value("ORCH_RUNTIME_DIR", values.get("ORCH_RUNTIME_DIR"))
        ),
    )


def load_runner_codex_auth_bootstrap_settings(
    environ: Mapping[str, str] | None = None,
) -> RunnerAuthBootstrapSettings:
    """Runner init container 환경 변수에서 OAuth 부트스트랩 설정만 읽는다."""
    values = os.environ if environ is None else environ
    return RunnerAuthBootstrapSettings(
        codex_auth_secret_id=_require_value(
            "ORCH_CODEX_AUTH_SECRET_ID", values.get("ORCH_CODEX_AUTH_SECRET_ID")
        ),
        codex_home=Path(_require_value("CODEX_HOME", values.get("CODEX_HOME"))),
    )


def read_secret_manager_secret(secret_id: str) -> bytes:
    """Workload Identity ADC로 Secret Manager의 한 버전을 읽는다."""
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    response = client.access_secret_version(request={"name": secret_id})
    return response.payload.data


def _read_secret(secret_id: str, read_secret: SecretReader) -> bytes:
    """시크릿 조회 실패에서 시크릿 원문을 제거한 오류만 노출한다."""
    try:
        value = read_secret(secret_id)
    except Exception:
        LOGGER.error("Secret Manager 시크릿을 읽지 못했습니다: %s", secret_id)
        raise RuntimeError(f"Failed to read bootstrap secret '{secret_id}'.") from None
    if not value:
        raise RuntimeError(f"Bootstrap secret '{secret_id}' is empty.")
    return value


def _write_private_file(path: Path, contents: bytes) -> None:
    """원자적으로 교체되는 소유자 전용 파일을 기록한다.

    Kubernetes ``emptyDir``와 PVC mount root는 kubelet이 소유할 수 있으므로
    비루트 init container는 부모 디렉터리 mode를 변경하지 않는다. 파일 자체만
    생성과 교체 뒤 0600으로 제한한다.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    descriptor = os.open(
        temporary_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as file_handle:
            file_handle.write(contents)
            file_handle.flush()
            os.fsync(file_handle.fileno())
        os.replace(temporary_path, path)
        path.chmod(0o600)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _database_url(settings: DatabaseBootstrapSettings, password: bytes) -> str:
    """DB 비밀번호가 URL 의미를 바꾸지 않도록 percent-encoding 한다."""
    try:
        decoded_password = password.decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeError(
            f"Bootstrap secret '{settings.db_password_secret_id}' is not valid UTF-8."
        ) from None
    return (
        "postgresql://"
        f"{quote(settings.db_user, safe='')}:{quote(decoded_password, safe='')}"
        f"@{settings.db_host}/{quote(settings.db_name, safe='')}"
    )


def bootstrap_api_database(
    settings: DatabaseBootstrapSettings,
    read_secret: SecretReader,
) -> None:
    """API가 사용할 DB 연결 파일만 권한 제한 런타임 볼륨에 준비한다."""
    database_password = _read_secret(settings.db_password_secret_id, read_secret)
    database_env = f"ORCH_DATABASE_URL={_database_url(settings, database_password)}\n"
    _write_private_file(settings.runtime_dir / "db.env", database_env.encode("utf-8"))


def bootstrap_runner_codex_auth(
    settings: RunnerAuthBootstrapSettings,
    read_secret: SecretReader,
    *,
    replace_existing: bool = False,
) -> None:
    """Runner의 최초 OAuth 파일을 준비하거나 명시 opt-in으로 기존 파일을 교체한다."""
    auth_path = settings.codex_home / "auth.json"
    if auth_path.is_symlink():
        raise RuntimeError("CODEX_HOME/auth.json must be a regular file.")
    if auth_path.exists():
        if not auth_path.is_file():
            raise RuntimeError("CODEX_HOME/auth.json must be a regular file.")
        if not replace_existing:
            auth_path.chmod(0o600)
            return

    auth_payload = _read_secret(settings.codex_auth_secret_id, read_secret)
    _write_private_file(auth_path, auth_payload)
    LOGGER.info("Codex OAuth 인증 파일을 준비했습니다: %s", settings.codex_auth_secret_id)


def main(argv: Sequence[str] | None = None) -> int:
    """선택한 init container 역할의 권한 제한 시크릿 부트스트랩을 수행한다."""
    parser = argparse.ArgumentParser(description="Bootstrap Agent Orchestration secrets.")
    parser.add_argument(
        "role",
        nargs="?",
        choices=("api-database", "runner-codex-auth"),
        help="Bootstrap role. Defaults to api-database for compatibility.",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="Replace an existing Runner Codex OAuth file from Secret Manager.",
    )
    arguments = parser.parse_args(argv)
    role = arguments.role
    if arguments.replace_existing and role != "runner-codex-auth":
        parser.error("--replace-existing is only valid for runner-codex-auth.")
    if role == "runner-codex-auth":
        bootstrap_runner_codex_auth(
            load_runner_codex_auth_bootstrap_settings(),
            read_secret_manager_secret,
            replace_existing=arguments.replace_existing,
        )
        return 0

    bootstrap_api_database(load_api_database_bootstrap_settings(), read_secret_manager_secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
