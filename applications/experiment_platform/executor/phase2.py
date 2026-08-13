"""Phase 2 executor container 간 workspace state 실행 경계.

[파이프라인] launcher가 8-container Kubernetes Job을 조립한 뒤 workspace-preparer가
봉인 이슈 checkout을 만들고 Codex·verifier·finalizer가 하나의 candidate를 수렴시키는
구간을 담당한다.

[기능] 각 container의 환경 입력을 기존 Stage 2~5 공개 인터페이스로 변환하고,
base tip의 Codex 실행 또는 기존 candidate 채택 검증을 선택해 VerificationResult를
finalizer에 전달한다. candidate 학습이 끝나면 두 조건을 채점하고, 그 결과와 candidate
diff를 Codex에 다시 넘겨 `report.md`를 받은 뒤, 지표와 리포트를 함께 GCS에 게시하고
요약과 리포트 본문을 Experiment API에 보고해 실험을 완주로 확정한다. stage 시작·종료와
정제된 실패 사유를 container 로그로 남기고, Codex를 실행하는 두 stage에 한해 원문 출력
tail도 함께 남긴다(#612). 그 두 stage는 토큰 사용량을 input·cached·output으로 나눈
요약 한 줄도 남긴다(#742) — `--json` 이후 원문 tail은 JSONL이라 사람이 훑기 어렵고,
원가 집계가 읽는 값이 그 한 줄이다.

[비책임] Job·Secret·PVC manifest(`launcher.jobs`), GitHub App token 발급
(`token_minter.py`), candidate API의 DB 상태 전이(`app/experiments/service.py`)는
담당하지 않는다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import asdict, dataclass
import json
import logging
import os
from pathlib import Path
import re
import sys
import uuid

from applications.experiment_platform.executor.codex_worker import (
    CodexRunResult,
    CodexTokenUsage,
    CodexWorkerError,
    run_codex_for_workspace,
)
from applications.experiment_platform.executor.config import ISSUE_BRANCH_PATTERN
from applications.experiment_platform.executor.finalizer import FinalizeInput, finalize_candidate
from applications.experiment_platform.executor.github_issues import GitHubIssues
from applications.experiment_platform.executor.prompt import ResourceBudget
from applications.experiment_platform.executor.api_client import report_result
from applications.experiment_platform.executor.measurement import (
    MeasurementInput,
    build_experiment_metrics,
    build_metric_snapshot,
    write_experiment_metrics,
)
from applications.experiment_platform.executor.report import (
    REPORT_FILENAME,
    ReportInput,
    read_report_markdown,
    write_experiment_report,
)
from applications.experiment_platform.executor.results_store import (
    collect_publishable_files,
    publish_results,
)
from applications.experiment_platform.executor.state import ExecutorWorkspaceState, read_state
from applications.experiment_platform.executor.training import (
    TrainingError,
    TrainingInput,
    TrainingStage,
    dependencies_changed,
    ensure_dataset,
    expected_dataset_sha256,
    feature_definitions_changed,
    run_training,
    sync_dependencies,
)
from applications.experiment_platform.executor.verifier import (
    CandidatePolicy,
    VerificationResult,
    verify_candidate,
)
from applications.experiment_platform.executor.workspace import (
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
# 채점 결과를 두는 clone 밖 디렉터리. 게시가 같은 container에서 일어나므로 volume
# 핸드오프가 필요 없다.
_RESULT_DIRNAME = "result"
_METRICS_FILENAME = "metrics.json"
# 내려받은 스냅샷 CSV의 위치. 같은 이유로 clone 밖이며, baseline·candidate 두 단계가
# 이 한 파일을 공유한다(#605).
_TRAINING_DATASET_DIRNAME = "training-dataset"


@dataclass(frozen=True)
class _ResultPayload:
    """API에 보고할 지표 요약과 리포트 본문이다.

    둘을 함께 돌려주는 이유는 리포트가 이 함수 안에서만 만들어지고 보고는 바깥에서
    일어나기 때문이다. 본문이 `None`이면 리포트 없이 보고한다.
    """

    snapshot: dict[str, object]
    report_markdown: str | None


class Phase2ExecutorError(RuntimeError):
    """container 경계 입력 또는 handoff가 계약에 맞지 않는다."""


def _safe_failure_reason(error: Exception) -> str:
    """executor 도메인 예외의 제한된 고정 사유 코드만 기록한다."""
    error_type = type(error)
    is_executor_domain_error = isinstance(error, Phase2ExecutorError) or (
        error_type.__module__.startswith("applications.experiment_platform.executor")
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


def _optional_positive_int(name: str) -> int | None:
    """양의 정수 환경 변수를 읽되, 없거나 해석할 수 없으면 `None`을 반환한다.

    자원 예산은 **없어도 실행이 계속돼야 한다.** 예산 환경을 붙이지 않은 배포에서
    필수로 읽으면 기존 경로가 통째로 죽는다. 값이 이상할 때 추측해서 쓰지 않고
    `None`으로 떨어뜨리는 이유도 같다 — 틀린 예산을 알리는 것보다 침묵이 낫다.
    """
    value = os.environ.get(name)
    if value is None or not value.strip().isdecimal():
        return None
    parsed = int(value.strip())
    return parsed if parsed > 0 else None


def _resource_budget() -> ResourceBudget:
    """codex-worker에 주어진 실제 자원 상한을 환경에서 읽는다.

    메모리와 CPU는 launcher가 Job resource limit에서 계산해 literal env로 넣은 값이고,
    시간은 학습 opt-in일 때만 붙는다(`jobs._resource_budget_environment`). admission이
    `env[].valueFrom`을 금지하므로 Downward API는 사용하지 않는다.

    시간 변수 이름이 `ORCH_TRAINING_TIMEOUT_SEC`이 아닌 이유는 그것이 학습 container가
    집행하는 값이고 학습 좌표와 함께 그쪽에만 가기 때문이다(#605). 여기서 읽는 것은 같은
    숫자의 고지본이다.
    """
    return ResourceBudget(
        memory_request_bytes=_optional_positive_int(
            "ORCH_CONTAINER_MEMORY_REQUEST_BYTES"
        ),
        memory_limit_bytes=_optional_positive_int("ORCH_CONTAINER_MEMORY_LIMIT_BYTES"),
        cpu_request_millicores=_optional_positive_int(
            "ORCH_CONTAINER_CPU_REQUEST_MILLICORES"
        ),
        cpu_limit_millicores=_optional_positive_int(
            "ORCH_CONTAINER_CPU_LIMIT_MILLICORES"
        ),
        training_timeout_seconds=_optional_positive_int(
            "ORCH_BUDGET_TRAINING_TIMEOUT_SEC"
        ),
    )


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


def _run_training_if_enabled(
    stage: TrainingStage, workspace: Path
) -> tuple[int, ...]:
    """데이터셋 URI가 지정된 경우에만 해당 조건의 학습을 실행하고 쓴 seed를 돌려준다.

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
        return ()
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
    return run_training(
        TrainingInput(
            stage=stage,
            workspace=workspace,
            dataset_path=dataset_path,
            output_root=workspace_root / _TRAINING_OUTPUT_DIRNAME,
            state_directory=_STATE_DIRECTORY,
            timeout_seconds=_positive_int("ORCH_TRAINING_TIMEOUT_SEC"),
        )
    )


def _log_codex_output(stdout: str, stderr: str, *, stage: str = "codex-worker") -> None:
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
                "codex output stage=%s stream=%s bytes=%d\n%s",
                stage,
                stream,
                len(text.encode("utf-8")),
                text,
            )
        else:
            _LOGGER.info(
                "codex output stage=%s stream=%s bytes=0", stage, stream
            )


