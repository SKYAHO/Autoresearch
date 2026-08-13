"""Executor Pod branch-creator CLI의 봉인 SHA 기반 exp branch 생성 경계.

[파이프라인]
launcher가 실험·이슈·branch·기준 SHA를 봉인해 Pod를 기동하고 initContainer가 token
파일을 전달한 뒤, workspace-preparer가 checkout하기 전에 exp ref 상태를 기록하는 구간을
담당한다.

[기능]
봉인 좌표와 token 파일을 읽어 branch_creator의 immutable ref 생성/관찰을 실행하고,
실패 시 자격 증명을 제외한 예외 종류·정제 사유·HTTP 상태와 봉인 좌표를 기록한다.

[비책임]
GitHub App private key 읽기와 token 발급(`token_minter.py`), ref update/reset/force-push,
이슈 검증·Git checkout(`workspace.py`), Kubernetes Job 생성과 Codex 실행은 담당하지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from applications.experiment_platform.executor.branch_creator import (
    BranchCreatorInput,
    RefClient,
    ensure_issue_branch,
)
from applications.experiment_platform.executor.config import ExecutorConfigError
from applications.experiment_platform.shared.github_refs import GitHubRefError, GitHubRefs


_LOGGER = logging.getLogger(__name__)


class ExecutorTokenError(RuntimeError):
    """Executor가 token 파일을 안전하게 읽지 못했다."""


def _read_token(path: Path) -> str:
    """initContainer가 쓴 token 파일에서 비어 있지 않은 값을 읽는다."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ExecutorTokenError("token_file_unavailable") from error
    if not token:
        raise ExecutorTokenError("token_file_empty")
    return token


async def _run(refs: RefClient) -> int:
    """Pod 환경 입력으로 branch를 bootstrap하고 executor exit code를 반환한다."""
    coordinates: BranchCreatorInput | None = None
    try:
        coordinates = BranchCreatorInput.from_environment()
        token = _read_token(coordinates.token_file)
        result = await ensure_issue_branch(coordinates, refs, token)
    except (
        ExecutorConfigError,
        ExecutorTokenError,
        GitHubRefError,
    ) as error:
        reason = getattr(error, "reason", str(error))
        status_code = getattr(error, "status_code", None)
        if coordinates is None:
            _LOGGER.error(
                "executor branch bootstrap failed error_type=%s reason=%s "
                "status_code=%s",
                type(error).__name__,
                reason,
                status_code,
            )
        else:
            _LOGGER.error(
                "executor branch bootstrap failed error_type=%s reason=%s "
                "status_code=%s "
                "experiment_id=%s issue_number=%s branch=%s base_sha=%s",
                type(error).__name__,
                reason,
                status_code,
                coordinates.experiment_id,
                coordinates.issue_number,
                coordinates.issue_branch,
                coordinates.base_dev_sha,
            )
        return 1

    _LOGGER.info(
        "executor branch creator complete "
        "experiment_id=%s issue_number=%s branch=%s base_sha=%s created=%s remote_tip=%s",
        coordinates.experiment_id,
        coordinates.issue_number,
        coordinates.issue_branch,
        coordinates.base_dev_sha,
        result.created,
        result.remote_tip,
    )
    return 0


def main(*, refs: RefClient | None = None) -> int:
    """Executor main container의 동기식 CLI 진입점."""
    return asyncio.run(_run(refs if refs is not None else GitHubRefs()))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
