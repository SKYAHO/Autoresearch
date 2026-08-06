"""실험 Phase 1·2 Kubernetes Job manifest와 API 경계.

[파이프라인] launcher가 DB에서 봉인 좌표를 선점한 뒤 Phase 1 branch bootstrap 또는
Phase 2 candidate executor Pod를 기동해 exp branch·candidate 보고를 위임하는 구간을
담당한다.

[기능] digest 고정 image로 기존 branch-bootstrap과 8-container executor Job을 조립하고,
label 기반 active/terminal Job 계산·GET·create 및 409 후 동일 이름 재확인을 제공한다.

[비책임] Experiment 선점·생성 확인 시각·실패 회수(`launcher.repository`), Secret 값
보관과 RBAC/admission/egress(Autoresearch-infra), GitHub token·ref 처리(`executor`)는
담당하지 않는다.
"""

from __future__ import annotations

from typing import Protocol

from kubernetes.client import (
    BatchV1Api,
    V1Capabilities,
    V1Container,
    V1EmptyDirVolumeSource,
    V1EnvVar,
    V1Job,
    V1JobSpec,
    V1KeyToPath,
    V1ObjectMeta,
    V1PodSecurityContext,
    V1PodSpec,
    V1PodTemplateSpec,
    V1PersistentVolumeClaimVolumeSource,
    V1SeccompProfile,
    V1SecretVolumeSource,
    V1SecurityContext,
    V1Toleration,
    V1Volume,
    V1VolumeMount,
)
from kubernetes.client.exceptions import ApiException

from agent_orchestration.launcher.config import LauncherSettings
from agent_orchestration.launcher.repository import ClaimedExperiment


BRANCH_BOOTSTRAP_LABEL_SELECTOR = "app.kubernetes.io/component=branch-bootstrap"
EXPERIMENT_EXECUTOR_LABEL_SELECTOR = "app.kubernetes.io/component=experiment-executor"
_PRIVATE_KEY_DIRECTORY = "/var/run/secrets/github-app"
_PRIVATE_KEY_FILE = f"{_PRIVATE_KEY_DIRECTORY}/private-key.pem"
_TOKEN_DIRECTORY = "/var/run/github-token"
_TOKEN_FILE = f"{_TOKEN_DIRECTORY}/token"
_EXECUTOR_USER_ID = 10001
_WORKSPACE_DIRECTORY = "/workspace"
_STATE_DIRECTORY = "/var/run/executor-state"
_CODEX_HOME_DIRECTORY = "/var/lib/codex"
_API_TOKEN_DIRECTORY = "/var/run/executor-api-token"
_TEMP_DIRECTORY = "/tmp"


class JobClient(Protocol):
    """한 launcher tick에 필요한 Kubernetes Job 연산."""

    def count_active(self, namespace: str, label_selector: str) -> int: ...

    def get(self, namespace: str, name: str) -> object | None: ...

    def create(self, namespace: str, job: V1Job) -> None: ...


def _env(name: str, value: str) -> V1EnvVar:
    """literal non-secret 환경 변수 한 개를 만든다."""
    return V1EnvVar(name=name, value=value)


def _restricted_container_security_context() -> V1SecurityContext:
    """restricted namespace에서 executor container가 사용할 최소 권한을 만든다."""
    return V1SecurityContext(
        allow_privilege_escalation=False,
        capabilities=V1Capabilities(drop=["ALL"]),
        read_only_root_filesystem=True,
    )


def _token_minter(
    *, purpose: str, token_volume: str, token_directory: str, settings: LauncherSettings
) -> V1Container:
    """purpose별 최소 권한 installation token을 memory volume에 쓴다."""
    return V1Container(
        name=f"{purpose}-token-minter",
        image=settings.executor_image,
        command=["python", "-m", "agent_orchestration.executor.token_minter"],
        env=[
            _env("ORCH_GITHUB_APP_ID", str(settings.github_app_id)),
            _env(
                "ORCH_GITHUB_APP_INSTALLATION_ID",
                str(settings.github_app_installation_id),
            ),
            _env("ORCH_GITHUB_APP_PRIVATE_KEY_FILE", _PRIVATE_KEY_FILE),
            _env("ORCH_GITHUB_TOKEN_PURPOSE", purpose),
            _env("ORCH_GITHUB_TOKEN_FILE", f"{token_directory}/token"),
        ],
        security_context=_restricted_container_security_context(),
        volume_mounts=[
            V1VolumeMount(
                name="github-app-private-key",
                mount_path=_PRIVATE_KEY_DIRECTORY,
                read_only=True,
            ),
            V1VolumeMount(
                name=token_volume, mount_path=token_directory, read_only=False
            ),
        ],
    )


