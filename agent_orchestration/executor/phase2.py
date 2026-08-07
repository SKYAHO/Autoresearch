"""Phase 2 executor container 간 workspace state 실행 경계.

[파이프라인] launcher가 8-container Kubernetes Job을 조립한 뒤 workspace-preparer가
봉인 이슈 checkout을 만들고 Codex·verifier·finalizer가 하나의 candidate를 수렴시키는
구간을 담당한다.

[기능] 각 container의 환경 입력을 기존 Stage 2~5 공개 인터페이스로 변환하고,
base tip의 Codex 실행 또는 기존 candidate 채택 검증을 선택해 VerificationResult를
finalizer에 전달한다. stage 시작·종료와 정제된 실패 사유를 container 로그로 남긴다.

[비책임] Job·Secret·PVC manifest(`launcher.jobs`), GitHub App token 발급
(`token_minter.py`), candidate API의 DB 상태 전이(`app/experiments/service.py`)는
담당하지 않는다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import re
import sys
import uuid

from agent_orchestration.executor.codex_worker import (
    CodexRunResult,
    run_codex_for_workspace,
)
from agent_orchestration.executor.config import ISSUE_BRANCH_PATTERN
from agent_orchestration.executor.finalizer import FinalizeInput, finalize_candidate
from agent_orchestration.executor.github_issues import GitHubIssues
from agent_orchestration.executor.state import ExecutorWorkspaceState, read_state
from agent_orchestration.executor.training import (
    TrainingError,
    TrainingInput,
    TrainingStage,
    dependencies_changed,
    feature_definitions_changed,
    run_training,
    sync_dependencies,
)
from agent_orchestration.executor.verifier import (
    CandidatePolicy,
    VerificationResult,
    verify_candidate,
)
from agent_orchestration.executor.workspace import (
    WorkspacePrepareInput,
    prepare_workspace,
)


_STATE_PATH = Path("/var/run/executor-state/state.json")
_VERIFICATION_PATH = Path("/var/run/verification-result/result.json")
_LOGGER = logging.getLogger(__name__)
_SAFE_REASON_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_SAFE_ENVIRONMENT_REASON_PATTERN = re.compile(
    r"^(?:missing|invalid) ORCH_[A-Z0-9_]+$"
)
_ISSUE_NUMBER_PATTERN = re.compile(r"^[1-9][0-9]*$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
# launcher가 executor-state volume을 마운트하는 고정 경로다
# (`launcher/jobs.py`의 `_STATE_DIRECTORY`). 환경 변수로 받지 않는 이유는 verification
# 경로와 같다 — Pod 안의 마운트 지점은 manifest가 정하는 고정값이고, 주입 가능한 값으로
# 두면 계약이 두 곳으로 갈린다.
_STATE_DIRECTORY = Path("/var/run/executor-state")


class Phase2ExecutorError(RuntimeError):
    """container 경계 입력 또는 handoff가 계약에 맞지 않는다."""


def _safe_failure_reason(error: Exception) -> str:
    """executor 도메인 예외의 제한된 고정 사유 코드만 기록한다."""
    error_type = type(error)
    is_executor_domain_error = isinstance(error, Phase2ExecutorError) or (
        error_type.__module__.startswith("agent_orchestration.executor")
        and error_type.__name__.endswith("Error")
    )
    if is_executor_domain_error:
        reason = getattr(error, "reason", None)
        if reason is None and len(error.args) == 1:
            reason = error.args[0]
        if isinstance(reason, str) and (
            _SAFE_REASON_PATTERN.fullmatch(reason) is not None
            or _SAFE_ENVIRONMENT_REASON_PATTERN.fullmatch(reason) is not None
        ):
            return reason
    return "redacted"


def _safe_log_coordinates() -> tuple[str, str, str, str]:
    """봉인 좌표 형식에 맞는 환경 값만 로그 필드로 반환한다."""
    experiment_id = os.environ.get("ORCH_EXPERIMENT_ID", "")
    try:
        safe_experiment_id = str(uuid.UUID(experiment_id))
    except ValueError:
        safe_experiment_id = "unknown"

    issue_number = os.environ.get("ORCH_ISSUE_NUMBER", "")
    safe_issue_number = (
        issue_number
        if _ISSUE_NUMBER_PATTERN.fullmatch(issue_number) is not None
        else "unknown"
    )
    issue_branch = os.environ.get("ORCH_ISSUE_BRANCH", "")
    safe_issue_branch = (
        issue_branch
        if ISSUE_BRANCH_PATTERN.fullmatch(issue_branch) is not None
        else "unknown"
    )
    base_sha = os.environ.get("ORCH_BASE_DEV_SHA", "")
    safe_base_sha = (
        base_sha if _SHA_PATTERN.fullmatch(base_sha) is not None else "unknown"
    )
    return (
        safe_experiment_id,
        safe_issue_number,
        safe_issue_branch,
        safe_base_sha,
    )


def _required(name: str) -> str:
    """비어 있지 않은 container 환경 변수 하나를 반환한다."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise Phase2ExecutorError(f"missing {name}")
    return value


