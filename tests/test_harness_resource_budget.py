"""하네스가 에이전트에게 알리는 자원 예산의 계약을 고정한다(#656).

에이전트는 자기 코드가 자원을 얼마나 써도 되는지 알 방법이 없었고, 초과는 cgroup
group-kill이라 로그 한 줄 없이 실험이 끝났다(#651). 이 파일이 지키는 것은 셋이다.

1. 알려진 예산은 하네스 지침에 **실제 값으로** 나타난다
2. 값이 없으면 문단이 통째로 빠진다 — 추측한 숫자를 적지 않는다
3. 숫자는 Job spec에서 오고 지침 문자열에 박히지 않는다 — 그래야 resource 정의를 바꿔도
   지침이 조용히 거짓이 되지 않는다
"""

from __future__ import annotations

from pathlib import Path
import sys
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_orchestration.executor.prompt import (  # noqa: E402
    ResourceBudget,
    build_harness_instructions,
)
from agent_orchestration.launcher.config import LauncherSettings  # noqa: E402
from agent_orchestration.launcher.jobs import build_executor_job  # noqa: E402
from agent_orchestration.launcher.repository import ClaimedExperiment  # noqa: E402


_DIGEST = "d3d273e66324042cd8e547068c194231cf1812d53cb68236edba56b067055293"
_DATASET_URI = f"gs://experiment-results/training-snapshots/by-hash/{_DIGEST}/"
_MEMORY_ENV = "ORCH_CONTAINER_MEMORY_LIMIT_BYTES"
_TIMEOUT_ENV = "ORCH_BUDGET_TRAINING_TIMEOUT_SEC"


def _settings(*, dataset_uri: str = "") -> LauncherSettings:
    return LauncherSettings(
        mlflow_tracking_uri="",
        experiment_results_root="",
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
        training_dataset_uri=dataset_uri,
    )


def _claim() -> ClaimedExperiment:
    return ClaimedExperiment(
        experiment_id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        issue_number=656,
        issue_branch="exp/656",
        base_dev_sha="a" * 40,
        job_name="ar-exec-1234567812345678123456781234",
    )


def _codex_worker(settings: LauncherSettings):
    job = build_executor_job(_claim(), settings)
    spec = job.spec.template.spec
    containers = [*(spec.init_containers or []), *(spec.containers or [])]
    return next(container for container in containers if container.name == "codex-worker")


# --- 지침 렌더 ---------------------------------------------------------------


def test_known_budget_appears_in_the_harness_instructions() -> None:
    """알려진 상한은 사람이 읽는 표기로 지침에 나타난다."""
    text = build_harness_instructions(
        (), ResourceBudget(memory_limit_bytes=2 * 1024**3, training_timeout_seconds=1800)
    )
    assert "2.0 GiB" in text
    assert "1,800초" in text
    assert "자원 예산" in text


def test_budget_section_is_omitted_when_nothing_is_known() -> None:
    """예산 환경이 없는 배포에서는 문단이 통째로 빠지고 나머지는 그대로다.

    모르는 값을 추측해 적는 것보다 말하지 않는 편이 낫다 — 틀린 예산을 믿고 구현하면
    이 기능이 풀려는 문제가 되살아난다.
    """
    text = build_harness_instructions((), ResourceBudget())
    assert "자원 예산" not in text
    # 기존 절은 영향을 받지 않는다.
    assert "## 무결성" in text
    assert "## 실험 공간의 알려진 제약: ONNX 변환" in text
    assert text == build_harness_instructions(())


def test_partial_budget_reports_only_the_known_value() -> None:
    """한쪽만 알면 그쪽만 적는다. 모르는 축을 지어내지 않는다."""
    memory_only = build_harness_instructions(
        (), ResourceBudget(memory_limit_bytes=4 * 1024**3)
    )
    assert "4.0 GiB" in memory_only
    assert "seed 하나당" not in memory_only

    time_only = build_harness_instructions((), ResourceBudget(training_timeout_seconds=600))
    assert "600초" in time_only
    assert "container당" not in time_only


