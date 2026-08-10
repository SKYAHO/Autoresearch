"""하네스가 에이전트에게 알리는 자원 예산의 계약을 고정한다(#656, #664).

에이전트는 자기 코드가 자원을 얼마나 써도 되는지 알 방법이 없었고, 초과는 cgroup
group-kill이라 로그 한 줄 없이 실험이 끝났다(#651).

축마다 실패 방식이 다르다 — 메모리는 죽고(group-kill), CPU는 죽지 않고 느려지며(CFS
스로틀링), 시간은 상한에서 잘린다. 예산 절이 상한만 적고 그 결과를 빼면 에이전트는
무엇을 피해야 하는지 알 수 없으므로, 값과 결과를 함께 고정한다.

이 파일이 지키는 것은 셋이다.

1. 알려진 예산은 하네스 지침에 **실제 값으로** 나타난다
2. 값이 없으면 문단이 통째로 빠진다 — 추측한 숫자를 적지 않는다
3. 숫자는 Job spec에서 오고 지침 문자열에 박히지 않는다 — 그래야 resource 정의를 바꿔도
   지침이 조용히 거짓이 되지 않는다
"""

from __future__ import annotations

from pathlib import Path
import sys
import uuid

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_orchestration.executor.prompt import (  # noqa: E402
    ResourceBudget,
    build_harness_instructions,
)
from kubernetes.client import V1ResourceRequirements  # noqa: E402

from agent_orchestration.launcher import jobs  # noqa: E402
from agent_orchestration.launcher.config import LauncherSettings  # noqa: E402
from agent_orchestration.launcher.jobs import (  # noqa: E402
    _container_resources,
    _cpu_limit_millicores,
    _memory_limit_bytes,
    _parse_cpu_millicores,
    _parse_memory_quantity,
    build_executor_job,
)
from agent_orchestration.launcher.repository import ClaimedExperiment  # noqa: E402


_DIGEST = "d3d273e66324042cd8e547068c194231cf1812d53cb68236edba56b067055293"
_DATASET_URI = f"gs://experiment-results/training-snapshots/by-hash/{_DIGEST}/"
_MEMORY_ENV = "ORCH_CONTAINER_MEMORY_LIMIT_BYTES"
_CPU_ENV = "ORCH_CONTAINER_CPU_LIMIT_MILLICORES"
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
        (),
        ResourceBudget(
            memory_limit_bytes=2 * 1024**3,
            cpu_limit_millicores=4000,
            training_timeout_seconds=1800,
        ),
    )
    assert "2.0 GiB" in text
    assert "4 vCPU" in text
    assert "1,800초" in text
    assert "자원 예산" in text


def test_cpu_budget_states_the_silent_failure_mode() -> None:
    """CPU 초과는 죽지 않고 느려지기만 한다는 사실을 알려야 한다(#664).

    메모리 초과는 group-kill이라 최소한 "죽었다"가 남지만, CPU 초과는 CFS 스로틀링이라
    로그에 흔적이 없다. 게다가 container 안의 `os.cpu_count()`는 cgroup 상한이 아니라
    노드 전체 vCPU를 반환하므로, 상한만 알리고 이 사실을 빼면 에이전트는 여전히 노드
    기준으로 스레드를 잡는다 — 알림의 실효가 사라진다.
    """
    text = build_harness_instructions((), ResourceBudget(cpu_limit_millicores=4000))

    assert "스로틀링" in text
    assert "os.cpu_count()" in text


def test_fractional_cpu_budget_is_not_rounded_away() -> None:
    """분수 코어 상한이 정수로 부풀지 않는다.

    밀리코어로 들고 있는 이유가 이것이다. Downward API를 divisor "1"로 읽으면 `500m`이
    `1`로 올림돼, container별 차등(#652)을 도입하는 순간 실제의 두 배를 알리게 된다.
    """
    text = build_harness_instructions((), ResourceBudget(cpu_limit_millicores=500))

    assert "0.5 vCPU" in text


def test_sub_100_millicore_budget_is_not_rounded_up() -> None:
    """100m 미만 상한도 표기 과정에서 실제보다 크게 알리지 않는다."""
    text = build_harness_instructions((), ResourceBudget(cpu_limit_millicores=50))

    assert "0.05 vCPU" in text


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
    assert "vCPU" not in memory_only

    time_only = build_harness_instructions((), ResourceBudget(training_timeout_seconds=600))
    assert "600초" in time_only
    assert "container당" not in time_only

    cpu_only = build_harness_instructions((), ResourceBudget(cpu_limit_millicores=4000))
    assert "4 vCPU" in cpu_only
    # 예산 절 말미의 #651 서술이 "10.3 GiB"를 언급하므로 단위가 아니라 상한 줄로 본다.
    assert "메모리: container당" not in cpu_only
    assert "seed 하나당" not in cpu_only


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


