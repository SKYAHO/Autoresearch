"""Executor Pod main container의 봉인 SHA 기반 exp branch 생성 경계.

[파이프라인]
launcher가 실험·이슈·branch·기준 SHA를 봉인해 Pod를 기동하고 initContainer가 token
파일을 전달한 뒤, 실제 실험 코드가 checkout·실행되기 전에 exp ref를 확정하는 구간을
담당한다.

[기능]
봉인 좌표와 token 파일을 읽어 기존 ref가 같은 SHA인지 확인하고, 없을 때만 생성한다.
생성 422 경합은 한 번 재조회하여 같은 SHA인 경우에만 멱등 성공으로 처리한다.

[비책임]
GitHub App private key 읽기와 token 발급(`token_minter.py`), ref update/reset/force-push,
Kubernetes Job 생성과 후속 Git checkout·실험 실행은 담당하지 않는다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Protocol

from agent_orchestration.executor.config import (
    BranchBootstrapInput,
    ExecutorConfigError,
)
from agent_orchestration.github_refs import GitHubRefError, GitHubRefs


_LOGGER = logging.getLogger(__name__)


class BranchConflictError(RuntimeError):
    """기존 exp ref가 launcher가 봉인한 기준 SHA와 다르다."""


class ExecutorTokenError(RuntimeError):
    """Executor가 token 파일을 안전하게 읽지 못했다."""


class RefClient(Protocol):
    """Branch bootstrap에 필요한 GitHub ref 연산."""

    async def get_sha(
        self, repository: str, ref: str, token: str
    ) -> str | None: ...

    async def create(
        self,
        repository: str,
        ref: str,
        sha: str,
        token: str,
    ) -> str: ...


@dataclass(frozen=True)
class BranchBootstrapResult:
    """Branch bootstrap이 새 ref를 만들었는지 나타낸다."""

    created: bool


def _read_token(path: Path) -> str:
    """initContainer가 쓴 token 파일에서 비어 있지 않은 값을 읽는다."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ExecutorTokenError("token_file_unavailable") from error
    if not token:
        raise ExecutorTokenError("token_file_empty")
    return token


async def bootstrap_branch(
    coordinates: BranchBootstrapInput,
    refs: RefClient,
    token: str,
) -> BranchBootstrapResult:
    """봉인 SHA에서 exp ref를 멱등 생성하며 다른 SHA는 변경하지 않는다."""
    ref = f"heads/{coordinates.issue_branch}"
    existing_sha = await refs.get_sha(
        coordinates.github_repository,
        ref,
        token,
    )
    if existing_sha is not None:
        if existing_sha == coordinates.base_dev_sha:
            return BranchBootstrapResult(created=False)
        raise BranchConflictError("branch_ref_conflict")

    try:
        await refs.create(
            coordinates.github_repository,
            ref,
            coordinates.base_dev_sha,
            token,
        )
    except GitHubRefError as error:
        if error.status_code != 422:
            raise
        raced_sha = await refs.get_sha(
            coordinates.github_repository,
            ref,
            token,
        )
        if raced_sha == coordinates.base_dev_sha:
            return BranchBootstrapResult(created=False)
        if raced_sha is not None:
            raise BranchConflictError("branch_ref_conflict") from error
        raise
    return BranchBootstrapResult(created=True)


async def _run(refs: RefClient) -> int:
    """Pod 환경 입력으로 branch를 bootstrap하고 executor exit code를 반환한다."""
    coordinates: BranchBootstrapInput | None = None
    try:
        coordinates = BranchBootstrapInput.from_environment()
        token = _read_token(coordinates.token_file)
        result = await bootstrap_branch(coordinates, refs, token)
    except (
        BranchConflictError,
        ExecutorConfigError,
        ExecutorTokenError,
        GitHubRefError,
    ):
        if coordinates is None:
            _LOGGER.error("executor branch bootstrap failed")
        else:
            _LOGGER.error(
                "executor branch bootstrap failed "
                "experiment_id=%s issue_number=%s branch=%s base_sha=%s",
                coordinates.experiment_id,
                coordinates.issue_number,
                coordinates.issue_branch,
                coordinates.base_dev_sha,
            )
        return 1

    _LOGGER.info(
        "executor branch bootstrap complete "
        "experiment_id=%s issue_number=%s branch=%s base_sha=%s created=%s",
        coordinates.experiment_id,
        coordinates.issue_number,
        coordinates.issue_branch,
        coordinates.base_dev_sha,
        result.created,
    )
    return 0


def main(*, refs: RefClient | None = None) -> int:
    """Executor main container의 동기식 CLI 진입점."""
    return asyncio.run(_run(refs if refs is not None else GitHubRefs()))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
