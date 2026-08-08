"""학습 opt-in 환경이 어느 container에 붙는지를 계약으로 고정한다(#605).

학습은 baseline(Codex 실행 **전**)과 candidate(push **후**) 두 지점에서 돈다. 그 두
container에만 데이터셋 좌표가 붙어야 하고, credential이 없는 codex-worker·verifier에는
붙지 않아야 한다. URI가 비어 있으면 아무것도 붙지 않아 executor가 기존 경로만 돈다.
"""

from __future__ import annotations

from pathlib import Path
import sys
import uuid

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_orchestration.launcher.config import (  # noqa: E402
    LauncherConfigError,
    LauncherSettings,
)
from agent_orchestration.launcher.jobs import build_executor_job  # noqa: E402
from agent_orchestration.launcher.repository import ClaimedExperiment  # noqa: E402


_EXPERIMENT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")
_DIGEST = "d3d273e66324042cd8e547068c194231cf1812d53cb68236edba56b067055293"
_DATASET_URI = f"gs://experiment-results/training-snapshots/by-hash/{_DIGEST}/"
_TRAINING_KEYS = frozenset(
    {
        "ORCH_TRAINING_DATASET_URI",
        "ORCH_TRAINING_TIMEOUT_SEC",
        "ORCH_TRAINING_DOWNLOAD_TIMEOUT_SEC",
        "ORCH_UV_SYNC_TIMEOUT_SEC",
    }
)


_MLFLOW_URI = "http://mlflow.mlflow.svc.cluster.local:5000"


def _settings(*, dataset_uri: str = "", mlflow_tracking_uri: str = "") -> LauncherSettings:
    return LauncherSettings(
        mlflow_tracking_uri=mlflow_tracking_uri,
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
        experiment_id=_EXPERIMENT_ID,
        issue_number=605,
        issue_branch="exp/605",
        base_dev_sha="a" * 40,
        job_name="ar-exec-1234567812345678123456781234",
    )


def _environment(container) -> dict[str, str]:
    return {variable.name: variable.value for variable in (container.env or [])}


def _containers(settings: LauncherSettings) -> dict[str, object]:
    job = build_executor_job(_claim(), settings)
    spec = job.spec.template.spec
    return {
        container.name: container
        for container in [*(spec.init_containers or []), *(spec.containers or [])]
    }


def test_training_environment_is_absent_when_the_dataset_uri_is_unset() -> None:
    """URI가 없으면 어느 container에도 붙지 않는다 — 학습을 켜지 않은 배포."""
    for name, container in _containers(_settings()).items():
        present = _TRAINING_KEYS & set(_environment(container))
        assert not present, f"{name}에 학습 환경이 붙었다: {sorted(present)}"


def test_training_environment_is_limited_to_the_two_training_containers() -> None:
    """baseline은 workspace-preparer, candidate는 candidate-finalizer에서 돈다."""
    containers = _containers(_settings(dataset_uri=_DATASET_URI))
    expected = {"workspace-preparer", "candidate-finalizer"}
    for name, container in containers.items():
        environment = _environment(container)
        if name in expected:
            assert _TRAINING_KEYS <= set(environment), f"{name}에 학습 환경이 부족하다"
            assert environment["ORCH_TRAINING_DATASET_URI"] == _DATASET_URI
        else:
            present = _TRAINING_KEYS & set(environment)
            assert not present, f"{name}에 학습 환경이 붙었다: {sorted(present)}"


def test_mlflow_tracking_uri_is_exported_without_the_orch_prefix() -> None:
    """`train.py`가 `os.getenv("MLFLOW_TRACKING_URI")`로 읽는다 — 접두사를 붙이면 무효다.

    launcher가 **받는** 이름은 `ORCH_MLFLOW_TRACKING_URI`이고 executor에 **내보내는**
    이름은 `MLFLOW_TRACKING_URI`다. 두 이름이 다르다.
    """
    containers = _containers(
        _settings(dataset_uri=_DATASET_URI, mlflow_tracking_uri=_MLFLOW_URI)
    )
    for name in ("workspace-preparer", "candidate-finalizer"):
        environment = _environment(containers[name])
        assert environment["MLFLOW_TRACKING_URI"] == _MLFLOW_URI
        assert "ORCH_MLFLOW_TRACKING_URI" not in environment


def test_codex_worker_never_receives_the_dataset_coordinate() -> None:
    """Codex container는 데이터 좌표를 알 필요가 없다 — 최소 노출 원칙."""
    containers = _containers(_settings(dataset_uri=_DATASET_URI))
    assert "ORCH_TRAINING_DATASET_URI" not in _environment(containers["codex-worker"])


@pytest.mark.parametrize(
    "uri",
    [
        f"gs://bucket/prefix/{_DIGEST}/",
        f"gs://bucket/by-hash/{_DIGEST[:-1]}/",
        f"https://bucket/by-hash/{_DIGEST}/",
        "gs://bucket/by-hash/NOTAHASH/",
    ],
)
def test_malformed_dataset_uri_is_rejected_at_the_launcher(uri: str) -> None:
    """형식 오류를 Pod까지 끌고 가지 않는다 — 8 container를 띄운 뒤 죽으면 원인이 묻힌다."""
    with pytest.raises(LauncherConfigError, match="invalid training_dataset_uri"):
        _settings(dataset_uri=uri)
