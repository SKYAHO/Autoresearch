"""GitHub Actions workflow run·job 조회와 dispatch REST 경계.

[파이프라인] 실험 이미지 워크플로우를 실행시키고 그 run과 job의 종료 상태를 읽어오는
구간을 담당한다.

[기능] run 식별에 필요한 값 타입과, 한 판정에 필요한 연산 3개(run 조회, dispatch,
job conclusion 조회)를 프로토콜로 정의한다.

[비책임] installation token 발급(`github_app`), 상태 판정 규칙(`service`), 워크플로우
자체의 동작은 담당하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class WorkflowRun:
    """실험 이미지 워크플로우 run 한 건의 종료 판단에 필요한 최소 상태."""

    run_id: int
    status: str
    conclusion: str | None


class WorkflowRunClient(Protocol):
    """한 판정에 필요한 GitHub Actions 연산."""

    async def find_run(
        self,
        *,
        repository: str,
        workflow_file: str,
        display_title: str,
        token: str,
    ) -> WorkflowRun | None: ...

    async def dispatch(
        self,
        *,
        repository: str,
        workflow_file: str,
        ref: str,
        inputs: dict[str, str],
        token: str,
    ) -> None: ...

    async def job_conclusion(
        self,
        *,
        repository: str,
        run_id: int,
        job_name: str,
        token: str,
    ) -> str | None: ...
