"""실험 launcher의 DB 선점과 Kubernetes Job 생성 계약을 검증한다.

[파이프라인] `[AR]` 이슈와 기준 SHA가 DB에 봉인된 뒤 executor Pod가 exp branch를
만들기 전 — CronJob 한 tick이 전역 상한 안에서 Experiment를 선점하고 결정론적 Job을
생성하는 제어 경계를 검증한다.

[기능] PostgreSQL lock/query SQL, SQLite 상태·event 원자성, 중단 복구와 409 멱등성,
init/app container 사이의 시크릿·좌표 격리를 검증한다.

[비책임] 실제 PostgreSQL 동시 실행, Kubernetes admission/RBAC/egress와 executor의 Git
ref 생성은 각각 통합 환경·Autoresearch-infra·``tests/test_experiment_executor.py``의
검증 범위다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker

from agent_orchestration.app.database import Base
from agent_orchestration.app.experiments.models import (
    Experiment,
    ExperimentEvent,
    ExperimentStatus,
)
from agent_orchestration.launcher import repository as launcher_repository
from agent_orchestration.launcher.config import LauncherConfigError, LauncherSettings
from agent_orchestration.launcher.jobs import (
    KubernetesJobs,
    build_branch_job,
)
from agent_orchestration.launcher.main import run_tick
from agent_orchestration.launcher.repository import (
    ADVISORY_LOCK_STATEMENT,
    CREATED_CLAIM_STATEMENT,
    ClaimedExperiment,
    claim_experiments,
)


EXPERIMENT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
UTC_NOW = datetime(2026, 8, 5, 3, 0, tzinfo=UTC)
EXECUTOR_IMAGE = "asia-northeast3-docker.pkg.dev/example/executor@sha256:" + "b" * 64
LABEL_SELECTOR = "app.kubernetes.io/component=branch-bootstrap"


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    """PostgreSQL UUID server default를 재현하는 in-memory engine."""
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def register_uuid_function(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)

    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def session(sqlite_engine: Engine) -> Iterator[Session]:
    factory = sessionmaker(bind=sqlite_engine, expire_on_commit=False)
    with factory() as database_session:
        yield database_session


@pytest.fixture(autouse=True)
def sqlite_advisory_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLite 결과 테스트에서는 PostgreSQL 전용 lock 획득만 명시적으로 대체한다."""
    monkeypatch.setattr(
        launcher_repository,
        "_try_advisory_lock",
        lambda _session: True,
    )


def _settings(
    *,
    executor_image: str = EXECUTOR_IMAGE,
    executor_node_pool: str = "batch-od",
    max_concurrent_experiments: int = 5,
) -> LauncherSettings:
    return LauncherSettings(
        database_url="postgresql://launcher:password@db/orchestration",
        job_namespace="agent-orchestration",
        executor_image=executor_image,
        executor_service_account="experiment-branch-executor",
        executor_node_pool=executor_node_pool,
        github_app_secret_name="autoresearch-experiment-branch-writer-app",
        github_app_id=123,
        github_app_installation_id=456,
        github_repository="SKYAHO/Autoresearch",
        max_concurrent_experiments=max_concurrent_experiments,
    )


def _experiment(
    session: Session,
    *,
    experiment_id: uuid.UUID = EXPERIMENT_ID,
    status: ExperimentStatus = ExperimentStatus.CREATED,
    issue_number: int | None = 546,
    issue_branch: str | None = "exp/546-example",
    base_sha: str | None = "a" * 40,
    job_name: str | None = None,
    job_created_at: datetime | None = None,
) -> Experiment:
    row = Experiment(
        id=experiment_id,
        hypothesis=f"experiment {experiment_id}",
        status=status.value,
        issue_number=issue_number,
        issue_branch=issue_branch,
        base_dev_sha=base_sha,
        executor_job_name=job_name,
        executor_job_created_at=job_created_at,
    )
    session.add(row)
    session.commit()
    return row


def _claim() -> ClaimedExperiment:
    return ClaimedExperiment(
        experiment_id=EXPERIMENT_ID,
        issue_number=546,
        issue_branch="exp/546-example",
        base_dev_sha="a" * 40,
        job_name=f"ar-branch-{EXPERIMENT_ID.hex}",
    )


def _environment(container: object) -> dict[str, str]:
    return {item.name: item.value for item in container.env}


