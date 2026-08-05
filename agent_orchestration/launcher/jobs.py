"""실험 branch-bootstrap Kubernetes Job manifest와 API 경계.

[파이프라인] launcher가 DB에서 봉인 좌표를 선점한 뒤 executor init/app container를
기동해 exp branch 생성을 위임하는 구간을 담당한다.

[기능] digest 고정 image 하나로 token-minter와 branch-bootstrap container를 조립하고,
label 기반 active Job 계산·GET·create 및 409 후 동일 이름 재확인을 제공한다.

[비책임] Experiment 선점·생성 확인 시각 저장(`launcher.repository`), Secret 값 보관과
RBAC/admission/egress(Autoresearch-infra), GitHub token·ref 처리(`executor`)는 담당하지
않는다.
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


BRANCH_BOOTSTRAP_LABEL_SELECTOR = (
    "app.kubernetes.io/component=branch-bootstrap"
)
_PRIVATE_KEY_DIRECTORY = "/var/run/secrets/github-app"
_PRIVATE_KEY_FILE = f"{_PRIVATE_KEY_DIRECTORY}/private-key.pem"
_TOKEN_DIRECTORY = "/var/run/github-token"
_TOKEN_FILE = f"{_TOKEN_DIRECTORY}/token"
_EXECUTOR_USER_ID = 10001


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


def build_branch_job(
    claim: ClaimedExperiment,
    settings: LauncherSettings,
) -> V1Job:
    """봉인 좌표와 시크릿 참조만 가진 결정론적 executor Job을 만든다."""
    labels = {
        "app.kubernetes.io/component": "branch-bootstrap",
    }
    token_minter = V1Container(
        name="github-token-minter",
        image=settings.executor_image,
        command=[
            "python",
            "-m",
            "agent_orchestration.executor.token_minter",
        ],
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
                name="github-token",
                mount_path=_TOKEN_DIRECTORY,
                read_only=False,
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
                name="github-token",
                mount_path=_TOKEN_DIRECTORY,
                read_only=True,
            )
        ],
    )
    pod_spec = V1PodSpec(
        automount_service_account_token=False,
        service_account_name=settings.executor_service_account,
        node_selector={
            "cloud.google.com/gke-nodepool": settings.executor_node_pool,
        },
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
                empty_dir=V1EmptyDirVolumeSource(
                    medium="Memory",
                    size_limit="1Mi",
                ),
            ),
        ],
    )
    return V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=V1ObjectMeta(
            name=claim.job_name,
            namespace=settings.job_namespace,
            labels=labels,
        ),
        spec=V1JobSpec(
            backoff_limit=0,
            active_deadline_seconds=settings.active_deadline_sec,
            ttl_seconds_after_finished=settings.ttl_after_finished_sec,
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(labels=labels),
                spec=pod_spec,
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


def ensure_branch_job(
    jobs: JobClient,
    claim: ClaimedExperiment,
    settings: LauncherSettings,
) -> None:
    """동일 이름 Job 존재를 확인하거나 생성하고 409는 GET 확인 뒤만 성공시킨다."""
    if jobs.get(settings.job_namespace, claim.job_name) is not None:
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