def test_experiment_job_never_uses_value_from() -> None:
    """실험 Job의 어떤 container도 `valueFrom`을 쓰지 않는다.

    admission 정책 `autoresearch-experiment-job-contract`가 `valueFrom`을 **종류 불문하고**
    금지한다(`c.env.all(e, !has(e.valueFrom))`). 시크릿이 환경 변수로 새는 경로를 닫으려는
    규칙이며, Codex가 `danger-full-access`로 도는 container에서 환경 변수는 그대로 읽힌다.

    #658이 Downward API(`resourceFieldRef`)로 예산을 넣었다가 배포 직후 launcher가 매 tick
    422로 죽었다(#665). **이 저장소가 그 계약을 스스로 검사하지 않으면 같은 사고가
    배포 시점에만 드러난다.**
    """
    job = build_executor_job(_claim(), _settings(dataset_uri=_DATASET_URI))
    spec = job.spec.template.spec
    for container in [*(spec.init_containers or []), *(spec.containers or [])]:
        assert not container.env_from, f"{container.name}에 envFrom이 있다"
        for variable in container.env or []:
            assert variable.value_from is None, (
                f"{container.name}의 {variable.name}이 valueFrom을 쓴다 — "
                "admission이 Job 생성을 거부한다"
            )


def test_memory_budget_value_comes_from_the_container_resources() -> None:
    """예산 숫자의 출처는 `_container_resources()` 하나여야 한다.

    값을 따로 적어두면 자원 정의가 바뀔 때 에이전트에게 알리는 예산만 옛 값으로 남고,
    그 거짓을 검증할 방법이 없다. `jobs.py` docstring이 저장소에 없는 `notes.md`를 근거로
    인용하고 있는 것과 같은 종류의 드리프트다(#652).
    """
    container = _codex_worker(_settings())
    variable = next(item for item in container.env if item.name == _MEMORY_ENV)
    limits = _container_resources().limits or {}
    assert variable.value == str(_memory_limit_bytes())
    assert _memory_limit_bytes() == _parse_memory_quantity(str(limits["memory"]))


def test_memory_budget_follows_a_change_to_the_container_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """자원 정의를 바꾸면 알리는 예산도 따라 바뀐다.

    현재 값과 같은 숫자를 박아 두면 위 테스트는 통과한다 — 그것이 정확히 이 기능이 막으려는
    드리프트다. 그래서 정의를 **실제로 바꿔** 예산이 따라오는지 본다. 따라오지 않으면 값이
    어딘가에 복제돼 있다는 뜻이다.
    """
    monkeypatch.setattr(
        jobs,
        "_container_resources",
        lambda: V1ResourceRequirements(
            requests={"cpu": "2", "memory": "4Gi"},
            limits={"cpu": "4", "memory": "8Gi"},
        ),
    )
    container = _codex_worker(_settings())
    memory = next(item for item in container.env if item.name == _MEMORY_ENV)
    cpu = next(item for item in container.env if item.name == _CPU_ENV)
    assert memory.value == str(8 * 1024**3)
    assert cpu.value == "4000"


def test_cpu_budget_value_comes_from_the_container_resources() -> None:
    """CPU 예산도 `_container_resources()` 하나에서 나온다."""
    container = _codex_worker(_settings())
    variable = next(item for item in container.env if item.name == _CPU_ENV)
    limits = _container_resources().limits or {}
    assert variable.value == str(_cpu_limit_millicores())
    assert _cpu_limit_millicores() == _parse_cpu_millicores(str(limits["cpu"]))


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [("4", 4000), ("2", 2000), ("500m", 500), ("1500m", 1500), ("1", 1000)],
)
def test_cpu_quantity_parsing(quantity: str, expected: int) -> None:
    """분수 코어를 정수로 부풀리지 않는다.

    코어 단위로 반올림하면 `500m` 상한이 `1`로 보고돼 에이전트가 실제의 두 배를 예산으로
    믿는다. 지금 값은 정수라 차이가 없지만, container별 차등(#652)에서 분수 코어를 주는
    순간 조용히 틀린 예산이 된다.
    """
    assert _parse_cpu_millicores(quantity) == expected


def test_unparsable_cpu_quantity_fails_loudly() -> None:
    """해석할 수 없는 CPU 표기는 조용히 0이 되지 않는다."""
    with pytest.raises(ValueError):
        _parse_cpu_millicores("4 cores")


@pytest.mark.parametrize(
    ("quantity", "expected"),
    [
        ("2Gi", 2 * 1024**3),
        ("8Gi", 8 * 1024**3),
        ("2048Mi", 2048 * 1024**2),
        ("2G", 2 * 1000**3),
        ("1536Mi", 1536 * 1024**2),
    ],
)
def test_memory_quantity_parsing(quantity: str, expected: int) -> None:
    """수량 표기가 바뀌어도 바이트 변환이 깨지지 않는다.

    binary(`Gi`)와 decimal(`G`)은 다른 배수다 — 같이 취급하면 예산이 7% 어긋난다.
    """
    assert _parse_memory_quantity(quantity) == expected


def test_unparsable_memory_quantity_fails_loudly() -> None:
    """해석할 수 없는 표기는 조용히 0이 되지 않고 예외로 끊긴다.

    잘못된 숫자를 지침에 싣는 것이 값을 싣지 않는 것보다 나쁘다 — 에이전트가 그것을
    믿고 구현한다.
    """
    with pytest.raises(ValueError):
        _parse_memory_quantity("2 gigabytes")


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