def _log_codex_usage(usage: CodexTokenUsage | None, *, stage: str) -> None:
    """Codex 토큰 사용량을 사람이 읽고 기계가 파싱할 수 있는 한 줄로 남긴다(#742).

    `--json` 이후 원문 tail은 JSONL이라 사람이 훑기 어렵다. 원가 집계가 필요로 하는
    값은 이 한 줄이 전부이므로, 로그를 뒤지지 않아도 되게 고정 형식으로 따로 남긴다.

    사용량을 모를 때도 한 줄을 남긴다 — "0 토큰"과 "이벤트를 못 봤다"를 구분해야
    집계가 표본을 잘못 세지 않는다.
    """
    if usage is None:
        _LOGGER.info("codex token usage stage=%s available=0", stage)
        return
    _LOGGER.info(
        "codex token usage stage=%s available=1 turns=%d input=%d cached_input=%d "
        "fresh_input=%d output=%d reasoning=%d total=%d",
        stage,
        usage.turns,
        usage.input_tokens,
        usage.cached_input_tokens,
        usage.fresh_input_tokens,
        usage.output_tokens,
        usage.reasoning_output_tokens,
        usage.total_tokens,
    )


def codex_worker_main() -> int:
    """read-only state와 CODEX_HOME으로 base tip Codex 실행을 수행한다."""
    try:
        result: CodexRunResult = run_codex_for_workspace(
            _state(),
            codex_home=Path(_required("ORCH_CODEX_HOME")),
            timeout_seconds=_positive_int("ORCH_CODEX_TIMEOUT_SEC"),
            budget=_resource_budget(),
        )
    except CodexWorkerError as error:
        # timeout·child leak처럼 결과가 없는 경로가 오히려 원문이 가장 필요한 곳이다.
        _log_codex_output(error.stdout, error.stderr)
        _log_codex_usage(error.usage, stage="codex-worker")
        raise
    _log_codex_output(result.stdout, result.stderr)
    _log_codex_usage(result.usage, stage="codex-worker")
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


