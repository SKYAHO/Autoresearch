"""Phase 2 executor Job의 조립·재시도·종료 회수 경계를 검증한다.

전체 파이프라인에서 launcher가 DB에 봉인된 실험을 8-container executor Job으로
전달한 뒤, 최종 Kubernetes 실패를 Experiment ERROR로 회수하는 구간을 검증한다.
실제 GitHub·Kubernetes·Codex 호출은 하지 않고 manifest와 repository 경계를 관찰한다.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
from agent_orchestration.executor.codex_worker import CodexRunResult
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