def _positive_int(name: str) -> int:
    """양의 정수 container 환경 변수를 반환한다."""
    value = _required(name)
    if not value.isdecimal() or int(value) < 1:
        raise Phase2ExecutorError(f"invalid {name}")
    return int(value)


def _coordinates() -> tuple[uuid.UUID, int, str, str, str]:
    """launcher가 봉인한 experiment·issue·SHA·repository 좌표를 읽는다."""
    try:
        experiment_id = uuid.UUID(_required("ORCH_EXPERIMENT_ID"))
    except ValueError as error:
        raise Phase2ExecutorError("invalid ORCH_EXPERIMENT_ID") from error
    return (
        experiment_id,
        _positive_int("ORCH_ISSUE_NUMBER"),
        _required("ORCH_ISSUE_BRANCH"),
        _required("ORCH_BASE_DEV_SHA"),
        _required("ORCH_GITHUB_REPOSITORY"),
    )


def _state() -> ExecutorWorkspaceState:
    """state volume에서 workspace-preparer가 기록한 봉인 state를 재검증해 읽는다."""
    workspace = Path(_required("ORCH_EXECUTOR_WORKSPACE"))
    return read_state(_STATE_PATH, workspace=workspace)


def workspace_preparer_main() -> int:
    """clone token과 launcher 좌표로 Stage 2 workspace-preparer를 실행한다."""
    experiment_id, issue_number, issue_branch, base_dev_sha, repository = _coordinates()
    config = WorkspacePrepareInput(
        experiment_id=experiment_id,
        issue_number=issue_number,
        issue_branch=issue_branch,
        base_dev_sha=base_dev_sha,
        issue_body_sha256=_required("ORCH_ISSUE_BODY_SHA256"),
        github_repository=repository,
        token_file=Path(_required("ORCH_GITHUB_TOKEN_FILE")),
        workspace=Path(_required("ORCH_EXECUTOR_WORKSPACE")),
    )
    asyncio.run(prepare_workspace(config, GitHubIssues()))
    # clone은 workspace 루트가 아니라 그 아래 `repository/`에 놓인다
    # (`workspace.py`의 `repository = workspace / "repository"`).
    _run_training_if_enabled(TrainingStage.BASELINE, config.workspace / "repository")
    return 0


def _run_training_if_enabled(stage: TrainingStage, workspace: Path) -> None:
    """데이터셋이 지정된 경우에만 해당 조건의 학습을 실행한다.

    `ORCH_TRAINING_DATASET_PATH` 미설정이면 조용히 건너뛴다. 학습 입력은 미리 게시된
    데이터셋 스냅샷인데, 그 스냅샷을 Pod가 읽으려면 `experiment-job` GSA에 스냅샷 root
    read 권한이 필요하다(현재 `objectCreator`만 보유). 권한이 붙기 전까지는 Phase 2의
    기존 경로(clone → Codex → verify → push)가 그대로 동작해야 하므로 opt-in으로 둔다.

    조립을 Pod 안에서 하지 않는 이유는 feast group이 executor 이미지에 없고
    `pyproject.toml`이 feast와 dev를 conflicts로 선언해 재빌드로도 넣을 수 없기 때문이다.
    """
    dataset = os.environ.get("ORCH_TRAINING_DATASET_PATH", "").strip()
    if not dataset:
        return
    if stage is TrainingStage.CANDIDATE:
        base_ref = _required("ORCH_BASE_DEV_SHA")
        # 지원 범위를 벗어난 가설이면 학습을 시작하지 않는다. 그냥 진행하면 새 피처가
        # 없는 스냅샷으로 학습돼 candidate가 baseline과 같은 결과를 내고, 그것이 실패로
        # 보이지 않아 아무도 알아채지 못한다.
        changed = feature_definitions_changed(workspace, base_ref=base_ref)
        if changed:
            # 어떤 경로가 걸렸는지는 로그로 남기고, 예외 사유는 접미사 없는 고정 코드로
            # 둔다. `_safe_failure_reason`이 `^[a-z][a-z0-9_]*$`에 맞는 값만 남기고
            # 나머지는 `redacted`로 지우므로(#583), 경로를 붙이면 사유가 통째로 사라진다.
            _LOGGER.error(
                "training rejected stage=candidate reason=feature_change_unsupported paths=%s",
                ",".join(changed),
            )
            raise TrainingError("feature_change_unsupported")
        if dependencies_changed(workspace, base_ref=base_ref):
            sync_dependencies(
                workspace, timeout_seconds=_positive_int("ORCH_UV_SYNC_TIMEOUT_SEC")
            )
    run_training(
        TrainingInput(
            stage=stage,
            workspace=workspace,
            dataset_path=Path(dataset),
            state_directory=_STATE_DIRECTORY,
            timeout_seconds=_positive_int("ORCH_TRAINING_TIMEOUT_SEC"),
        )
    )