def _write_report_if_enabled(
    workspace: Path,
    *,
    metrics_path: Path,
    issue_body: str,
    base_dev_sha: str,
    candidate_sha: str,
) -> Path | None:
    """실험을 수행한 에이전트에게 자기 결과를 서술하게 하고 그 파일을 돌려준다.

    **어떤 실패도 위로 올리지 않는다.** 리포트가 없다고 게시와 API 보고까지 잃으면
    측정한 숫자마저 사라진다 — 리포트는 실험의 최종 산출물이지만 숫자보다 뒤에 오고,
    둘 중 하나만 남길 수 있다면 숫자다. 그래서 실패는 로그로만 남긴다.

    `ORCH_CODEX_HOME`이 없으면 리포트를 켜지 않은 배포다. 조용히 건너뛰지 않고 한 줄을
    남겨 "설정하지 않았다"와 "Codex가 실패했다"를 구분한다.

    Returns:
        게시할 `report.md` 경로. 쓰지 못했으면 `None`이다.
    """
    codex_home = os.environ.get("ORCH_CODEX_HOME", "").strip()
    if not codex_home:
        _LOGGER.warning(
            "experiment report was not written reason=codex_home_unset"
        )
        return None
    try:
        result = write_experiment_report(
            ReportInput(
                repository=workspace,
                metrics_path=metrics_path,
                issue_body=issue_body,
                base_dev_sha=base_dev_sha,
                candidate_sha=candidate_sha,
                codex_home=Path(codex_home),
                timeout_seconds=_positive_int("ORCH_CODEX_TIMEOUT_SEC"),
            )
        )
    except CodexWorkerError as error:
        # timeout처럼 결과가 없는 경로가 오히려 원문이 가장 필요한 곳이다(#612).
        _log_codex_output(error.stdout, error.stderr, stage="experiment-report")
        _log_codex_usage(error.usage, stage="experiment-report")
        _LOGGER.error(
            "experiment report failed reason=%s", _safe_failure_reason(error)
        )
        return None
    except Exception as error:  # noqa: BLE001 - 리포트 실패가 지표 게시를 막으면 안 된다
        _LOGGER.error(
            "experiment report failed error_type=%s reason=%s",
            type(error).__name__,
            _safe_failure_reason(error),
        )
        return None
    _log_codex_output(
        result.codex.stdout, result.codex.stderr, stage="experiment-report"
    )
    _log_codex_usage(result.codex.usage, stage="experiment-report")
    if result.path is None:
        # `report_not_a_regular_file`은 Codex가 symlink를 남겼다는 뜻이다. 게시하면
        # 링크 대상이 그대로 올라가므로 버렸다 — 사유가 로그에 남아야 구분된다.
        _LOGGER.error(
            "experiment report failed reason=%s exit_code=%d",
            result.reason or "report_missing",
            result.codex.exit_code,
        )
        return None
    if result.missing_sections:
        # 절이 빠져도 게시한다. 형식이 어긋난 리포트가 리포트 없음보다 낫고, 무엇이
        # 빠졌는지는 이 한 줄로 남는다. 절 이름은 이 저장소가 정한 고정 목록이라
        # 외부 문자열이 아니다.
        _LOGGER.warning(
            "experiment report is missing sections count=%d sections=%s",
            len(result.missing_sections),
            ",".join(result.missing_sections),
        )
    _LOGGER.info(
        "experiment report written sections_missing=%d", len(result.missing_sections)
    )
    return result.path