def _container(
    name: str,
    command: list[str],
    env: list[V1EnvVar],
    mounts: list[V1VolumeMount],
    settings: LauncherSettings,
) -> V1Container:
    """공통 restricted context로 Phase 2 container 하나를 만든다."""
    return V1Container(
        name=name,
        image=settings.executor_image,
        command=command,
        env=env,
        security_context=_restricted_container_security_context(),
        volume_mounts=mounts,
    )


def build_executor_job(claim: ClaimedExperiment, settings: LauncherSettings) -> V1Job:
    """8-container Phase 2 executor Job을 최소 credential mount로 조립한다."""
    labels = {"app.kubernetes.io/component": "experiment-executor"}
    coordinates = [
        _env("ORCH_EXPERIMENT_ID", str(claim.experiment_id)),
        _env("ORCH_ISSUE_NUMBER", str(claim.issue_number)),
        _env("ORCH_ISSUE_BRANCH", claim.issue_branch),
        _env("ORCH_BASE_DEV_SHA", claim.base_dev_sha),
        _env("ORCH_ISSUE_BODY_SHA256", claim.issue_body_sha256),
        _env("ORCH_GITHUB_REPOSITORY", settings.github_repository),
    ]
    workspace_mount = V1VolumeMount(
        name="workspace", mount_path=_WORKSPACE_DIRECTORY, read_only=False
    )
    git_read_only = V1VolumeMount(
        name="workspace",
        mount_path=f"{_WORKSPACE_DIRECTORY}/repository/.git",
        sub_path="repository/.git",
        read_only=True,
    )
    state_read_only = V1VolumeMount(
        name="executor-state", mount_path=_STATE_DIRECTORY, read_only=True
    )
    temporary_mount = V1VolumeMount(
        name="executor-tmp", mount_path=_TEMP_DIRECTORY, read_only=False
    )
    branch_token = "/var/run/branch-token"
    clone_token = "/var/run/clone-token"
    push_token = "/var/run/push-token"
    branch_creator = _container(
        "branch-creator",
        ["python", "-m", "agent_orchestration.executor.main"],
        [*coordinates, _env("ORCH_GITHUB_TOKEN_FILE", f"{branch_token}/token")],
        [V1VolumeMount(name="branch-token", mount_path=branch_token, read_only=True)],
        settings,
    )
    workspace_preparer = _container(
        "workspace-preparer",
        ["python", "-m", "agent_orchestration.executor.phase2", "workspace-preparer"],
        [
            *coordinates,
            _env("ORCH_GITHUB_TOKEN_FILE", f"{clone_token}/token"),
            _env("ORCH_EXECUTOR_WORKSPACE", _WORKSPACE_DIRECTORY),
        ],
        [
            V1VolumeMount(name="clone-token", mount_path=clone_token, read_only=True),
            workspace_mount,
            V1VolumeMount(
                name="executor-state", mount_path=_STATE_DIRECTORY, read_only=False
            ),
            temporary_mount,
        ],
        settings,
    )
    codex_worker = _container(
        "codex-worker",
        ["python", "-m", "agent_orchestration.executor.phase2", "codex-worker"],
        [
            _env("ORCH_EXECUTOR_WORKSPACE", _WORKSPACE_DIRECTORY),
            _env("ORCH_CODEX_HOME", _CODEX_HOME_DIRECTORY),
            _env("ORCH_CODEX_TIMEOUT_SEC", str(settings.codex_timeout_sec)),
        ],
        [
            workspace_mount,
            git_read_only,
            state_read_only,
            temporary_mount,
            V1VolumeMount(
                name="codex-home", mount_path=_CODEX_HOME_DIRECTORY, read_only=True
            ),
        ],
        settings,
    )
    candidate_verifier = _container(
        "candidate-verifier",
        ["python", "-m", "agent_orchestration.executor.phase2", "candidate-verifier"],
        [_env("ORCH_EXECUTOR_WORKSPACE", _WORKSPACE_DIRECTORY)],
        [
            workspace_mount,
            git_read_only,
            state_read_only,
            temporary_mount,
            V1VolumeMount(
                name="verification-result",
                mount_path="/var/run/verification-result",
                read_only=False,
            ),
        ],
        settings,
    )
    candidate_finalizer = _container(
        "candidate-finalizer",
        ["python", "-m", "agent_orchestration.executor.phase2", "candidate-finalizer"],
        [
            *coordinates,
            _env("ORCH_GITHUB_TOKEN_FILE", f"{push_token}/token"),
            _env("ORCH_EXECUTOR_WORKSPACE", _WORKSPACE_DIRECTORY),
            _env("ORCH_EXECUTOR_API_URL", settings.executor_api_url),
            _env("ORCH_EXECUTOR_API_TOKEN_FILE", f"{_API_TOKEN_DIRECTORY}/token"),
        ],
        [
            workspace_mount,
            state_read_only,
            temporary_mount,
            V1VolumeMount(name="push-token", mount_path=push_token, read_only=True),
            V1VolumeMount(
                name="verification-result",
                mount_path="/var/run/verification-result",
                read_only=True,
            ),
            V1VolumeMount(
                name="executor-api-token",
                mount_path=_API_TOKEN_DIRECTORY,
                read_only=True,
            ),
        ],
        settings,
    )
    pod_spec = V1PodSpec(
        automount_service_account_token=False,
        service_account_name=settings.executor_service_account,
        node_selector={"cloud.google.com/gke-nodepool": settings.executor_node_pool},
        tolerations=[
            V1Toleration(
                key="workload",
                operator="Equal",
                value=settings.executor_node_pool,
                effect="NoSchedule",
            )
        ],
        security_context=V1PodSecurityContext(
            run_as_non_root=True,
            run_as_user=_EXECUTOR_USER_ID,
            run_as_group=_EXECUTOR_USER_ID,
            fs_group=_EXECUTOR_USER_ID,
            seccomp_profile=V1SeccompProfile(type="RuntimeDefault"),
        ),
        restart_policy="Never",
        init_containers=[
            _token_minter(
                purpose="branch",
                token_volume="branch-token",
                token_directory=branch_token,
                settings=settings,
            ),
            branch_creator,
            _token_minter(
                purpose="clone",
                token_volume="clone-token",
                token_directory=clone_token,
                settings=settings,
            ),
            workspace_preparer,
            codex_worker,
            candidate_verifier,
            _token_minter(
                purpose="push",
                token_volume="push-token",
                token_directory=push_token,
                settings=settings,
            ),
        ],
        containers=[candidate_finalizer],
        volumes=[
            V1Volume(
                name="github-app-private-key",
                secret=V1SecretVolumeSource(
                    secret_name=settings.github_app_secret_name,
                    items=[V1KeyToPath(key="private-key.pem", path="private-key.pem")],
                    default_mode=0o440,
                ),
            ),
            V1Volume(
                name="branch-token",
                empty_dir=V1EmptyDirVolumeSource(medium="Memory", size_limit="1Mi"),
            ),
            V1Volume(
                name="clone-token",
                empty_dir=V1EmptyDirVolumeSource(medium="Memory", size_limit="1Mi"),
            ),
            V1Volume(
                name="push-token",
                empty_dir=V1EmptyDirVolumeSource(medium="Memory", size_limit="1Mi"),
            ),
            V1Volume(
                name="workspace",
                empty_dir=V1EmptyDirVolumeSource(
                    size_limit=settings.workspace_size_limit
                ),
            ),
            V1Volume(
                name="executor-state",
                empty_dir=V1EmptyDirVolumeSource(medium="Memory", size_limit="1Mi"),
            ),
            V1Volume(
                name="verification-result",
                empty_dir=V1EmptyDirVolumeSource(medium="Memory", size_limit="1Mi"),
            ),
            V1Volume(
                name="executor-tmp",
                empty_dir=V1EmptyDirVolumeSource(medium="Memory", size_limit="1Gi"),
            ),
            V1Volume(
                name="codex-home",
                persistent_volume_claim=V1PersistentVolumeClaimVolumeSource(
                    claim_name=settings.codex_home_pvc_name, read_only=True
                ),
            ),
            V1Volume(
                name="executor-api-token",
                secret=V1SecretVolumeSource(
                    secret_name=settings.executor_api_token_secret_name,
                    items=[V1KeyToPath(key="token", path="token")],
                    default_mode=0o440,
                ),
            ),
        ],
    )
    return V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=V1ObjectMeta(
            name=claim.job_name, namespace=settings.job_namespace, labels=labels
        ),
        spec=V1JobSpec(
            backoff_limit=1,
            active_deadline_seconds=settings.active_deadline_sec,
            ttl_seconds_after_finished=settings.ttl_after_finished_sec,
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(labels=labels), spec=pod_spec
            ),
        ),
    )


