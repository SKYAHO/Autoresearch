"""Phase 2 executor Job의 조립·재시도·종료 회수 경계를 검증한다.

전체 파이프라인에서 launcher가 DB에 봉인된 실험을 8-container executor Job으로
전달한 뒤, 최종 Kubernetes 실패를 Experiment ERROR로 회수하는 구간을 검증한다.
실제 GitHub·Kubernetes·Codex 호출은 하지 않고 manifest와 repository 경계를 관찰한다.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker

from agent_orchestration.app.database import Base
from agent_orchestration.app.experiments.models import (
    Experiment,
    ExperimentEvent,
    ExperimentStatus,
)
from agent_orchestration.executor.results_store import PublishedObject
from agent_orchestration.launcher.config import LauncherSettings
from agent_orchestration.launcher.jobs import (
    EXPERIMENT_EXECUTOR_LABEL_SELECTOR,
    KubernetesJobs,
    build_executor_job,
)
from agent_orchestration.launcher.main import run_tick
from agent_orchestration.launcher.repository import (
    ClaimedExperiment,
    claim_experiments,
    reconcile_failed_jobs,
)
from agent_orchestration.executor import phase2
from agent_orchestration.executor.codex_worker import CodexRunResult, CodexWorkerError
from agent_orchestration.executor.state import ExecutorWorkspaceState, write_state
from agent_orchestration.executor.verifier import VerificationResult
from agent_orchestration.executor.workspace import PreparedWorkspace


_EXPERIMENT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
_BODY = "<!-- experiment-id: 12345678-1234-5678-1234-567812345678 -->\nbody"


class UnsafeExecutorError(RuntimeError):
    """향후 executor 도메인 예외가 비정제 문자열을 담는 경우를 재현한다."""


UnsafeExecutorError.__module__ = "agent_orchestration.executor.future"


def test_phase2_main_logs_stage_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(phase2, "workspace_preparer_main", lambda: 0)

    with caplog.at_level(logging.INFO, logger=phase2.__name__):
        exit_code = phase2.main(["workspace-preparer"])

    assert exit_code == 0
    assert "phase2 stage started stage=workspace-preparer" in caplog.text
    assert "phase2 stage finished stage=workspace-preparer exit_code=0" in caplog.text


def test_phase2_main_logs_nonzero_stage_exit_as_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(phase2, "codex_worker_main", lambda: 17)

    with caplog.at_level(logging.INFO, logger=phase2.__name__):
        exit_code = phase2.main(["codex-worker"])

    assert exit_code == 17
    assert (
        "phase2 stage failed stage=codex-worker error_type=StageExitCode "
        "reason=nonzero_exit exit_code=17"
    ) in caplog.text
    assert "phase2 stage finished stage=codex-worker" not in caplog.text


def test_phase2_main_logs_invalid_stage_without_echoing_argument(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sensitive_argument = "invalid-stage-secret-token"
    monkeypatch.setenv("ORCH_ISSUE_BRANCH", "exp/582-safe\nsecret-token")

    with caplog.at_level(logging.ERROR, logger=phase2.__name__):
        exit_code = phase2.main([sensitive_argument])

    assert exit_code == 1
    assert "phase2 stage selection failed reason=invalid_stage_argument" in caplog.text
    assert sensitive_argument not in caplog.text
    assert "branch=unknown" in caplog.text
    assert "secret-token" not in caplog.text


def test_phase2_logs_a_branch_named_only_by_the_issue_number(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#589 이후의 브랜치가 `unknown`으로 지워지면 장애 시점에 식별자가 사라진다."""
    monkeypatch.setenv("ORCH_ISSUE_BRANCH", "exp/582")

    with caplog.at_level(logging.ERROR, logger=phase2.__name__):
        assert phase2.main(["not-a-stage"]) == 1

    assert "branch=exp/582" in caplog.text


def test_phase2_main_logs_sanitized_domain_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("ORCH_EXPERIMENT_ID", str(_EXPERIMENT_ID))
    monkeypatch.setenv("ORCH_ISSUE_NUMBER", "582")
    monkeypatch.setenv("ORCH_ISSUE_BRANCH", "exp/582-phase2-failure-logging")
    monkeypatch.setenv("ORCH_BASE_DEV_SHA", "a" * 40)

    def fail() -> int:
        raise phase2.Phase2ExecutorError("issue_marker_mismatch")

    monkeypatch.setattr(phase2, "workspace_preparer_main", fail)

    with caplog.at_level(logging.ERROR, logger=phase2.__name__):
        exit_code = phase2.main(["workspace-preparer"])

    assert exit_code == 1
    assert (
        "phase2 stage failed stage=workspace-preparer "
        "error_type=Phase2ExecutorError reason=issue_marker_mismatch"
    ) in caplog.text
    assert f"experiment_id={_EXPERIMENT_ID}" in caplog.text
    assert "issue_number=582" in caplog.text
    assert "branch=exp/582-phase2-failure-logging" in caplog.text
    assert f"base_sha={'a' * 40}" in caplog.text


