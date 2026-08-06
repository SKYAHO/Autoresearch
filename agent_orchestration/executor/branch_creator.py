"""Executor Pod의 봉인 SHA 기반 exp branch 생성 경계.

[파이프라인]
launcher가 실험·이슈·branch·기준 SHA를 봉인하고 token-minter가 제한된 토큰을 전달한 뒤,
workspace-preparer가 checkout하기 전에 원격 exp ref를 준비하는 구간을 담당한다.

[기능]
봉인 좌표에서 없던 ref만 기준 SHA로 생성하고, 기존 ref는 SHA가 같거나 달라도 절대
변경하지 않은 채 관찰한 remote tip을 반환한다. 422 생성 경합도 한 번 재조회해 같은
규칙으로 수렴시킨다.

[비책임]
GitHub App token 발급(`token_minter.py`), 이슈 body 검증·clone(`workspace.py`), candidate
commit·push 및 채택 판단(Stage 5)은 담당하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from agent_orchestration.executor.config import BranchCreatorInput
from agent_orchestration.github_refs import GitHubRefError


class RefClient(Protocol):
    """branch 생성에 필요한 최소 GitHub ref 연산."""

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
class BranchCreatorResult:
    """branch 생성 여부와 관찰한 원격 tip."""

    created: bool
    remote_tip: str


async def ensure_issue_branch(
    coordinates: BranchCreatorInput,
    refs: RefClient,
    token: str,
) -> BranchCreatorResult:
    """없던 branch만 봉인 SHA에 만들고 기존 ref는 변경하지 않는다."""
    ref = f"heads/{coordinates.issue_branch}"
    existing_sha = await refs.get_sha(coordinates.github_repository, ref, token)
    if existing_sha is not None:
        return BranchCreatorResult(created=False, remote_tip=existing_sha)

    try:
        created_sha = await refs.create(
            coordinates.github_repository,
            ref,
            coordinates.base_dev_sha,
            token,
        )
    except GitHubRefError as error:
        if error.status_code != 422:
            raise
        raced_sha = await refs.get_sha(coordinates.github_repository, ref, token)
        if raced_sha is not None:
            return BranchCreatorResult(created=False, remote_tip=raced_sha)
        raise
    return BranchCreatorResult(created=True, remote_tip=created_sha)
