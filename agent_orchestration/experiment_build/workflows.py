"""GitHub Actions workflow run·job 조회와 dispatch REST 경계.

[파이프라인] 실험 이미지 워크플로우를 실행시키고 그 run과 job의 종료 상태를 읽어오는
구간을 담당한다.

[기능] run 식별에 필요한 값 타입과, 한 판정에 필요한 연산 3개(run 조회, dispatch,
job conclusion 조회)를 프로토콜로 정의한다. REST 구현은 목록 응답에서 `run-name`이
정확히 일치하는 가장 최근 run만 고른다.

[비책임] installation token 발급(`github_app`), 상태 판정 규칙(`service`), 워크플로우
자체의 동작은 담당하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


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


_GITHUB_API_URL = "https://api.github.com"
_API_VERSION = "2022-11-28"
_REQUEST_TIMEOUT_SEC = 30
_RUNS_PER_PAGE = 100


class WorkflowRunError(RuntimeError):
    """workflow run REST 호출이 실패했거나 응답을 신뢰할 수 없다."""

    def __init__(self, reason: str, *, status_code: int | None = None) -> None:
        self.reason = reason
        self.status_code = status_code
        suffix = f" (status={status_code})" if status_code is not None else ""
        super().__init__(f"{reason}{suffix}")


class GitHubWorkflowRuns:
    """installation token으로 GitHub Actions workflow run API를 호출한다."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def _request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        params: dict[str, str | int] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> httpx.Response:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        try:
            async with httpx.AsyncClient(
                base_url=_GITHUB_API_URL,
                headers=headers,
                timeout=_REQUEST_TIMEOUT_SEC,
                transport=self._transport,
            ) as client:
                return await client.request(
                    method, path, params=params, json=json_body
                )
        except httpx.HTTPError as error:
            raise WorkflowRunError("request_failed") from error

    async def find_run(
        self,
        *,
        repository: str,
        workflow_file: str,
        display_title: str,
        token: str,
    ) -> WorkflowRun | None:
        """`run-name`이 정확히 일치하는 run 중 가장 최근 것을 반환한다."""
        response = await self._request(
            "GET",
            f"/repos/{repository}/actions/workflows/{workflow_file}/runs",
            token,
            params={"event": "workflow_dispatch", "per_page": _RUNS_PER_PAGE},
        )
        if response.status_code != 200:
            raise WorkflowRunError("list_failed", status_code=response.status_code)
        try:
            payload = response.json()
        except ValueError as error:
            raise WorkflowRunError("invalid_response") from error
        runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
        if not isinstance(runs, list):
            raise WorkflowRunError("invalid_response")
        matched = [
            run
            for run in runs
            if isinstance(run, dict) and run.get("display_title") == display_title
        ]
        if not matched:
            return None
        newest = max(matched, key=lambda run: str(run.get("created_at", "")))
        run_id = newest.get("id")
        status = newest.get("status")
        if not isinstance(run_id, int) or not isinstance(status, str):
            raise WorkflowRunError("invalid_response")
        conclusion = newest.get("conclusion")
        return WorkflowRun(
            run_id=run_id,
            status=status,
            conclusion=conclusion if isinstance(conclusion, str) else None,
        )

    async def dispatch(
        self,
        *,
        repository: str,
        workflow_file: str,
        ref: str,
        inputs: dict[str, str],
        token: str,
    ) -> None:
        """워크플로우를 실행시키고 204 외의 응답을 실패로 본다."""
        response = await self._request(
            "POST",
            f"/repos/{repository}/actions/workflows/{workflow_file}/dispatches",
            token,
            json_body={"ref": ref, "inputs": dict(inputs)},
        )
        if response.status_code != 204:
            raise WorkflowRunError("dispatch_failed", status_code=response.status_code)

    async def job_conclusion(
        self,
        *,
        repository: str,
        run_id: int,
        job_name: str,
        token: str,
    ) -> str | None:
        """run의 job 중 이름이 일치하는 것의 conclusion을 반환한다."""
        response = await self._request(
            "GET",
            f"/repos/{repository}/actions/runs/{run_id}/jobs",
            token,
            params={"per_page": _RUNS_PER_PAGE},
        )
        if response.status_code != 200:
            raise WorkflowRunError("jobs_failed", status_code=response.status_code)
        try:
            payload = response.json()
        except ValueError as error:
            raise WorkflowRunError("invalid_response") from error
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            raise WorkflowRunError("invalid_response")
        for job in jobs:
            if isinstance(job, dict) and job.get("name") == job_name:
                conclusion = job.get("conclusion")
                return conclusion if isinstance(conclusion, str) else None
        return None