def test_phase2_module_execution_preserves_phase2_failure_reason(
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent_orchestration.executor.phase2",
            "workspace-preparer",
        ],
        check=False,
        capture_output=True,
        env={},
        text=True,
    )

    assert completed.returncode == 1
    assert (
        "phase2 stage failed stage=workspace-preparer "
        "error_type=Phase2ExecutorError reason=missing ORCH_EXPERIMENT_ID"
    ) in completed.stderr


@pytest.mark.parametrize(
    "failure",
    [
        OSError("/var/run/secrets/private-token"),
        RuntimeError("response body contains secret-token"),
        ValueError("invalid value secret-token"),
        KeyError("secret-token"),
    ],
)
def test_phase2_main_redacts_external_failure_details(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
) -> None:
    def fail() -> int:
        raise failure

    monkeypatch.setattr(phase2, "workspace_preparer_main", fail)

    with caplog.at_level(logging.ERROR, logger=phase2.__name__):
        exit_code = phase2.main(["workspace-preparer"])

    assert exit_code == 1
    assert f"error_type={type(failure).__name__} reason=redacted" in caplog.text
    assert "private-token" not in caplog.text
    assert "secret-token" not in caplog.text


@pytest.mark.parametrize(
    "unsafe_reason",
    [
        "/var/run/secrets/private-token",
        "response body contains secret-token",
        "safe_code\nsecret-token",
    ],
)
def test_phase2_main_redacts_unsafe_executor_domain_reason(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    unsafe_reason: str,
) -> None:
    def fail() -> int:
        raise UnsafeExecutorError(unsafe_reason)

    monkeypatch.setattr(phase2, "workspace_preparer_main", fail)

    with caplog.at_level(logging.ERROR, logger=phase2.__name__):
        exit_code = phase2.main(["workspace-preparer"])

    assert exit_code == 1
    assert "error_type=UnsafeExecutorError reason=redacted" in caplog.text
    assert "private-token" not in caplog.text
    assert "secret-token" not in caplog.text


def _settings() -> LauncherSettings:
    return LauncherSettings(
        database_url="postgresql://launcher:password@db/orchestration",
        job_namespace="agent-orchestration",
        executor_image=(
            "asia-northeast3-docker.pkg.dev/example/executor@sha256:" + "b" * 64
        ),
        executor_service_account="experiment-executor",
        executor_node_pool="batch-od",
        github_app_secret_name="experiment-app",
        github_app_id=123,
        github_app_installation_id=456,
        github_repository="SKYAHO/Autoresearch",
        max_concurrent_experiments=2,
        executor_api_url="http://agent-orchestration-api",
        executor_api_token_secret_name="executor-api-token",
        codex_home_secret_name="codex-auth",
        workspace_size_limit="8Gi",
        codex_timeout_sec=900,
        active_deadline_sec=2700,
    )


def _claim() -> ClaimedExperiment:
    return ClaimedExperiment(
        experiment_id=_EXPERIMENT_ID,
        issue_number=557,
        issue_branch="exp/557-phase2",
        base_dev_sha="a" * 40,
        job_name=f"ar-exec-{_EXPERIMENT_ID.hex}",
    )


def _session() -> tuple[Session, object]:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def register_uuid_function(
        dbapi_connection: object, _connection_record: object
    ) -> None:
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)(), engine