class FakeJobs:
    """Job 조회·생성 호출을 기록하는 Kubernetes API 대역."""

    def __init__(
        self,
        *,
        existing_names: set[str] | None = None,
        active_jobs: int = 0,
        create_error: Exception | None = None,
        confirm_after_create_error: bool = False,
    ) -> None:
        self.existing_names = set(existing_names or set())
        self.active_jobs = active_jobs
        self.create_error = create_error
        self.confirm_after_create_error = confirm_after_create_error
        self.created_names: set[str] = set()
        self.create_attempts: list[str] = []
        self.get_calls: list[str] = []
        self.list_calls: list[tuple[str, str]] = []

    def count_active(self, namespace: str, label_selector: str) -> int:
        self.list_calls.append((namespace, label_selector))
        return self.active_jobs

    def get(self, namespace: str, name: str) -> object | None:
        assert namespace == "agent-orchestration"
        self.get_calls.append(name)
        return object() if name in self.existing_names else None

    def create(self, namespace: str, job: object) -> None:
        assert namespace == "agent-orchestration"
        name = job.metadata.name
        self.create_attempts.append(name)
        if self.create_error is not None:
            if self.confirm_after_create_error:
                self.existing_names.add(name)
            raise self.create_error
        self.created_names.add(name)
        self.existing_names.add(name)


def test_launcher_settings_requires_digest_pinned_executor_image() -> None:
    with pytest.raises(ValueError, match="executor_image"):
        _settings(executor_image="example/executor:latest")


def test_launcher_settings_reads_required_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "ORCH_DATABASE_URL": "postgresql://launcher:password@db/orchestration",
        "ORCH_JOB_NAMESPACE": "agent-orchestration",
        "ORCH_EXECUTOR_IMAGE": EXECUTOR_IMAGE,
        "ORCH_EXECUTOR_SERVICE_ACCOUNT": "experiment-branch-executor",
        "ORCH_EXECUTOR_NODE_POOL": "batch-od",
        "ORCH_GITHUB_APP_SECRET_NAME": "branch-writer-app",
        "ORCH_GITHUB_APP_ID": "123",
        "ORCH_GITHUB_APP_INSTALLATION_ID": "456",
        "ORCH_GITHUB_REPOSITORY": "SKYAHO/Autoresearch",
        "ORCH_MAX_CONCURRENT_EXPERIMENTS": "2",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    settings = LauncherSettings.from_environment()

    assert settings.max_concurrent_experiments == 2
    assert settings.executor_node_pool == "batch-od"
    assert settings.active_deadline_sec == 300
    assert settings.ttl_after_finished_sec == 30


def test_launcher_settings_requires_explicit_executor_node_pool_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "ORCH_DATABASE_URL": "postgresql://launcher:password@db/orchestration",
        "ORCH_JOB_NAMESPACE": "agent-orchestration",
        "ORCH_EXECUTOR_IMAGE": EXECUTOR_IMAGE,
        "ORCH_EXECUTOR_SERVICE_ACCOUNT": "experiment-branch-executor",
        "ORCH_GITHUB_APP_SECRET_NAME": "branch-writer-app",
        "ORCH_GITHUB_APP_ID": "123",
        "ORCH_GITHUB_APP_INSTALLATION_ID": "456",
        "ORCH_GITHUB_REPOSITORY": "SKYAHO/Autoresearch",
        "ORCH_MAX_CONCURRENT_EXPERIMENTS": "2",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ORCH_EXECUTOR_NODE_POOL", raising=False)

    with pytest.raises(
        LauncherConfigError,
        match="missing ORCH_EXECUTOR_NODE_POOL",
    ):
        LauncherSettings.from_environment()


@pytest.mark.parametrize("node_pool", ["", "batch od", "Batch-od", "batch_od"])
def test_launcher_settings_rejects_invalid_executor_node_pool(
    node_pool: str,
) -> None:
    with pytest.raises(LauncherConfigError, match="executor_node_pool"):
        _settings(executor_node_pool=node_pool)