def build_branch_job(
    claim: ClaimedExperiment,
    settings: LauncherSettings,
) -> V1Job:
    """기존 Phase 1 branch-bootstrap Job 계약을 별도로 보존한다."""
    labels = {"app.kubernetes.io/component": "branch-bootstrap"}
    token_minter = V1Container(
        name="github-token-minter",
        image=settings.executor_image,
        command=["python", "-m", "agent_orchestration.executor.token_minter"],
        env=[
            _env("ORCH_GITHUB_APP_ID", str(settings.github_app_id)),
            _env(
                "ORCH_GITHUB_APP_INSTALLATION_ID",
                str(settings.github_app_installation_id),
            ),
            _env("ORCH_GITHUB_APP_PRIVATE_KEY_FILE", _PRIVATE_KEY_FILE),
            _env("ORCH_GITHUB_TOKEN_FILE", _TOKEN_FILE),
        ],
        security_context=_restricted_container_security_context(),
        volume_mounts=[
            V1VolumeMount(
                name="github-app-private-key",
                mount_path=_PRIVATE_KEY_DIRECTORY,
                read_only=True,
            ),
            V1VolumeMount(
                name="github-token", mount_path=_TOKEN_DIRECTORY, read_only=False
            ),
        ],
    )
    branch_bootstrap = V1Container(
        name="branch-bootstrap",
        image=settings.executor_image,
        command=["python", "-m", "agent_orchestration.executor.main"],
        env=[
            _env("ORCH_EXPERIMENT_ID", str(claim.experiment_id)),
            _env("ORCH_ISSUE_NUMBER", str(claim.issue_number)),
            _env("ORCH_ISSUE_BRANCH", claim.issue_branch),
            _env("ORCH_BASE_DEV_SHA", claim.base_dev_sha),
            _env("ORCH_GITHUB_REPOSITORY", settings.github_repository),
            _env("ORCH_GITHUB_TOKEN_FILE", _TOKEN_FILE),
        ],
        security_context=_restricted_container_security_context(),
        volume_mounts=[
            V1VolumeMount(
                name="github-token", mount_path=_TOKEN_DIRECTORY, read_only=True
            )
        ],
    )
    pod_spec = V1PodSpec(
        automount_service_account_token=False,
        service_account_name=settings.executor_service_account,
        node_selector={"cloud.google.com/gke-nodepool": settings.executor_node_pool},
        tolerations=[
            V1Toleration(
                key="workload",
                operator="Equal",
                value=settings.executor_node_pool,
                effect="NoSchedule",
            )
        ],
        security_context=V1PodSecurityContext(
            run_as_non_root=True,
            run_as_user=_EXECUTOR_USER_ID,
            run_as_group=_EXECUTOR_USER_ID,
            fs_group=_EXECUTOR_USER_ID,
            seccomp_profile=V1SeccompProfile(type="RuntimeDefault"),
        ),
        restart_policy="Never",
        init_containers=[token_minter],
        containers=[branch_bootstrap],
        volumes=[
            V1Volume(
                name="github-app-private-key",
                secret=V1SecretVolumeSource(
                    secret_name=settings.github_app_secret_name,
                    items=[V1KeyToPath(key="private-key.pem", path="private-key.pem")],
                    default_mode=0o440,
                ),
            ),
            V1Volume(
                name="github-token",
                empty_dir=V1EmptyDirVolumeSource(medium="Memory", size_limit="1Mi"),
            ),
        ],
    )
    return V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=V1ObjectMeta(
            name=claim.job_name, namespace=settings.job_namespace, labels=labels
        ),
        spec=V1JobSpec(
            backoff_limit=0,
            active_deadline_seconds=settings.active_deadline_sec,
            ttl_seconds_after_finished=settings.ttl_after_finished_sec,
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(labels=labels), spec=pod_spec
            ),
        ),
    )


