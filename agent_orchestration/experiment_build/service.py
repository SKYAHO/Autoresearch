"""실험 candidate 런타임 판정 규칙.

[파이프라인] ②candidate Job을 만들기 직전 — 실험 이미지 워크플로우를 필요하면
실행시키고, 그 결과로부터 쓸 이미지 참조와 코드 아카이브 SHA를 확정하는 구간을
담당한다.

[기능] 두 SHA를 검증하고, 같은 candidate에 대한 기존 run을 먼저 찾아 중복 dispatch를
막으며, run과 build job의 conclusion 조합을 준비 상태로 옮긴다.

[비책임] 의존성 diff 판단·코드 아카이브 업로드·이미지 빌드(`experiment-image.yml`),
③baseline 파드의 이미지 결정, `BUILD_FAILED`의 Experiment 상태 매핑(호출자)은 담당하지
않는다.
"""

from __future__ import annotations

import re

from agent_orchestration.experiment_build.config import ExperimentBuildSettings
from agent_orchestration.experiment_build.contracts import (
    CandidateRuntime,
    ExperimentBuildError,
    ImageBuildState,
)
from agent_orchestration.experiment_build.workflows import WorkflowRunClient


DECIDE_JOB_NAME = "decide"
BUILD_JOB_NAME = "build-experiment-feast-image"
RUN_NAME_PREFIX = "experiment-image "

_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_TERMINAL_RUN_STATUS = "completed"
_SUCCESS = "success"
_SKIPPED = "skipped"


def run_display_title(candidate_sha: str) -> str:
    """워크플로우 `run-name`과 같은 문자열을 만든다."""
    return f"{RUN_NAME_PREFIX}{candidate_sha}"


def _validate_sha(name: str, value: str) -> None:
    """40자 소문자 hex가 아니면 호출 전에 거부한다."""
    if _SHA_PATTERN.fullmatch(value) is None:
        raise ValueError(f"invalid {name}: must be a 40-character lowercase sha")


async def resolve_candidate_runtime(
    candidate_sha: str,
    base_dev_sha: str,
    *,
    workflows: WorkflowRunClient,
    settings: ExperimentBuildSettings,
    token: str,
) -> CandidateRuntime:
    """②candidate 파드가 쓸 이미지 참조와 코드 아카이브 SHA를 판정한다.

    같은 `candidate_sha`의 run이 이미 있으면 다시 dispatch하지 않는다. run이 성공했을
    때 이미지를 실제로 구웠는지는 build job의 conclusion(`skipped` 대 `success`)으로
    읽으므로 레지스트리 조회 권한이 필요 없다.
    """
    _validate_sha("candidate_sha", candidate_sha)
    _validate_sha("base_dev_sha", base_dev_sha)

    run = await workflows.find_run(
        repository=settings.github_repository,
        workflow_file=settings.workflow_file,
        display_title=run_display_title(candidate_sha),
        token=token,
    )
    if run is None:
        await workflows.dispatch(
            repository=settings.github_repository,
            workflow_file=settings.workflow_file,
            ref=settings.workflow_ref,
            inputs={
                "base_dev_sha": base_dev_sha,
                "candidate_sha": candidate_sha,
            },
            token=token,
        )
        return CandidateRuntime(state=ImageBuildState.BUILD_PENDING)

    if run.status != _TERMINAL_RUN_STATUS:
        return CandidateRuntime(state=ImageBuildState.BUILD_PENDING)
    if run.conclusion != _SUCCESS:
        return CandidateRuntime(state=ImageBuildState.BUILD_FAILED)

    conclusion = await workflows.job_conclusion(
        repository=settings.github_repository,
        run_id=run.run_id,
        job_name=BUILD_JOB_NAME,
        token=token,
    )
    if conclusion == _SKIPPED:
        image_ref = settings.dev_feast_image
    elif conclusion == _SUCCESS:
        image_ref = f"{settings.feast_image_uri}:exp-{candidate_sha}"
    else:
        raise ExperimentBuildError(
            f"run {run.run_id} succeeded but {BUILD_JOB_NAME} conclusion is {conclusion}"
        )
    return CandidateRuntime(
        state=ImageBuildState.READY,
        image_ref=image_ref,
        code_archive_sha=candidate_sha,
    )
