"""Executor Pod initContainer의 GitHub installation token 파일 발급 경계.

[파이프라인]
launcher가 executor Pod를 기동한 뒤 main container가 exp branch를 만들기 전에,
initContainer가 짧은 수명의 GitHub App installation token을 memory volume에 전달하는
구간을 담당한다.

[기능]
App private key로 contents:write token을 한 번 발급하고, 같은 volume의 0400 임시
파일을 `os.replace`하여 main container가 읽을 token 파일로 원자 교체한다. 실패 시
자격 증명을 제외한 예외 종류·정제 사유·HTTP 상태를 기록한다.

[비책임]
private key의 Secret mount와 memory volume 구성(Autoresearch-infra), token 재발급,
Git ref 조회·생성(`main.py`)은 담당하지 않는다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
import os
from pathlib import Path
import tempfile
from typing import Protocol

from agent_orchestration.executor.config import ExecutorConfigError, TokenMinterInput
from agent_orchestration.github_app import (
    GitHubAppCredentials,
    GitHubAppError,
    InstallationToken,
    create_installation_token,
)


_LOGGER = logging.getLogger(__name__)
_CONTENTS_WRITE_PERMISSION = {"contents": "write"}


class TokenMinterError(RuntimeError):
    """Token 파일 전달 경계가 시크릿 비노출 사유와 함께 실패했다."""


class TokenFactory(Protocol):
    """GitHub installation token 발급 호출 계약."""

    async def __call__(
        self,
        credentials: GitHubAppCredentials,
        *,
        permissions: Mapping[str, str],
    ) -> InstallationToken: ...


async def write_installation_token(
    *,
    credentials: GitHubAppCredentials,
    output: Path,
    permissions: Mapping[str, str],
    token_factory: TokenFactory = create_installation_token,
) -> None:
    """발급한 token을 같은 디렉터리의 0400 파일을 거쳐 원자 교체한다."""
    token = await token_factory(credentials, permissions=permissions)
    if not token.value:
        raise TokenMinterError("empty_token")

    temporary_path: Path | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=output.parent,
            prefix=f".{output.name}.",
        )
        temporary_path = Path(temporary_name)
        os.fchmod(descriptor, 0o400)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            stream.write(token.value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, output)
        temporary_path = None
    except OSError as error:
        raise TokenMinterError("token_file_write_failed") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


async def _run(token_factory: TokenFactory) -> int:
    """환경 좌표로 token 파일을 만들고 initContainer exit code를 반환한다."""
    try:
        config = TokenMinterInput.from_environment()
        await write_installation_token(
            credentials=config.credentials,
            output=config.output,
            permissions=_CONTENTS_WRITE_PERMISSION,
            token_factory=token_factory,
        )
    except (ExecutorConfigError, GitHubAppError, TokenMinterError) as error:
        _LOGGER.error(
            "installation token mint failed error_type=%s reason=%s status_code=%s",
            type(error).__name__,
            getattr(error, "reason", str(error)),
            getattr(error, "status_code", None),
        )
        return 1
    _LOGGER.info("installation token file ready")
    return 0


def main(*, token_factory: TokenFactory = create_installation_token) -> int:
    """Token-minter initContainer의 동기식 CLI 진입점."""
    return asyncio.run(_run(token_factory))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