def _is_terminal(job: object) -> bool:
    """Complete 또는 Failed=True condition이 있는 Job인지 반환한다."""
    status = getattr(job, "status", None)
    conditions = getattr(status, "conditions", None) or []
    return any(
        getattr(condition, "type", None) in {"Complete", "Failed"}
        and str(getattr(condition, "status", "")).lower() == "true"
        for condition in conditions
    )


class KubernetesJobs:
    """공식 Kubernetes client의 namespaced Job 연산 adapter."""

    def __init__(self, api: BatchV1Api) -> None:
        self._api = api

    def count_active(self, namespace: str, label_selector: str) -> int:
        """label과 일치하며 아직 terminal condition이 없는 Job 수를 반환한다."""
        response = self._api.list_namespaced_job(
            namespace=namespace,
            label_selector=label_selector,
        )
        return sum(not _is_terminal(job) for job in response.items)

    def get(self, namespace: str, name: str) -> V1Job | None:
        """동일 이름 Job을 반환하고 404만 없음으로 해석한다."""
        try:
            return self._api.read_namespaced_job(name=name, namespace=namespace)
        except ApiException as error:
            if error.status == 404:
                return None
            raise

    def create(self, namespace: str, job: V1Job) -> None:
        """Job을 생성하며 API 오류는 호출자가 판단하도록 전파한다."""
        self._api.create_namespaced_job(namespace=namespace, body=job)

    def list_terminal(self, namespace: str, label_selector: str) -> set[str]:
        """selector에 맞는 최종 Failed Job 이름만 반환한다."""
        response = self._api.list_namespaced_job(
            namespace=namespace, label_selector=label_selector
        )
        return {
            job.metadata.name
            for job in response.items
            if any(
                getattr(condition, "type", None) == "Failed"
                and str(getattr(condition, "status", "")).lower() == "true"
                for condition in (
                    getattr(getattr(job, "status", None), "conditions", None) or []
                )
            )
        }


