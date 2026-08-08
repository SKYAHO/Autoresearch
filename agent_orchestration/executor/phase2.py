"""Phase 2 executor container 간 workspace state 실행 경계.

[파이프라인] launcher가 8-container Kubernetes Job을 조립한 뒤 workspace-preparer가
봉인 이슈 checkout을 만들고 Codex·verifier·finalizer가 하나의 candidate를 수렴시키는
구간을 담당한다.

[기능] 각 container의 환경 입력을 기존 Stage 2~5 공개 인터페이스로 변환하고,
base tip의 Codex 실행 또는 기존 candidate 채택 검증을 선택해 VerificationResult를
finalizer에 전달한다. stage 시작·종료와 정제된 실패 사유를 container 로그로 남기고,
codex-worker에 한해 Codex 원문 출력 tail도 함께 남긴다(#612).

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
    CodexWorkerError,
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
    ensure_dataset,
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
# 학습 산출물 루트. clone(`<workspace>/repository`)의 형제로 두어 verifier의 git 수집
# 범위 밖에 놓는다(#603).
_TRAINING_OUTPUT_DIRNAME = "training-output"
# 내려받은 스냅샷 CSV의 위치. 같은 이유로 clone 밖이며, baseline·candidate 두 단계가
# 이 한 파일을 공유한다(#605).
_TRAINING_DATASET_DIRNAME = "training-dataset"


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
    """데이터셋 URI가 지정된 경우에만 해당 조건의 학습을 실행한다.

    `ORCH_TRAINING_DATASET_URI` 미설정이면 조용히 건너뛴다 — 학습을 켜지 않은 배포에서
    기존 경로(clone → Codex → verify → push)가 그대로 동작해야 하므로 opt-in으로 둔다.

    조립을 Pod 안에서 하지 않는 이유는 feast group이 executor 이미지에 없고
    `pyproject.toml`이 feast와 dev를 conflicts로 선언해 재빌드로도 넣을 수 없기 때문이다.
    그래서 조립은 밖에서 끝내고 Pod은 게시된 스냅파일을 내려받아 읽기만 한다.

    두 조건이 **같은 파일**을 쓰는 것이 paired 대조의 전제다. `ensure_dataset`이 이미
    받아둔 파일을 재사용하고 해시를 대조하므로, 조건별 재다운로드도 조건별 데이터
    차이도 생기지 않는다(#605).
    """
    dataset_uri = os.environ.get("ORCH_TRAINING_DATASET_URI", "").strip()
    if not dataset_uri:
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
    # clone(`workspace`)의 형제 디렉터리들이다 — 같은 volume이라 두 조건이 공유하고,
    # clone 밖이라 verifier가 Codex의 변경으로 수집하지 않는다(#603).
    workspace_root = Path(_required("ORCH_EXECUTOR_WORKSPACE"))
    dataset_path = ensure_dataset(
        dataset_uri=dataset_uri,
        destination_dir=workspace_root / _TRAINING_DATASET_DIRNAME,
        workspace=workspace,
        timeout_seconds=_positive_int("ORCH_TRAINING_DOWNLOAD_TIMEOUT_SEC"),
    )
    run_training(
        TrainingInput(
            stage=stage,
            workspace=workspace,
            dataset_path=dataset_path,
            output_root=workspace_root / _TRAINING_OUTPUT_DIRNAME,
            state_directory=_STATE_DIRECTORY,
            timeout_seconds=_positive_int("ORCH_TRAINING_TIMEOUT_SEC"),
        )
    )


def _log_codex_output(stdout: str, stderr: str) -> None:
    """Codex 원문 출력 tail을 stage 로그로 남긴다.

    이 모듈은 원칙적으로 정제된 사유 코드만 로그로 내보내지만(`_safe_failure_reason`),
    Codex 출력만은 원문 그대로 남긴다(#612). Codex는 sandbox 실패나 작업 거절을 exit 0으로
    보고하므로 종료 코드만으로는 성공과 구분되지 않는다. 성공·실패를 가리지 않고 남기는
    이유도 같다 — 진단이 필요한 실패가 바로 그 "겉보기 성공" 쪽이다.

    비어 있어도 한 줄을 남겨 "출력이 없었다"와 "로깅이 깨졌다"를 구분한다.
    """
    for stream, text in (("stdout", stdout), ("stderr", stderr)):
        if text:
            _LOGGER.info(
                "codex output stage=codex-worker stream=%s bytes=%d\n%s",
                stream,
                len(text.encode("utf-8")),
                text,
            )
        else:
            _LOGGER.info(
                "codex output stage=codex-worker stream=%s bytes=0", stream
            )


def codex_worker_main() -> int:
    """read-only state와 CODEX_HOME으로 base tip Codex 실행을 수행한다."""
    try:
        result: CodexRunResult = run_codex_for_workspace(
            _state(),
            codex_home=Path(_required("ORCH_CODEX_HOME")),
            timeout_seconds=_positive_int("ORCH_CODEX_TIMEOUT_SEC"),
        )
    except CodexWorkerError as error:
        # timeout·child leak처럼 결과가 없는 경로가 오히려 원문이 가장 필요한 곳이다.
        _log_codex_output(error.stdout, error.stderr)
        raise
    _log_codex_output(result.stdout, result.stderr)
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
        # pytest 관측치는 없어도 handoff가 성립한다 — 차단 사유가 아니기 때문이다(#615).
        # 그래도 그대로 실어 나른다. 기본값으로 되돌리면 "pytest가 통과했다"는 거짓이 된다.
        pytest_exit_code = raw.get("pytest_exit_code", 0)
        pytest_output = raw.get("pytest_output", "")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        raise Phase2ExecutorError("verification_result_invalid") from error
    if (
        not isinstance(changed_paths, list)
        or not all(isinstance(path, str) for path in changed_paths)
        or not isinstance(fingerprint, str)
        or not isinstance(tree, str)
        or not isinstance(pytest_exit_code, int)
        or isinstance(pytest_exit_code, bool)
        or not isinstance(pytest_output, str)
    ):
        raise Phase2ExecutorError("verification_result_invalid")
    return VerificationResult(
        tuple(changed_paths), fingerprint, tree, pytest_exit_code, pytest_output
    )


def _log_pytest_observation(result: VerificationResult) -> None:
    """비차단 pytest의 결과를 stage 로그로 남긴다.

    pytest는 candidate를 거부하지 않으므로(#615) 이 로그가 유일한 관측 수단이다. 실패
    여부와 무관하게 한 줄을 남겨 "통과했다"와 "실행되지 않았다"를 구분한다. 실패했을
    때만 출력 tail을 붙인다 — 통과한 pytest의 출력은 진단 가치가 없고 64 KiB를 차지한다.
    """
    if result.pytest_exit_code == 0:
        _LOGGER.info(
            "pytest observation stage=candidate-verifier blocking=false exit_code=0"
        )
        return
    _LOGGER.warning(
        "pytest observation stage=candidate-verifier blocking=false exit_code=%d "
        "bytes=%d\n%s",
        result.pytest_exit_code,
        len(result.pytest_output.encode("utf-8")),
        result.pytest_output,
    )


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
    _log_pytest_observation(result)
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