def test_claim_does_not_require_a_stored_issue_body(
    monkeypatch: object,
) -> None:
    """실행 입력은 GitHub에서 읽으므로 DB 본문 부재가 선점을 막으면 안 된다."""
    session, engine = _session()
    try:
        monkeypatch.setattr(
            "agent_orchestration.launcher.repository._try_advisory_lock",
            lambda _session: True,
        )
        missing_body = Experiment(
            id=uuid.UUID(int=1),
            hypothesis="missing body",
            status=ExperimentStatus.CREATED.value,
            issue_number=558,
            issue_branch="exp/558-missing-body",
            base_dev_sha="a" * 40,
        )
        session.add(missing_body)
        session.commit()

        claims = claim_experiments(session, active_jobs=0, max_concurrency=2)

        assert len(claims) == 1
        assert claims[0].experiment_id == missing_body.id
        assert missing_body.status == ExperimentStatus.RUNNING.value
        assert missing_body.executor_job_name == f"ar-exec-{missing_body.id.hex}"
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_executor_job_has_sealed_eight_container_capability_boundaries() -> None:
    """token/state/.git mount 하나라도 넓어지면 재시도 Pod가 권한을 오용할 수 있다."""
    job = build_executor_job(_claim(), _settings())
    pod = job.spec.template.spec
    containers = [*pod.init_containers, *pod.containers]

    assert job.metadata.name == f"ar-exec-{_EXPERIMENT_ID.hex}"
    assert job.metadata.labels == {"app.kubernetes.io/component": "experiment-executor"}
    assert job.spec.backoff_limit == 1
    assert job.spec.active_deadline_seconds == 2700
    assert [container.name for container in pod.init_containers] == [
        "branch-token-minter",
        "branch-creator",
        "clone-token-minter",
        "workspace-preparer",
        "codex-worker",
        "candidate-verifier",
        "push-token-minter",
    ]
    assert [container.name for container in pod.containers] == ["candidate-finalizer"]
    assert pod.automount_service_account_token is False
    assert all(
        container.security_context.allow_privilege_escalation is False
        and container.security_context.capabilities.drop == ["ALL"]
        for container in containers
    )

    mounts = {
        container.name: {
            (mount.name, mount.mount_path, mount.sub_path, mount.read_only)
            for mount in container.volume_mounts
        }
        for container in containers
    }
    assert {
        name
        for name, values in mounts.items()
        if any(volume == "github-app-private-key" for volume, *_rest in values)
    } == {"branch-token-minter", "clone-token-minter", "push-token-minter"}
    assert ("branch-token", "/var/run/branch-token", None, False) in mounts[
        "branch-token-minter"
    ]
    assert ("clone-token", "/var/run/clone-token", None, False) in mounts[
        "clone-token-minter"
    ]
    assert ("push-token", "/var/run/push-token", None, False) in mounts[
        "push-token-minter"
    ]
    assert ("executor-state", "/var/run/executor-state", None, False) in mounts[
        "workspace-preparer"
    ]
    for name in ("codex-worker", "candidate-verifier", "candidate-finalizer"):
        assert ("executor-state", "/var/run/executor-state", None, True) in mounts[name]
    for name in ("codex-worker", "candidate-verifier"):
        assert ("workspace", "/workspace", None, False) in mounts[name]
        assert (
            "workspace",
            "/workspace/repository/.git",
            "repository/.git",
            True,
        ) in mounts[name]
    assert (
        "codex-home",
        "/var/lib/codex/auth.json",
        "auth.json",
        True,
    ) in mounts["codex-worker"]
    assert {
        name
        for name, container_mounts in mounts.items()
        if any(volume == "codex-home" for volume, *_rest in container_mounts)
    } == {"codex-worker"}
    codex_volume = next(volume for volume in pod.volumes if volume.name == "codex-home")
    assert codex_volume.persistent_volume_claim is None
    assert codex_volume.secret.secret_name == "codex-auth"
    assert codex_volume.secret.default_mode == 0o440
    assert [(item.key, item.path) for item in codex_volume.secret.items] == [
        ("auth.json", "auth.json")
    ]
    assert any(
        volume == "executor-api-token"
        for volume, *_rest in mounts["candidate-finalizer"]
    )
    assert not any(
        volume == "executor-api-token" for volume, *_rest in mounts["push-token-minter"]
    )
    finalizer_environment = {item.name: item.value for item in pod.containers[0].env}
    assert finalizer_environment["ORCH_EXECUTOR_WORKSPACE"] == "/workspace"
    preparer_environment = {
        item.name: item.value
        for item in next(
            container
            for container in pod.init_containers
            if container.name == "workspace-preparer"
        ).env
    }
    assert "ORCH_ISSUE_BODY_SHA256" not in preparer_environment
    for name in (
        "workspace-preparer",
        "codex-worker",
        "candidate-verifier",
        "candidate-finalizer",
    ):
        assert ("executor-tmp", "/tmp", None, False) in mounts[name]
    volumes = {volume.name: volume for volume in pod.volumes}
    assert volumes["executor-tmp"].empty_dir.medium == "Memory"