def ensure_executor_job(
    jobs: JobClient,
    claim: ClaimedExperiment,
    settings: LauncherSettings,
    *,
    job_absent: bool = False,
) -> None:
    """Phase 2 Job은 동일 이름 확인과 409 재조회 뒤에만 생성 성공으로 본다."""
    if not job_absent and jobs.get(settings.job_namespace, claim.job_name) is not None:
        return
    try:
        jobs.create(settings.job_namespace, build_executor_job(claim, settings))
    except ApiException as error:
        if (
            error.status != 409
            or jobs.get(settings.job_namespace, claim.job_name) is None
        ):
            raise


def ensure_branch_job(
    jobs: JobClient,
    claim: ClaimedExperiment,
    settings: LauncherSettings,
    *,
    job_absent: bool = False,
) -> None:
    """동일 이름 Job 존재를 확인하거나 생성하고 409는 GET 확인 뒤만 성공시킨다."""
    if (
        not job_absent
        and jobs.get(
            settings.job_namespace,
            claim.job_name,
        )
        is not None
    ):
        return
    try:
        jobs.create(
            settings.job_namespace,
            build_branch_job(claim, settings),
        )
    except ApiException as error:
        if error.status != 409:
            raise
        if jobs.get(settings.job_namespace, claim.job_name) is None:
            raise
