"""Phase 2 Job이 요청하는 자원 값을 인프라 계약으로 고정한다.

역할 분담(`SKYAHO/Autoresearch-infra#562`): 인프라는 namespace LimitRange·Quota로
**상한**을 정하고, launcher는 Job이 실제로 얼마를 **요청**할지 명시한다. 명시하지 않으면
LimitRange 기본값(limit 1Gi)이 적용돼 학습 단계가 OOM으로 죽으므로, "모든 container에
자원이 붙어 있다"를 테스트로 고정한다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sys
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_orchestration.launcher.config import LauncherSettings  # noqa: E402
from agent_orchestration.launcher.jobs import build_executor_job  # noqa: E402
from agent_orchestration.launcher.repository import ClaimedExperiment  # noqa: E402


_EXPERIMENT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")

# 인프라 LimitRange가 허용하는 컨테이너 상한이다. 이 값을 넘기면 admission이 거부한다.
_NAMESPACE_CONTAINER_MAX_MEMORY = "2Gi"
_NAMESPACE_CONTAINER_MAX_CPU = "1"


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
        issue_number=574,
        issue_branch="exp/574-demo",
        base_dev_sha="a" * 40,
        job_name=f"ar-exec-{_EXPERIMENT_ID.hex}",
    )


def _all_containers(job) -> list:
    spec = job.spec.template.spec
    return list(spec.init_containers or []) + list(spec.containers or [])


def test_every_container_declares_resources() -> None:
    """자원을 명시하지 않은 container가 하나라도 있으면 안 된다.

    미명시 container는 namespace LimitRange 기본값(limit 1Gi)을 받는다. 조립 피크가
    1.13 GiB, 학습 피크가 1.22 GiB라 그 container에서 OOM으로 죽는다.
    """
    job = build_executor_job(_claim(), _settings())

    containers = _all_containers(job)
    assert containers, "Phase 2 Job에 container가 없다"
    missing = [c.name for c in containers if c.resources is None]
    assert not missing, f"자원 미명시 container: {missing}"


def test_requests_cover_the_measured_peaks() -> None:
    """요청 메모리가 실측 피크를 덮는다.

    조립 1.13 GiB, 학습 1.22 GiB
    (`experiments/2026-08-07_demo-window-assembly-memory/notes.md`). 요청이 이보다
    작으면 QoS가 Burstable로 떨어져 노드 압박 시 eviction 대상이 된다. 1Gi(=1024Mi)로는
    두 단계 모두 부족하므로 그 이상이어야 한다.
    """
    job = build_executor_job(_claim(), _settings())

    for container in _all_containers(job):
        assert container.resources.requests["memory"] == "1536Mi", container.name


def test_limits_stay_within_the_namespace_ceiling() -> None:
    """상한이 인프라 LimitRange 최대치와 같다.

    이 값을 넘기면 admission이 Job 생성을 거부한다. 인프라(#562)가 정하는 값이므로
    여기서 임의로 올리면 안 되고, 올려야 하면 인프라 변경이 선행되어야 한다.
    """
    job = build_executor_job(_claim(), _settings())

    for container in _all_containers(job):
        limits = container.resources.limits
        assert limits["memory"] == _NAMESPACE_CONTAINER_MAX_MEMORY, container.name
        assert limits["cpu"] == _NAMESPACE_CONTAINER_MAX_CPU, container.name


def test_concurrent_jobs_fit_the_namespace_quota() -> None:
    """동시 실행 상한만큼 띄워도 namespace quota 안에 들어온다.

    Pod 실효 요청은 `max(앱 container 합계, 각 initContainer의 최댓값)`이다.
    initContainer는 순차 실행이고 sidecar(`restartPolicy: Always`)가 없으므로 개수만큼
    곱해지지 않는다. 이 계산이 깨지면 두 번째 Job이 quota에 걸려 Pending으로 남는다.
    """
    settings = _settings()
    job = build_executor_job(_claim(), settings)
    spec = job.spec.template.spec

    app_total = sum(_mebibytes(c.resources.requests["memory"]) for c in spec.containers)
    init_max = max(
        _mebibytes(c.resources.requests["memory"]) for c in spec.init_containers
    )
    effective = max(app_total, init_max)

    quota_requests_memory_mib = 4 * 1024
    assert effective * settings.max_concurrent_experiments <= quota_requests_memory_mib


def _mebibytes(value: str) -> int:
    if value.endswith("Mi"):
        return int(value[:-2])
    if value.endswith("Gi"):
        return int(value[:-2]) * 1024
    raise AssertionError(f"예상하지 못한 메모리 단위: {value}")


def test_job_deadline_is_carried_from_settings() -> None:
    """자원 변경이 기존 deadline 계약을 건드리지 않았는지 함께 고정한다."""
    settings = _settings()
    job = build_executor_job(_claim(), settings)

    assert job.spec.active_deadline_seconds == settings.active_deadline_sec
    assert isinstance(datetime.now(UTC), datetime)