def test_terminal_failed_executor_job_moves_running_experiment_to_error_once(
    monkeypatch: object,
) -> None:
    """두 번째 Pod까지 실패한 Job만 회수하고 기존 Phase 1 Job은 건드리지 않는다."""
    session, engine = _session()
    try:
        experiment = Experiment(
            id=_EXPERIMENT_ID,
            hypothesis="failed executor",
            status=ExperimentStatus.RUNNING.value,
            issue_body=_BODY,
            issue_number=557,
            issue_branch="exp/557-phase2",
            base_dev_sha="a" * 40,
            executor_job_name=f"ar-exec-{_EXPERIMENT_ID.hex}",
            executor_job_created_at=datetime(2026, 8, 6, tzinfo=UTC),
        )
        legacy = Experiment(
            id=uuid.UUID(int=2),
            hypothesis="legacy branch bootstrap",
            status=ExperimentStatus.RUNNING.value,
            issue_body=_BODY,
            issue_number=554,
            issue_branch="exp/554-legacy",
            base_dev_sha="a" * 40,
            executor_job_name="ar-branch-legacy",
        )
        session.add_all((experiment, legacy))
        session.commit()

        recovered = reconcile_failed_jobs(session, {experiment.executor_job_name})
        repeated = reconcile_failed_jobs(session, {experiment.executor_job_name})

        assert recovered == [experiment.id]
        assert repeated == []
        assert experiment.status == ExperimentStatus.ERROR.value
        assert legacy.status == ExperimentStatus.RUNNING.value
        assert session.scalars(
            select(ExperimentEvent).where(
                ExperimentEvent.experiment_id == experiment.id,
                ExperimentEvent.to_status == ExperimentStatus.ERROR.value,
            )
        ).all()
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_terminal_failed_executor_job_recovers_an_evaluating_experiment(
    monkeypatch: object,
) -> None:
    """candidate 보고 뒤에 죽은 실험도 회수한다.

    candidate 학습·채점·게시·결과 보고가 candidate 보고 **뒤에** 온다. 거기서 죽으면
    Job은 Failed인데 실험은 EVALUATING에 남아 끝나지 않는 "평가 중"이 된다.
    """
    session, engine = _session()
    try:
        stuck = Experiment(
            id=_EXPERIMENT_ID,
            hypothesis="died after candidate report",
            status=ExperimentStatus.EVALUATING.value,
            issue_body=_BODY,
            issue_number=557,
            issue_branch="exp/557-phase2",
            base_dev_sha="a" * 40,
            candidate_sha="c" * 40,
            executor_job_name=f"ar-exec-{_EXPERIMENT_ID.hex}",
            executor_job_created_at=datetime(2026, 8, 6, tzinfo=UTC),
        )
        completed = Experiment(
            id=uuid.UUID(int=3),
            hypothesis="reported results before the pod died",
            status=ExperimentStatus.PASSED.value,
            issue_body=_BODY,
            issue_number=558,
            issue_branch="exp/558-phase2",
            base_dev_sha="a" * 40,
            candidate_sha="d" * 40,
            executor_job_name=f"ar-exec-{uuid.UUID(int=3).hex}",
        )
        session.add_all((stuck, completed))
        session.commit()

        recovered = reconcile_failed_jobs(
            session, {stuck.executor_job_name, completed.executor_job_name}
        )

        assert recovered == [stuck.id]
        assert stuck.status == ExperimentStatus.ERROR.value
        # 결과가 이미 남은 실험을 실행 실패로 되돌리면 사실과 어긋난다.
        assert completed.status == ExperimentStatus.PASSED.value
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_launcher_reconciles_only_failed_phase2_jobs_before_claiming(
    monkeypatch: object,
) -> None:
    """tick은 Phase 1 selector나 Complete Job을 ERROR 회수 후보로 해석하면 안 된다."""
    session, engine = _session()

    class _Jobs:
        def list_terminal(self, namespace: str, selector: str) -> set[str]:
            assert namespace == "agent-orchestration"
            assert selector == EXPERIMENT_EXECUTOR_LABEL_SELECTOR
            return {f"ar-exec-{_EXPERIMENT_ID.hex}"}

        def count_active(self, _namespace: str, _selector: str) -> int:
            return 0

        def get(self, _namespace: str, _name: str) -> None:
            return None

        def create(self, _namespace: str, _job: object) -> None:
            raise AssertionError("failed job 회수에는 새 Job을 만들면 안 됩니다")

    try:
        monkeypatch.setattr(
            "agent_orchestration.launcher.repository._try_advisory_lock",
            lambda _session: False,
        )
        experiment = Experiment(
            id=_EXPERIMENT_ID,
            hypothesis="failed executor",
            status=ExperimentStatus.RUNNING.value,
            issue_body=_BODY,
            issue_number=557,
            issue_branch="exp/557-phase2",
            base_dev_sha="a" * 40,
            executor_job_name=f"ar-exec-{_EXPERIMENT_ID.hex}",
        )
        session.add(experiment)
        session.commit()

        assert run_tick(session, _Jobs(), _settings()) == []
        assert experiment.status == ExperimentStatus.ERROR.value
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_kubernetes_jobs_lists_only_terminal_failed_phase2_jobs() -> None:
    """Complete Job은 Candidate API 성공 이후이므로 오류 회수 대상이 아니다."""
    failed = SimpleNamespace(
        metadata=SimpleNamespace(name="ar-exec-failed"),
        status=SimpleNamespace(
            conditions=[SimpleNamespace(type="Failed", status="True")]
        ),
    )
    complete = SimpleNamespace(
        metadata=SimpleNamespace(name="ar-exec-complete"),
        status=SimpleNamespace(
            conditions=[SimpleNamespace(type="Complete", status="True")]
        ),
    )
    api = SimpleNamespace(
        list_namespaced_job=lambda **_kwargs: SimpleNamespace(items=[failed, complete])
    )

    assert KubernetesJobs(api).list_terminal(
        "agent-orchestration", EXPERIMENT_EXECUTOR_LABEL_SELECTOR
    ) == {"ar-exec-failed"}