def _publish_report(
    results_root: str,
    report_path: Path,
    *,
    experiment_id: uuid.UUID,
    issue_number: int,
) -> None:
    """리포트를 지표와 같은 실험 경로에 올린다.

    지표 게시와 **분리한 이유**는 순서 때문이다. 숫자를 먼저 확정하고, 리포트는 그 뒤에
    쓰이고 그 뒤에 올라간다. 여기서 실패해도 위로 올리지 않는다 — 이 시점에는 지표가
    이미 게시됐고, 남은 API 보고까지 막으면 워크벤치가 다시 빈다.
    """
    try:
        published = publish_results(
            results_root,
            {REPORT_FILENAME: report_path},
            issue_number=issue_number,
            experiment_id=str(experiment_id),
        )
    except Exception as error:  # noqa: BLE001 - 리포트 게시 실패가 API 보고를 막으면 안 된다
        _LOGGER.error(
            "experiment report was not published error_type=%s reason=%s",
            type(error).__name__,
            _safe_failure_reason(error),
        )
        return
    _LOGGER.info(
        "experiment report published uri=%s", published[REPORT_FILENAME].uri
    )


def _measure_and_publish_if_enabled(
    workspace: Path,
    *,
    seeds: tuple[int, ...],
    experiment_id: uuid.UUID,
    issue_number: int,
    issue_body: str,
    base_dev_sha: str,
    candidate_sha: str,
) -> _ResultPayload | None:
    """두 조건의 산출물을 채점하고 리포트까지 받아 Pod 밖으로 내보낸다.

    `/workspace`는 emptyDir이라 Pod TTL 후 통째로 사라진다. 여기서 내보내지 않으면
    **측정한 것이 아무것도 남지 않는다** — 실험 #619가 완주하고도 `metric_summary`가
    `null`이었던 이유다.

    seed가 없으면(학습을 켜지 않은 배포) 조용히 건너뛴다. 게시 루트가 비어 있으면
    채점만 하고 로컬에 남긴다 — 채점 실패와 게시 미설정을 구분해야 진단이 된다.

    채점 timeout은 학습 상한을 그대로 쓴다. 평가는 그 모델을 만든 학습보다 반드시
    싸므로 별도 손잡이를 만들면 아무도 조정하지 않는 설정만 늘어난다.

    Returns:
        API에 보고할 지표 요약과 리포트 본문. 채점하지 않았으면 `None`이다.
    """
    if not seeds:
        return None
    dataset_uri = os.environ.get("ORCH_TRAINING_DATASET_URI", "").strip()
    workspace_root = Path(_required("ORCH_EXECUTOR_WORKSPACE"))
    payload = build_experiment_metrics(
        MeasurementInput(
            workspace=workspace,
            output_root=workspace_root / _TRAINING_OUTPUT_DIRNAME,
            seeds=seeds,
            timeout_seconds=_positive_int("ORCH_TRAINING_TIMEOUT_SEC"),
        ),
        coordinates={
            "experiment_id": str(experiment_id),
            "issue_number": issue_number,
            "base_dev_sha": base_dev_sha,
            "candidate_sha": candidate_sha,
        },
        dataset_fingerprint=expected_dataset_sha256(dataset_uri),
    )
    metrics_path = write_experiment_metrics(
        payload, workspace_root / _RESULT_DIRNAME / _METRICS_FILENAME
    )
    results_root = os.environ.get("ORCH_EXPERIMENT_RESULTS_ROOT", "").strip()
    if not results_root:
        # 게시하지 않는 배포다. 지표는 만들었지만 Pod과 함께 사라진다는 것을 남긴다 —
        # 나중에 "왜 결과가 없나"를 로그 한 줄로 답할 수 있어야 한다. 그래도 요약은
        # 돌려준다: 게시 미설정 때문에 API 보고까지 막으면 워크벤치가 다시 빈다.
        _LOGGER.warning(
            "experiment metrics were not published reason=results_root_unset "
            "experiment_id=%s issue_number=%s",
            experiment_id,
            issue_number,
        )
        return _ResultPayload(
            snapshot=build_metric_snapshot(payload, results_uri=None),
            report_markdown=None,
        )
    # **측정 산출물을 먼저 확정한다.** 리포트를 기다렸다가 함께 올리면 Codex 실행
    # 시간(최대 `ORCH_CODEX_TIMEOUT_SEC`)만큼 숫자를 잃을 수 있는 창이 열린다 —
    # 그 사이 `activeDeadlineSeconds`나 OOM으로 container가 죽으면 push와
    # `RUNNING → EVALUATING`은 이미 끝난 뒤라 실험은 ERROR로 회수되고 측정한 숫자는
    # 어디에도 남지 않는다. 예외 경로만이 아니라 **container가 죽는 경로에서도**
    # "리포트 실패로 숫자를 잃지 않는다"가 성립해야 한다.
    #
    # 부수 효과 하나가 더 있다. 게시된 사본은 버킷 IAM이 `objectCreator`(교체 불가)라
    # write-once이므로, 뒤이어 도는 Codex가 로컬 `metrics.json`을 고치더라도 게시된
    # 숫자는 그대로다.
    published = publish_results(
        results_root,
        collect_publishable_files(
            metrics_path=metrics_path,
            training_output_root=workspace_root / _TRAINING_OUTPUT_DIRNAME,
        ),
        issue_number=issue_number,
        experiment_id=str(experiment_id),
    )
    reused = sorted(name for name, obj in published.items() if not obj.created)
    if reused:
        # write-once라 재시도가 앞선 실행의 결과를 덮지 못한다. 두 실행의 결과가
        # 다를 수 있으므로 무엇이 남았는지 드러내야 한다.
        _LOGGER.warning(
            "experiment results already existed count=%d experiment_id=%s",
            len(reused),
            experiment_id,
        )
    _LOGGER.info(
        "experiment results published count=%d uri=%s",
        len(published),
        published[_METRICS_FILENAME].uri,
    )
    report_path = _write_report_if_enabled(
        workspace,
        metrics_path=metrics_path,
        issue_body=issue_body,
        base_dev_sha=base_dev_sha,
        candidate_sha=candidate_sha,
    )
    report_markdown = None
    if report_path is not None:
        _publish_report(
            results_root,
            report_path,
            experiment_id=experiment_id,
            issue_number=issue_number,
        )
        # 읽기 실패는 `None`으로 흡수된다. 게시는 이미 끝났고 워크벤치 표시를 위해
        # 지표 보고를 잃을 이유가 없다.
        report_markdown = read_report_markdown(report_path)
        if report_markdown is None:
            _LOGGER.warning("experiment report body was not readable for the API report")
    return _ResultPayload(
        snapshot=build_metric_snapshot(
            payload, results_uri=published[_METRICS_FILENAME].uri
        ),
        report_markdown=report_markdown,
    )


