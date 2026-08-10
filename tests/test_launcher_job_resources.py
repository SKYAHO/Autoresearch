"""Phase 2 Job이 요청하는 자원 값을 인프라 계약으로 고정한다.

역할 분담(`SKYAHO/Autoresearch-infra#562`): 인프라는 namespace LimitRange·Quota로
**상한**을 정하고, launcher는 Job이 실제로 얼마를 **요청**할지 명시한다. 명시하지 않으면
LimitRange 기본값(limit 1Gi)이 적용돼 학습 단계가 OOM으로 죽으므로, "모든 container에
자원이 붙어 있다"를 테스트로 고정한다.

**이 파일이 고정하지 못하는 것**: 아래 상수는 인프라의 실제 배포값을 읽지 않고 손으로
베껴 둔 사본이다. 인프라가 LimitRange·Quota를 바꿔도 이 테스트는 그대로 통과하므로,
"계약을 고정한다"는 말은 **이 저장소가 그 계약을 어기지 않는다**까지만 참이다. 인프라
변경(`SKYAHO/Autoresearch-infra#624` 같은)이 있으면 여기를 같은 PR에서 손으로 맞춰야
한다. 자동 검증이 필요해지면 배포된 매니페스트를 읽는 별도 경로가 있어야 한다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
import sys
import uuid

from kubernetes.client import V1PodSpec
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_orchestration.launcher.config import LauncherSettings  # noqa: E402
from agent_orchestration.launcher.jobs import build_executor_job  # noqa: E402
from agent_orchestration.launcher.repository import ClaimedExperiment  # noqa: E402


_EXPERIMENT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")

# 인프라 LimitRange가 허용하는 컨테이너 상한이다. 이 값을 넘기면 admission이 거부한다.
_NAMESPACE_CONTAINER_MAX_MEMORY = "8Gi"
_NAMESPACE_CONTAINER_MAX_CPU = "4"

# 동시 5건 기준 namespace ResourceQuota의 requests 항목이다(`Autoresearch-infra#624`).
_QUOTA_REQUESTS_MEMORY_MIB = 10 * 1024
_QUOTA_REQUESTS_CPU_MILLICORES = 5 * 1000


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
        max_concurrent_experiments=5,
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
        assert container.resources.requests["memory"] == "2Gi", container.name
        assert container.resources.requests["cpu"] == "1", container.name


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
    곱해지지 않는다.

    이 계산이 깨지면 상한째 Job의 **생성 자체가 403으로 거부된다**(Pending이 아니다).
    launcher는 409만 흡수하므로 403은 tick 전체를 실패시키는데, 그 시점에는 이미 DB에
    `RUNNING`으로 커밋된 뒤다 — 워크벤치에는 실행 중으로 보이지만 Job이 없는 실험이
    남는다. CPU와 메모리 중 **하나만 넘겨도** 같은 일이 벌어지므로 둘 다 검사한다.
    """
    settings = _settings()
    job = build_executor_job(_claim(), settings)
    spec = job.spec.template.spec

    memory = _effective_request(spec, "memory", _mebibytes)
    assert memory * settings.max_concurrent_experiments <= _QUOTA_REQUESTS_MEMORY_MIB

    cpu = _effective_request(spec, "cpu", _millicores)
    assert cpu * settings.max_concurrent_experiments <= _QUOTA_REQUESTS_CPU_MILLICORES


def _effective_request(
    spec: V1PodSpec, resource: str, parse: Callable[[str], int]
) -> int:
    """Pod 실효 요청 = `max(앱 container 합계, 각 initContainer의 최댓값)`."""
    app_total = sum(parse(c.resources.requests[resource]) for c in spec.containers)
    init_max = max(parse(c.resources.requests[resource]) for c in spec.init_containers)
    return max(app_total, init_max)


def _mebibytes(value: str) -> int:
    if value.endswith("Mi"):
        return int(value[:-2])
    if value.endswith("Gi"):
        return int(value[:-2]) * 1024
    raise AssertionError(f"예상하지 못한 메모리 단위: {value}")


def _millicores(value: str) -> int:
    if value.endswith("m"):
        return int(value[:-1])
    try:
        return int(value) * 1000
    except ValueError as error:
        raise AssertionError(f"예상하지 못한 CPU 단위: {value}") from error


def test_cpu_parser_reports_an_unexpected_unit() -> None:
    with pytest.raises(AssertionError, match="예상하지 못한 CPU 단위: 0.5"):
        _millicores("0.5")


def test_job_deadline_is_carried_from_settings() -> None:
    """자원 변경이 기존 deadline 계약을 건드리지 않았는지 함께 고정한다."""
    settings = _settings()
    job = build_executor_job(_claim(), settings)

    assert job.spec.active_deadline_seconds == settings.active_deadline_sec
    assert isinstance(datetime.now(UTC), datetime)