def _set_phase2_environment(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """실제 Phase 2 entrypoint가 읽는 Pod env를 fake volume 좌표로 설정한다."""
    values = {
        "ORCH_EXPERIMENT_ID": str(_EXPERIMENT_ID),
        "ORCH_ISSUE_NUMBER": "557",
        "ORCH_ISSUE_BRANCH": "exp/557-phase2",
        "ORCH_BASE_DEV_SHA": "a" * 40,
        "ORCH_GITHUB_REPOSITORY": "SKYAHO/Autoresearch",
        "ORCH_GITHUB_TOKEN_FILE": str(root / "clone-token"),
        "ORCH_EXECUTOR_WORKSPACE": str(root / "workspace"),
        "ORCH_CODEX_HOME": str(root / "codex-home"),
        "ORCH_CODEX_TIMEOUT_SEC": "120",
        "ORCH_EXECUTOR_API_URL": "http://executor-api",
        "ORCH_EXECUTOR_API_TOKEN_FILE": str(root / "api-token"),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    (root / "clone-token").write_text("clone", encoding="utf-8")
    (root / "api-token").write_text("api", encoding="utf-8")
    (root / "codex-home").mkdir()


def test_base_tip_entrypoints_pass_sealed_state_to_verifier_and_finalizer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """manifest env에서 시작한 base-tip 실행은 Codex 뒤 handoff와 예상 tip을 보존한다."""
    _set_phase2_environment(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    state_path = tmp_path / "state" / "state.json"
    verification_path = tmp_path / "verification" / "result.json"
    monkeypatch.setattr(phase2, "_STATE_PATH", state_path)
    monkeypatch.setattr(phase2, "_VERIFICATION_PATH", verification_path)
    state = ExecutorWorkspaceState(
        schema_version=1,
        repository=repository,
        issue_body=_BODY,
        allowed_scope=("promotion",),
        base_dev_sha="a" * 40,
        remote_tip="a" * 40,
    )
    prepared_inputs: list[object] = []

    async def fake_prepare_workspace(
        config: object, issues: object
    ) -> PreparedWorkspace:
        prepared_inputs.append((config, issues))
        write_state(state_path, state, workspace=workspace)
        return PreparedWorkspace(
            repository=repository,
            issue_body=_BODY,
            allowed_scope=("promotion",),
            remote_tip="a" * 40,
        )

    codex_states: list[ExecutorWorkspaceState] = []
    verification = VerificationResult(
        ("autoresearch/change.py",), "fingerprint", "b" * 40
    )
    verification_inputs: list[tuple[Path, str, str | None, object]] = []
    finalized: list[tuple[object, VerificationResult]] = []
    monkeypatch.setattr(phase2, "prepare_workspace", fake_prepare_workspace)
    monkeypatch.setattr(
        phase2,
        "run_codex_for_workspace",
        lambda received, **_kwargs: (
            codex_states.append(received) or CodexRunResult(exit_code=0, duration_ms=1)
        ),
    )
    monkeypatch.setattr(
        phase2,
        "verify_candidate",
        lambda repository, base_sha, candidate_sha, policy: (
            verification_inputs.append((repository, base_sha, candidate_sha, policy))
            or verification
        ),
    )
    monkeypatch.setattr(
        phase2,
        "finalize_candidate",
        lambda config, received: finalized.append((config, received)) or "c" * 40,
    )

    assert phase2.workspace_preparer_main() == 0
    assert phase2.codex_worker_main() == 0
    assert phase2.candidate_verifier_main() == 0
    monkeypatch.setenv("ORCH_GITHUB_TOKEN_FILE", str(tmp_path / "push-token"))
    (tmp_path / "push-token").write_text("push", encoding="utf-8")
    assert phase2.candidate_finalizer_main() == 0

    assert len(prepared_inputs) == 1
    assert codex_states == [state]
    assert verification_inputs[0][:3] == (repository, "a" * 40, None)
    assert verification_inputs[0][3].allowed_scope == ("promotion",)
    config, received = finalized[0]
    assert config.expected_remote_tip == "a" * 40
    assert received == verification


def _base_tip_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """codex-worker가 실제로 Codex를 실행하는 base tip state를 만든다."""
    _set_phase2_environment(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    state_path = tmp_path / "state" / "state.json"
    monkeypatch.setattr(phase2, "_STATE_PATH", state_path)
    write_state(
        state_path,
        ExecutorWorkspaceState(
            schema_version=1,
            repository=repository,
            issue_body=_BODY,
            allowed_scope=(),
            base_dev_sha="a" * 40,
            remote_tip="a" * 40,
        ),
        workspace=workspace,
    )


def test_codex_worker_logs_codex_output_even_when_it_exits_zero(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Codex는 sandbox 실패도 exit 0으로 보고하므로 종료 코드만으로는 구분되지 않는다(#612)."""
    _base_tip_state(monkeypatch, tmp_path)
    monkeypatch.setattr(
        phase2,
        "run_codex_for_workspace",
        lambda *_args, **_kwargs: CodexRunResult(
            exit_code=0,
            duration_ms=1,
            stdout="the workspace sandbox failed to initialize",
            stderr="",
        ),
    )

    with caplog.at_level(logging.INFO):
        assert phase2.codex_worker_main() == 0

    assert "the workspace sandbox failed to initialize" in caplog.text
    assert "codex output stage=codex-worker stream=stdout" in caplog.text
    # 비어 있어도 한 줄을 남겨 "출력 없음"과 "로깅 깨짐"을 구분한다.
    assert "codex output stage=codex-worker stream=stderr bytes=0" in caplog.text


def test_codex_worker_logs_output_before_propagating_a_worker_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """timeout처럼 결과가 없는 경로에서도 원문이 남고 사유 코드는 정제된 채로 유지된다."""
    _base_tip_state(monkeypatch, tmp_path)
    error = CodexWorkerError("codex_timeout")
    error.stdout = "partial codex transcript"

    def fail(*_args: object, **_kwargs: object) -> CodexRunResult:
        raise error

    monkeypatch.setattr(phase2, "run_codex_for_workspace", fail)

    with caplog.at_level(logging.INFO):
        assert phase2.main(["codex-worker"]) == 1

    assert "partial codex transcript" in caplog.text
    assert "reason=codex_timeout" in caplog.text
    assert "reason=redacted" not in caplog.text


def test_candidate_verifier_reports_failing_pytest_without_rejecting(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """pytest가 차단하지 않게 된 뒤에는 이 로그가 유일한 관측 수단이다(#615)."""
    _base_tip_state(monkeypatch, tmp_path)
    verification_path = tmp_path / "verification" / "result.json"
    monkeypatch.setattr(phase2, "_VERIFICATION_PATH", verification_path)
    monkeypatch.setattr(
        phase2,
        "verify_candidate",
        lambda *_args, **_kwargs: VerificationResult(
            ("autoresearch/change.py",),
            "fingerprint",
            "b" * 40,
            1,
            "FAILED tests/test_unrelated.py::test_environment_dependent",
        ),
    )

    with caplog.at_level(logging.INFO):
        assert phase2.candidate_verifier_main() == 0

    assert "pytest observation stage=candidate-verifier blocking=false exit_code=1" in caplog.text
    assert "test_environment_dependent" in caplog.text
    # handoff에도 그대로 실려 기본값으로 되돌아가지 않는다.
    assert phase2._read_verification().pytest_exit_code == 1


def test_candidate_verifier_logs_a_line_even_when_pytest_passes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """통과와 미실행이 로그에서 같아 보이면 관측을 켠 의미가 없다."""
    _base_tip_state(monkeypatch, tmp_path)
    monkeypatch.setattr(phase2, "_VERIFICATION_PATH", tmp_path / "verification" / "result.json")
    monkeypatch.setattr(
        phase2,
        "verify_candidate",
        lambda *_args, **_kwargs: VerificationResult(
            ("autoresearch/change.py",), "fingerprint", "b" * 40
        ),
    )

    with caplog.at_level(logging.INFO):
        assert phase2.candidate_verifier_main() == 0

    assert "pytest observation stage=candidate-verifier blocking=false exit_code=0" in caplog.text


def test_existing_candidate_entrypoints_skip_codex_and_preserve_remote_tip(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """첫 Pod API 실패 뒤 재시도는 Codex 없이 state의 기존 candidate SHA만 채택한다."""
    _set_phase2_environment(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    state_path = tmp_path / "state" / "state.json"
    verification_path = tmp_path / "verification" / "result.json"
    monkeypatch.setattr(phase2, "_STATE_PATH", state_path)
    monkeypatch.setattr(phase2, "_VERIFICATION_PATH", verification_path)
    existing_sha = "b" * 40
    state = ExecutorWorkspaceState(
        schema_version=1,
        repository=repository,
        issue_body=_BODY,
        allowed_scope=(),
        base_dev_sha="a" * 40,
        remote_tip=existing_sha,
    )
    write_state(state_path, state, workspace=workspace)
    verification = VerificationResult(
        ("autoresearch/change.py",), "fingerprint", "c" * 40
    )
    verification_candidates: list[str | None] = []
    finalized: list[object] = []
    monkeypatch.setattr(
        phase2,
        "verify_candidate",
        lambda _repository, _base_sha, candidate_sha, _policy: (
            verification_candidates.append(candidate_sha) or verification
        ),
    )
    monkeypatch.setattr(
        phase2,
        "finalize_candidate",
        lambda config, _verification: finalized.append(config) or existing_sha,
    )

    assert phase2.codex_worker_main() == 0
    assert phase2.candidate_verifier_main() == 0
    monkeypatch.setenv("ORCH_GITHUB_TOKEN_FILE", str(tmp_path / "push-token"))
    (tmp_path / "push-token").write_text("push", encoding="utf-8")
    assert phase2.candidate_finalizer_main() == 0

    assert verification_candidates == [existing_sha]
    assert finalized[0].expected_remote_tip == existing_sha


def _finalizer_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """finalizer가 도달 가능한 최소 state와 push token을 갖춘 workspace를 만든다."""
    _set_phase2_environment(monkeypatch, tmp_path)
    workspace = tmp_path / "workspace"
    repository = workspace / "repository"
    repository.mkdir(parents=True)
    state_path = tmp_path / "state" / "state.json"
    monkeypatch.setattr(phase2, "_STATE_PATH", state_path)
    write_state(
        state_path,
        ExecutorWorkspaceState(
            schema_version=1,
            repository=repository,
            issue_body=_BODY,
            allowed_scope=(),
            base_dev_sha="a" * 40,
            remote_tip="a" * 40,
        ),
        workspace=workspace,
    )
    verification_path = tmp_path / "verification" / "result.json"
    monkeypatch.setattr(phase2, "_VERIFICATION_PATH", verification_path)
    verification_path.parent.mkdir(parents=True)
    verification_path.write_text(
        json.dumps(
            asdict(VerificationResult(("autoresearch/x.py",), "fingerprint", "b" * 40))
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        phase2, "finalize_candidate", lambda config, received: "c" * 40
    )
    monkeypatch.setenv("ORCH_GITHUB_TOKEN_FILE", str(tmp_path / "push-token"))
    (tmp_path / "push-token").write_text("push", encoding="utf-8")
    return repository


def _metrics_payload(seeds: tuple[int, ...] = (42, 43)) -> dict[str, object]:
    """`build_experiment_metrics`가 만드는 payload의 최소 실물 형태다.

    요약 조립을 대역으로 바꾸지 않기 위해 실제 형태를 쓴다 — 배선 테스트가 요약의
    형태 변화까지 잡아야 한다.
    """

    def _condition(offset: float) -> dict[str, dict[str, object]]:
        return {
            str(seed): {
                "roc_auc": 0.78 + offset,
                "log_loss": 0.087,
                "brier": 0.013,
            }
            for seed in seeds
        }

    return {
        "contract_version": "experiment-metrics-v1",
        "coordinates": {},
        "dataset_fingerprint": "d" * 64,
        "seeds": list(seeds),
        "conditions": {"baseline": _condition(0.0), "candidate": _condition(0.01)},
        "paired": {
            name: {
                "per_seed": {seed: 0.01 for seed in seeds},
                "mean": 0.01,
                "standard_error": 0.001,
            }
            for name in ("roc_auc", "log_loss", "brier")
        },
        "split_matches": {str(seed): True for seed in seeds},
    }


def _capture_result_reports(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """API 보고를 실제로 보내지 않고 인자만 관찰한다."""
    reported: list[dict[str, object]] = []
    monkeypatch.setattr(
        phase2, "report_result", lambda **kwargs: reported.append(kwargs)
    )
    return reported


def test_finalizer_skips_measurement_when_training_is_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """학습을 켜지 않은 배포에서는 채점도 게시도 시도하지 않는다.

    seed가 없으면 채점할 산출물이 없다. 여기서 빈 결과를 만들어 게시하면 "측정했다"로
    오인된다.
    """
    _finalizer_ready(monkeypatch, tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(
        phase2, "build_experiment_metrics", lambda *a, **k: calls.append(a) or {}
    )
    reported = _capture_result_reports(monkeypatch)

    assert phase2.candidate_finalizer_main() == 0
    assert calls == []
    # 채점하지 않았으면 보고할 숫자도 없다. 빈 보고는 "측정했다"로 오인된다.
    assert reported == []


def test_finalizer_publishes_metrics_with_sealed_coordinates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """채점 결과를 실험 좌표와 함께 게시한다.

    `metrics.json` 하나만 보고 어느 실험의 무엇인지 알 수 있어야 한다 — 좌표가 없으면
    Pod 밖으로 나간 순간 출처를 잃는다.
    """
    repository = _finalizer_ready(monkeypatch, tmp_path)
    monkeypatch.setenv("ORCH_TRAINING_DATASET_URI", f"gs://b/by-hash/{'d' * 64}/")
    monkeypatch.setenv("ORCH_TRAINING_TIMEOUT_SEC", "600")
    monkeypatch.setenv("ORCH_EXPERIMENT_RESULTS_ROOT", "gs://results")
    (tmp_path / "workspace" / "training-output").mkdir()
    monkeypatch.setattr(
        phase2, "_run_training_if_enabled", lambda stage, workspace: (42, 43)
    )

    captured: dict[str, object] = {}

    def _fake_build(config, *, coordinates, dataset_fingerprint):
        captured["coordinates"] = coordinates
        captured["seeds"] = config.seeds
        captured["workspace"] = config.workspace
        return _metrics_payload()

    published: list[tuple[str, dict[str, object]]] = []

    def _fake_publish(root, files, *, issue_number, experiment_id):
        published.append((root, dict(files)))
        return {
            name: PublishedObject(uri=f"{root}/{name}", created=True) for name in files
        }

    monkeypatch.setattr(phase2, "build_experiment_metrics", _fake_build)
    monkeypatch.setattr(phase2, "publish_results", _fake_publish)
    _capture_result_reports(monkeypatch)

    assert phase2.candidate_finalizer_main() == 0

    assert captured["seeds"] == (42, 43)
    assert captured["workspace"] == repository
    coordinates = captured["coordinates"]
    assert coordinates["issue_number"] == 557
    assert coordinates["base_dev_sha"] == "a" * 40
    # push가 만든 SHA가 그대로 실려야 한다 — 실험 결과와 코드가 이어지는 유일한 고리다.
    assert coordinates["candidate_sha"] == "c" * 40
    root, files = published[0]
    assert root == "gs://results"
    assert "metrics.json" in files


def test_finalizer_warns_instead_of_publishing_when_root_is_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """게시 루트가 없으면 채점만 하고 그 사실을 남긴다.

    조용히 건너뛰면 나중에 "왜 결과가 없나"에 답할 수 없다 — 채점 실패와 게시 미설정은
    다른 사건이다.
    """
    _finalizer_ready(monkeypatch, tmp_path)
    monkeypatch.setenv("ORCH_TRAINING_DATASET_URI", f"gs://b/by-hash/{'d' * 64}/")
    monkeypatch.setenv("ORCH_TRAINING_TIMEOUT_SEC", "600")
    monkeypatch.delenv("ORCH_EXPERIMENT_RESULTS_ROOT", raising=False)
    (tmp_path / "workspace" / "training-output").mkdir()
    monkeypatch.setattr(
        phase2, "_run_training_if_enabled", lambda stage, workspace: (42,)
    )
    monkeypatch.setattr(
        phase2, "build_experiment_metrics", lambda *a, **k: _metrics_payload()
    )
    published: list[object] = []
    monkeypatch.setattr(
        phase2, "publish_results", lambda *a, **k: published.append(a) or {}
    )
    reported = _capture_result_reports(monkeypatch)

    with caplog.at_level(logging.WARNING):
        assert phase2.candidate_finalizer_main() == 0

    assert published == []
    assert "results_root_unset" in caplog.text
    # 게시 미설정이 API 보고까지 막으면 워크벤치가 다시 빈다 — 채점했으면 보고한다.
    assert len(reported) == 1
    assert reported[0]["metric_snapshot"]["results_uri"] is None


def test_finalizer_reports_the_published_snapshot_to_the_experiment_api(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """채점한 숫자가 실험 행까지 도달한다.

    GCS에만 남기면 워크벤치는 계속 `metric_summary=null`을 본다 — 실험 #619가
    완주하고도 아무것도 안 남은 것으로 보였던 이유다.
    """
    _finalizer_ready(monkeypatch, tmp_path)
    monkeypatch.setenv("ORCH_TRAINING_DATASET_URI", f"gs://b/by-hash/{'d' * 64}/")
    monkeypatch.setenv("ORCH_TRAINING_TIMEOUT_SEC", "600")
    monkeypatch.setenv("ORCH_EXPERIMENT_RESULTS_ROOT", "gs://results")
    (tmp_path / "workspace" / "training-output").mkdir()
    monkeypatch.setattr(
        phase2, "_run_training_if_enabled", lambda stage, workspace: (42, 43)
    )
    monkeypatch.setattr(
        phase2, "build_experiment_metrics", lambda *a, **k: _metrics_payload()
    )
    monkeypatch.setattr(
        phase2,
        "publish_results",
        lambda root, files, **kwargs: {
            name: PublishedObject(uri=f"{root}/619/{name}", created=True)
            for name in files
        },
    )
    reported = _capture_result_reports(monkeypatch)

    assert phase2.candidate_finalizer_main() == 0

    assert len(reported) == 1
    call = reported[0]
    # push가 만든 SHA로 보고해야 서버가 다른 실행의 결과를 걸러낼 수 있다.
    assert call["candidate_sha"] == "c" * 40
    snapshot = call["metric_snapshot"]
    assert snapshot["contract_version"] == "experiment-metric-snapshot-v1"
    assert snapshot["split_matches"] is True
    # 전문의 위치가 요약에 실려야 워크벤치에서 seed별 숫자로 내려갈 수 있다.
    assert snapshot["results_uri"] == "gs://results/619/metrics.json"