def candidate_finalizer_main() -> int:
    """검증 handoff와 push/API token 파일로 candidate를 원격과 API에 수렴시킨다."""
    state = _state()
    experiment_id, issue_number, issue_branch, base_dev_sha, repository = _coordinates()
    candidate_sha = finalize_candidate(
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
    seeds = _run_training_if_enabled(TrainingStage.CANDIDATE, state.repository)
    result = _measure_and_publish_if_enabled(
        state.repository,
        seeds=seeds,
        experiment_id=experiment_id,
        issue_number=issue_number,
        issue_body=state.issue_body,
        base_dev_sha=base_dev_sha,
        candidate_sha=candidate_sha,
    )
    if result is not None:
        # 채점했으면 반드시 보고한다. 여기서 실패하면 stage가 실패해 Job이 Failed로
        # 끝나고 launcher가 실험을 ERROR로 회수한다 — GCS에는 결과가 있는데 실험은
        # 완주로 표시되지 않는 상태가 되지만, **없는 결과를 완주로 표시하는 것보다
        # 낫다.** 조용히 넘어가면 `metric_summary=null`이 다시 나온다.
        report_result(
            api_url=_required("ORCH_EXECUTOR_API_URL"),
            token_file=Path(_required("ORCH_EXECUTOR_API_TOKEN_FILE")),
            experiment_id=experiment_id,
            candidate_sha=candidate_sha,
            metric_snapshot=result.snapshot,
            report_markdown=result.report_markdown,
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
