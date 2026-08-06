"""Phase 2 executor container 간 workspace state 실행 경계.

[파이프라인] launcher가 8-container Kubernetes Job을 조립한 뒤 workspace-preparer가
봉인 이슈 checkout을 만들고 Codex·verifier·finalizer가 하나의 candidate를 수렴시키는
구간을 담당한다.

[기능] 각 container의 환경 입력을 기존 Stage 2~5 공개 인터페이스로 변환하고,
base tip의 Codex 실행 또는 기존 candidate 채택 검증을 선택해 VerificationResult를
finalizer에 전달한다.

[비책임] Job·Secret·PVC manifest(`launcher.jobs`), GitHub App token 발급
(`token_minter.py`), candidate API의 DB 상태 전이(`app/experiments/service.py`)는
담당하지 않는다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import uuid

from agent_orchestration.executor.codex_worker import (
    CodexRunResult,
    run_codex_for_workspace,
)
from agent_orchestration.executor.finalizer import FinalizeInput, finalize_candidate
from agent_orchestration.executor.github_issues import GitHubIssues
from agent_orchestration.executor.state import ExecutorWorkspaceState, read_state
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


class Phase2ExecutorError(RuntimeError):
    """container 경계 입력 또는 handoff가 계약에 맞지 않는다."""


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
    return 0


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
    if len(selected) != 1 or selected[0] not in commands:
        return 1
    try:
        return commands[selected[0]]()
    except (Phase2ExecutorError, OSError, RuntimeError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