def test_budget_section_states_the_consequence_not_an_implementation_rule() -> None:
    """구현 지시가 아니라 환경 서술이어야 한다.

    프롬프트에 금지·의무 조항을 쌓으면 실험 탐색 공간이 좁아진다. 예산 절은 상한과
    초과 시 벌어지는 일을 알릴 뿐, 어떤 자료구조를 쓰라고 지시하지 않는다.
    """
    text = build_harness_instructions(
        (), ResourceBudget(memory_limit_bytes=2 * 1024**3, training_timeout_seconds=1800)
    )
    assert "SIGKILL" in text
    for forbidden in ("sparse를 사용하십시오", "one-hot을 사용하지", "float32를 사용"):
        assert forbidden not in text


# --- Job spec 배선 -----------------------------------------------------------


def test_memory_budget_is_read_from_the_job_spec_not_hardcoded() -> None:
    """Downward API로 실제 `limits.memory`를 읽어야 드리프트가 없다.

    숫자를 코드에 박으면 `_container_resources()`나 infra LimitRange가 바뀔 때 지침이
    조용히 거짓이 되고, 그 거짓을 검증할 방법이 없다.
    """
    container = _codex_worker(_settings())
    variable = next(item for item in container.env if item.name == _MEMORY_ENV)
    assert variable.value is None, "리터럴 값이면 spec 변경을 따라가지 못한다"
    selector = variable.value_from.resource_field_ref
    assert selector.resource == "limits.memory"
    assert selector.divisor == "1", "바이트로 받아야 표기 변환이 한 곳에 모인다"


def test_memory_budget_reads_the_training_container_not_the_codex_container() -> None:
    """읽는 대상은 학습이 실제로 도는 container여야 한다.

    `container_name`을 생략하면 자기 자신(codex-worker)을 가리킨다. 지금은 8개 container가
    같은 자원을 받아 두 값이 같지만, container별 차등을 도입하면(#652) 코드를 쓰는
    container는 작게, 학습 container는 크게 주는 것이 자연스럽다. 그때 자기 값을 읽는
    구현은 **조용히 틀린 예산**을 알리고, 에이전트는 검증할 수 있었던 가설을 스스로
    축소한다.
    """
    container = _codex_worker(_settings())
    variable = next(item for item in container.env if item.name == _MEMORY_ENV)
    selector = variable.value_from.resource_field_ref
    assert selector.container_name == "candidate-finalizer"


def test_the_referenced_budget_container_exists_in_the_job() -> None:
    """참조 이름이 실제 container와 일치해야 한다.

    이름이 틀리면 **API 검증은 통과하고 Pod 시작 시점에** 죽는다 — 실험이 통째로 실패하는데
    원인은 매니페스트 오타다. dev 클러스터 실물 Pod으로 initContainer가 app container의
    상한을 읽을 수 있음을 확인했고(256Mi → 268435456), 여기서는 이름 일치만 고정한다.
    """
    job = build_executor_job(_claim(), _settings())
    spec = job.spec.template.spec
    names = {
        container.name
        for container in [*(spec.init_containers or []), *(spec.containers or [])]
    }
    worker = _codex_worker(_settings())
    variable = next(item for item in worker.env if item.name == _MEMORY_ENV)
    referenced = variable.value_from.resource_field_ref.container_name
    assert referenced in names, f"참조한 container가 Job에 없다: {referenced}"


def test_time_budget_follows_the_training_opt_in() -> None:
    """학습이 꺼진 배포에서는 시간 예산도 알리지 않는다."""
    assert _TIMEOUT_ENV not in {item.name for item in _codex_worker(_settings()).env}

    enabled = _codex_worker(_settings(dataset_uri=_DATASET_URI))
    variable = next(item for item in enabled.env if item.name == _TIMEOUT_ENV)
    assert variable.value == "1800"


def test_budget_disclosure_does_not_reuse_the_enforced_training_variable() -> None:
    """예산 고지는 `ORCH_TRAINING_TIMEOUT_SEC`을 재사용하지 않는다(#605 계약).

    그 이름은 학습 container가 **집행하는** 값이고 학습 좌표와 함께 두 container에만
    간다. codex-worker에 같은 이름을 붙이면 "학습 환경은 두 container에만"이라는 계약이
    흐려진다. 숫자가 어긋나지 않는 것은 같은 settings 필드에서 나온다는 사실이 보장한다.
    """
    names = {item.name for item in _codex_worker(_settings(dataset_uri=_DATASET_URI)).env}
    assert "ORCH_TRAINING_TIMEOUT_SEC" not in names
    assert "ORCH_TRAINING_DATASET_URI" not in names
    assert _TIMEOUT_ENV in names