def test_postgresql_claim_statements_keep_lock_and_skip_locked_contract() -> None:
    lock_sql = str(
        ADVISORY_LOCK_STATEMENT.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    claim_sql = str(
        CREATED_CLAIM_STATEMENT.limit(5).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "pg_try_advisory_xact_lock(546, 1)" in lock_sql
    assert "FOR UPDATE SKIP LOCKED" in claim_sql
    assert "experiments.base_dev_sha IS NOT NULL" in claim_sql


def test_claim_is_normal_noop_when_advisory_lock_is_unavailable(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment = _experiment(session)
    monkeypatch.setattr(
        launcher_repository,
        "_try_advisory_lock",
        lambda _session: False,
    )

    assert claim_experiments(session, active_jobs=0, max_concurrency=5) == []

    session.refresh(experiment)
    assert experiment.status == ExperimentStatus.CREATED.value
    assert experiment.executor_job_name is None


@pytest.mark.parametrize(
    ("issue_number", "issue_branch", "base_sha"),
    [
        (None, "exp/546-example", "a" * 40),
        (546, None, "a" * 40),
        (546, "exp/546-example", None),
    ],
)
def test_claim_skips_incomplete_coordinates(
    session: Session,
    issue_number: int | None,
    issue_branch: str | None,
    base_sha: str | None,
) -> None:
    _experiment(
        session,
        issue_number=issue_number,
        issue_branch=issue_branch,
        base_sha=base_sha,
    )

    assert claim_experiments(session, active_jobs=0, max_concurrency=5) == []


def test_claim_reserves_only_available_slots_and_records_stable_event(
    session: Session,
) -> None:
    for index in range(100):
        issue_number = 546 + index
        _experiment(
            session,
            experiment_id=uuid.UUID(int=index + 1),
            issue_number=issue_number,
            issue_branch=f"exp/{issue_number}-example",
        )

    claims = claim_experiments(session, active_jobs=4, max_concurrency=5)

    assert len(claims) == 1
    claim = claims[0]
    assert claim.job_name == f"ar-branch-{claim.experiment_id.hex}"
    persisted = session.get(Experiment, claim.experiment_id)
    assert persisted is not None
    assert persisted.status == ExperimentStatus.RUNNING.value
    assert persisted.executor_job_name == claim.job_name
    assert persisted.executor_job_created_at is None
    event_row = session.scalar(
        select(ExperimentEvent).where(
            ExperimentEvent.experiment_id == claim.experiment_id,
            ExperimentEvent.to_status == ExperimentStatus.RUNNING.value,
        )
    )
    assert event_row is not None
    assert event_row.idempotency_key == f"launcher-claim:{claim.experiment_id}"
    assert event_row.request_fingerprint == (
        "85cadf75c062e9988de043542c6e467b5cd2ee6db418fd5f83745ea862c22ac9"
    )
    assert event_row.reason == f"executor job claimed: {claim.job_name}"


def test_claim_rolls_back_job_name_and_status_when_event_flush_fails(
    session: Session,
) -> None:
    experiment = _experiment(session)

    @event.listens_for(session, "before_flush")
    def fail_claim_event(database_session: Session, _flush_context, _instances) -> None:
        if any(
            isinstance(row, ExperimentEvent)
            and row.idempotency_key == f"launcher-claim:{experiment.id}"
            for row in database_session.new
        ):
            raise RuntimeError("controlled event failure")

    with pytest.raises(RuntimeError, match="controlled event failure"):
        claim_experiments(session, active_jobs=0, max_concurrency=5)

    persisted = session.get(Experiment, experiment.id)
    assert persisted is not None
    assert persisted.status == ExperimentStatus.CREATED.value
    assert persisted.executor_job_name is None
    assert session.scalar(
        select(func.count())
        .select_from(ExperimentEvent)
        .where(ExperimentEvent.experiment_id == experiment.id)
    ) == 0


def test_unconfirmed_recovery_reservation_consumes_remaining_slot(
    session: Session,
) -> None:
    recovery = _experiment(
        session,
        status=ExperimentStatus.RUNNING,
        job_name=f"ar-branch-{EXPERIMENT_ID.hex}",
    )
    waiting = _experiment(
        session,
        experiment_id=uuid.UUID("22345678-1234-5678-1234-567812345678"),
        issue_number=547,
        issue_branch="exp/547-example",
    )

    claims = claim_experiments(session, active_jobs=4, max_concurrency=5)

    assert [claim.experiment_id for claim in claims] == [recovery.id]
    session.refresh(waiting)
    assert waiting.status == ExperimentStatus.CREATED.value
    assert waiting.executor_job_name is None


def test_tick_limits_recoverable_jobs_to_safe_capacity(session: Session) -> None:
    recoveries: list[Experiment] = []
    for index in range(3):
        experiment_id = uuid.UUID(int=index + 1)
        issue_number = 546 + index
        recoveries.append(
            _experiment(
                session,
                experiment_id=experiment_id,
                status=ExperimentStatus.RUNNING,
                issue_number=issue_number,
                issue_branch=f"exp/{issue_number}-example",
                job_name=f"ar-branch-{experiment_id.hex}",
            )
        )
    kubernetes = FakeJobs(active_jobs=0)

    run_tick(
        session,
        kubernetes,
        _settings(max_concurrent_experiments=2),
        clock=lambda: UTC_NOW,
    )

    assert kubernetes.created_names == {
        recoveries[0].executor_job_name,
        recoveries[1].executor_job_name,
    }
    assert recoveries[0].executor_job_created_at == UTC_NOW
    assert recoveries[1].executor_job_created_at == UTC_NOW
    assert recoveries[2].executor_job_created_at is None


def test_tick_skips_incomplete_unconfirmed_recovery(session: Session) -> None:
    _experiment(
        session,
        status=ExperimentStatus.RUNNING,
        base_sha=None,
        job_name=f"ar-branch-{EXPERIMENT_ID.hex}",
    )
    kubernetes = FakeJobs()

    run_tick(session, kubernetes, _settings(), clock=lambda: UTC_NOW)

    assert kubernetes.create_attempts == []


def test_job_passes_only_frozen_coordinates_and_token_file() -> None:
    settings = _settings()
    job = build_branch_job(_claim(), settings)
    pod = job.spec.template.spec

    assert job.metadata.name == f"ar-branch-{EXPERIMENT_ID.hex}"
    assert job.metadata.namespace == settings.job_namespace
    assert job.metadata.labels == {
        "app.kubernetes.io/component": "branch-bootstrap",
    }
    assert job.spec.template.metadata.labels == job.metadata.labels
    assert job.spec.backoff_limit == 0
    assert job.spec.active_deadline_seconds == 300
    assert job.spec.ttl_seconds_after_finished == 30
    assert pod.automount_service_account_token is False
    assert pod.service_account_name == settings.executor_service_account
    assert pod.restart_policy == "Never"
    assert [container.name for container in pod.init_containers] == [
        "github-token-minter"
    ]
    assert [container.name for container in pod.containers] == ["branch-bootstrap"]

    token_minter = pod.init_containers[0]
    branch_bootstrap = pod.containers[0]
    assert token_minter.image == branch_bootstrap.image == EXECUTOR_IMAGE
    assert token_minter.command == [
        "python",
        "-m",
        "agent_orchestration.executor.token_minter",
    ]
    assert branch_bootstrap.command == [
        "python",
        "-m",
        "agent_orchestration.executor.main",
    ]
    assert _environment(token_minter) == {
        "ORCH_GITHUB_APP_ID": "123",
        "ORCH_GITHUB_APP_INSTALLATION_ID": "456",
        "ORCH_GITHUB_APP_PRIVATE_KEY_FILE": (
            "/var/run/secrets/github-app/private-key.pem"
        ),
        "ORCH_GITHUB_TOKEN_FILE": "/var/run/github-token/token",
    }
    assert _environment(branch_bootstrap) == {
        "ORCH_EXPERIMENT_ID": str(EXPERIMENT_ID),
        "ORCH_ISSUE_NUMBER": "546",
        "ORCH_ISSUE_BRANCH": "exp/546-example",
        "ORCH_BASE_DEV_SHA": "a" * 40,
        "ORCH_GITHUB_REPOSITORY": "SKYAHO/Autoresearch",
        "ORCH_GITHUB_TOKEN_FILE": "/var/run/github-token/token",
    }

    volumes = {volume.name: volume for volume in pod.volumes}
    assert volumes["github-app-private-key"].secret.secret_name == (
        settings.github_app_secret_name
    )
    assert [
        (item.key, item.path)
        for item in volumes["github-app-private-key"].secret.items
    ] == [("private-key.pem", "private-key.pem")]
    # root 소유 Secret 파일은 Pod fsGroup 10001에만 읽기를 허용하고, 같은 UID로 실행되는
    # initContainer가 만든 0400 token은 app container가 동일 소유자로 읽는다.
    assert volumes["github-app-private-key"].secret.default_mode == 0o440
    assert volumes["github-token"].empty_dir.medium == "Memory"
    assert volumes["github-token"].empty_dir.size_limit == "1Mi"
    assert {
        (mount.name, mount.mount_path, mount.read_only)
        for mount in token_minter.volume_mounts
    } == {
        ("github-app-private-key", "/var/run/secrets/github-app", True),
        ("github-token", "/var/run/github-token", False),
    }
    assert {
        (mount.name, mount.mount_path, mount.read_only)
        for mount in branch_bootstrap.volume_mounts
    } == {("github-token", "/var/run/github-token", True)}


def test_job_targets_configured_experiment_node_pool_contract() -> None:
    pod = build_branch_job(_claim(), _settings()).spec.template.spec

    assert pod.node_selector == {
        "cloud.google.com/gke-nodepool": "batch-od",
    }
    assert [
        (item.key, item.operator, item.value, item.effect)
        for item in pod.tolerations
    ] == [("workload", "Equal", "batch-od", "NoSchedule")]


def test_job_security_context_meets_restricted_namespace_contract() -> None:
    pod = build_branch_job(_claim(), _settings()).spec.template.spec

    assert pod.security_context.run_as_non_root is True
    assert pod.security_context.run_as_user == 10001
    assert pod.security_context.run_as_group == 10001
    assert pod.security_context.fs_group == 10001
    assert pod.security_context.seccomp_profile.type == "RuntimeDefault"

    for container in [*pod.init_containers, *pod.containers]:
        security_context = container.security_context
        assert security_context.allow_privilege_escalation is False
        assert security_context.capabilities.drop == ["ALL"]
        assert security_context.read_only_root_filesystem is True


def test_tick_recovers_claim_when_job_creation_was_not_confirmed(
    session: Session,
) -> None:
    job_name = f"ar-branch-{EXPERIMENT_ID.hex}"
    experiment = _experiment(
        session,
        status=ExperimentStatus.RUNNING,
        job_name=job_name,
        job_created_at=None,
    )
    kubernetes = FakeJobs(existing_names=set())

    run_tick(session, kubernetes, _settings(), clock=lambda: UTC_NOW)

    assert kubernetes.created_names == {job_name}
    assert kubernetes.list_calls == [("agent-orchestration", LABEL_SELECTOR)]
    assert experiment.executor_job_created_at == UTC_NOW


def test_tick_marks_existing_unconfirmed_job_without_creating_it(
    session: Session,
) -> None:
    job_name = f"ar-branch-{EXPERIMENT_ID.hex}"
    experiment = _experiment(
        session,
        status=ExperimentStatus.RUNNING,
        job_name=job_name,
    )
    kubernetes = FakeJobs(existing_names={job_name}, active_jobs=1)

    run_tick(session, kubernetes, _settings(), clock=lambda: UTC_NOW)

    assert kubernetes.create_attempts == []
    assert experiment.executor_job_created_at == UTC_NOW


def test_tick_does_not_recreate_ttl_deleted_confirmed_job(
    session: Session,
) -> None:
    _experiment(
        session,
        status=ExperimentStatus.RUNNING,
        job_name=f"ar-branch-{EXPERIMENT_ID.hex}",
        job_created_at=UTC_NOW,
    )
    kubernetes = FakeJobs(existing_names=set())

    run_tick(session, kubernetes, _settings(), clock=lambda: UTC_NOW)

    assert kubernetes.created_names == set()
    assert kubernetes.create_attempts == []


def test_tick_accepts_create_409_only_after_same_name_get_confirms(
    session: Session,
) -> None:
    from kubernetes.client.exceptions import ApiException

    experiment = _experiment(
        session,
        status=ExperimentStatus.RUNNING,
        job_name=f"ar-branch-{EXPERIMENT_ID.hex}",
    )
    kubernetes = FakeJobs(
        create_error=ApiException(status=409, reason="AlreadyExists"),
        confirm_after_create_error=True,
    )

    run_tick(session, kubernetes, _settings(), clock=lambda: UTC_NOW)

    assert kubernetes.get_calls == [experiment.executor_job_name] * 2
    assert experiment.executor_job_created_at == UTC_NOW


def test_tick_reraises_create_409_when_get_cannot_confirm(
    session: Session,
) -> None:
    from kubernetes.client.exceptions import ApiException

    experiment = _experiment(
        session,
        status=ExperimentStatus.RUNNING,
        job_name=f"ar-branch-{EXPERIMENT_ID.hex}",
    )
    kubernetes = FakeJobs(
        create_error=ApiException(status=409, reason="AlreadyExists"),
    )

    with pytest.raises(ApiException) as error:
        run_tick(session, kubernetes, _settings(), clock=lambda: UTC_NOW)

    assert error.value.status == 409
    assert experiment.executor_job_created_at is None


def test_kubernetes_jobs_counts_nonterminal_labeled_jobs() -> None:
    api = SimpleNamespace(
        list_namespaced_job=lambda **_kwargs: SimpleNamespace(
            items=[
                SimpleNamespace(status=SimpleNamespace(conditions=None)),
                SimpleNamespace(
                    status=SimpleNamespace(
                        conditions=[SimpleNamespace(type="Complete", status="True")]
                    )
                ),
                SimpleNamespace(
                    status=SimpleNamespace(
                        conditions=[SimpleNamespace(type="Failed", status="True")]
                    )
                ),
            ]
        )
    )

    jobs = KubernetesJobs(api)

    assert jobs.count_active("agent-orchestration", LABEL_SELECTOR) == 1