def codex_worker_main() -> int:
    """read-only state와 CODEX_HOME으로 base tip Codex 실행을 수행한다."""
    result: CodexRunResult = run_codex_for_workspace(
        _state(),
        codex_home=Path(_required("ORCH_CODEX_HOME")),
        timeout_seconds=_positive_int("ORCH_CODEX_TIMEOUT_SEC"),
    )
    return result.exit_code


def _write_verification(result: VerificationResult) -> None:
    """verifier와 finalizer 전용 memory volume에 canonical handoff를 기록한다."""
    _VERIFICATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    _VERIFICATION_PATH.write_text(
        json.dumps(asdict(result), sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    _VERIFICATION_PATH.chmod(0o400)


def _read_verification() -> VerificationResult:
    """verifier handoff JSON의 타입을 검사해 VerificationResult로 복원한다."""
    try:
        raw = json.loads(_VERIFICATION_PATH.read_text(encoding="utf-8"))
        changed_paths = raw["changed_paths"]
        fingerprint = raw["content_fingerprint"]
        tree = raw["verified_tree_oid"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise Phase2ExecutorError("verification_result_invalid") from error
    if (
        not isinstance(changed_paths, list)
        or not all(isinstance(path, str) for path in changed_paths)
        or not isinstance(fingerprint, str)
        or not isinstance(tree, str)
    ):
        raise Phase2ExecutorError("verification_result_invalid")
    return VerificationResult(tuple(changed_paths), fingerprint, tree)


def candidate_verifier_main() -> int:
    """state tip에 맞는 working tree 또는 existing candidate diff를 검증한다."""
    state = _state()
    candidate_sha = None if state.remote_tip == state.base_dev_sha else state.remote_tip
    result = verify_candidate(
        state.repository,
        state.base_dev_sha,
        candidate_sha,
        CandidatePolicy(allowed_scope=state.allowed_scope),
    )
    _write_verification(result)
    return 0


def candidate_finalizer_main() -> int:
    """검증 handoff와 push/API token 파일로 candidate를 원격과 API에 수렴시킨다."""
    state = _state()
    experiment_id, issue_number, issue_branch, base_dev_sha, repository = _coordinates()
    finalize_candidate(
        FinalizeInput(
            experiment_id=experiment_id,
            issue_number=issue_number,
            issue_branch=issue_branch,
            base_dev_sha=base_dev_sha,
            expected_remote_tip=state.remote_tip,
            repository=state.repository,
            github_repository=repository,
            push_token_file=Path(_required("ORCH_GITHUB_TOKEN_FILE")),
            api_url=_required("ORCH_EXECUTOR_API_URL"),
            api_token_file=Path(_required("ORCH_EXECUTOR_API_TOKEN_FILE")),
        ),
        _read_verification(),
    )
    _run_training_if_enabled(TrainingStage.CANDIDATE, state.repository)
    return 0


def main(argv: list[str] | None = None) -> int:
    """container command의 고정 stage 이름 하나를 실행한다."""
    selected = argv if argv is not None else sys.argv[1:]
    commands: dict[str, Callable[[], int]] = {
        "workspace-preparer": workspace_preparer_main,
        "codex-worker": codex_worker_main,
        "candidate-verifier": candidate_verifier_main,
        "candidate-finalizer": candidate_finalizer_main,
    }
    coordinates = _safe_log_coordinates()
    if len(selected) != 1 or selected[0] not in commands:
        _LOGGER.error(
            "phase2 stage selection failed reason=invalid_stage_argument "
            "experiment_id=%s issue_number=%s branch=%s base_sha=%s",
            *coordinates,
        )
        return 1
    stage = selected[0]
    _LOGGER.info(
        "phase2 stage started stage=%s "
        "experiment_id=%s issue_number=%s branch=%s base_sha=%s",
        stage,
        *coordinates,
    )
    try:
        exit_code = commands[stage]()
    except Exception as error:
        _LOGGER.error(
            "phase2 stage failed stage=%s error_type=%s reason=%s "
            "experiment_id=%s issue_number=%s branch=%s base_sha=%s",
            stage,
            type(error).__name__,
            _safe_failure_reason(error),
            *coordinates,
        )
        return 1
    if exit_code != 0:
        _LOGGER.error(
            "phase2 stage failed stage=%s error_type=StageExitCode "
            "reason=nonzero_exit exit_code=%d "
            "experiment_id=%s issue_number=%s branch=%s base_sha=%s",
            stage,
            exit_code,
            *coordinates,
        )
        return exit_code
    _LOGGER.info(
        "phase2 stage finished stage=%s exit_code=%d "
        "experiment_id=%s issue_number=%s branch=%s base_sha=%s",
        stage,
        exit_code,
        *coordinates,
    )
    return exit_code


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
