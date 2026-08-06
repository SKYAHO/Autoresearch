"""실험 candidate 파드의 이미지·코드 참조 결정 경계.

[파이프라인] ①이 만든 candidate 코드가 exp 브랜치에 올라간 뒤 ②candidate Job을
생성하기 직전 — 의존성 diff에 따라 실험 전용 이미지를 굽거나 기존 dev 이미지를
재사용하기로 결정하는 구간을 담당한다.

[기능] 결정 결과 타입, 환경 설정, GitHub Actions run·job 조회 경계와 판정 규칙을
하나의 인터페이스로 묶어 제공한다.

[비책임] ③baseline 파드의 이미지 결정(항상 고정된 dev 이미지 + `base_dev_sha`),
②③④ Job manifest 조립과 Experiment 상태 머신 전이(호출자), GitHub 토큰 발급
(`github_app`)은 담당하지 않는다.
"""

from __future__ import annotations

from agent_orchestration.experiment_build.config import (
    ExperimentBuildConfigError,
    ExperimentBuildSettings,
)
from agent_orchestration.experiment_build.contracts import (
    CandidateRuntime,
    ExperimentBuildError,
    ImageBuildState,
)
from agent_orchestration.experiment_build.service import (
    BUILD_JOB_NAME,
    DECIDE_JOB_NAME,
    resolve_candidate_runtime,
    run_display_title,
)
from agent_orchestration.experiment_build.workflows import (
    WorkflowRun,
    WorkflowRunClient,
)


__all__ = [
    "BUILD_JOB_NAME",
    "CandidateRuntime",
    "DECIDE_JOB_NAME",
    "ExperimentBuildConfigError",
    "ExperimentBuildError",
    "ExperimentBuildSettings",
    "ImageBuildState",
    "WorkflowRun",
    "WorkflowRunClient",
    "resolve_candidate_runtime",
    "run_display_title",
]
