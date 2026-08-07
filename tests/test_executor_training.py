"""단일 파드 baseline·candidate 학습의 순서 계약을 고정한다.

실제 학습을 돌리지 않고 subprocess 경계만 대역으로 바꿔, "순서가 뒤집히면 거부한다"는
계약과 seed 해석·의존성 동기화 판단을 검증한다. 학습 알고리즘 자체는
`tests/test_pipeline_*`의 범위다.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_orchestration.executor import training as training_module  # noqa: E402
from agent_orchestration.executor.training import (  # noqa: E402
    TrainingError,
    TrainingInput,
    TrainingStage,
    dependencies_changed,
    feature_definitions_changed,
    resolve_policy_seeds,
    run_training,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """clone된 저장소를 흉내 내는 빈 디렉터리."""
    repository = tmp_path / "repository"
    repository.mkdir()
    return repository


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    path = tmp_path / "training_dataset.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    return path


@pytest.fixture
def state_directory(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    path.mkdir()
    return path


def _stub_runs(monkeypatch: pytest.MonkeyPatch, *, seeds: str = "42,43,44") -> list[list[str]]:
    """`_run`을 대역으로 바꾸고 호출된 argv를 순서대로 모은다."""
    calls: list[list[str]] = []

    def _fake_run(argv: list[str], *, cwd: Path, timeout_seconds: int) -> str:
        calls.append(argv)
        return seeds if argv[:2] == ["python", "-c"] else ""

    monkeypatch.setattr(training_module, "_run", _fake_run)
    return calls


def _input(stage: TrainingStage, workspace: Path, dataset: Path, state: Path) -> TrainingInput:
    return TrainingInput(
        stage=stage,
        workspace=workspace,
        dataset_path=dataset,
        state_directory=state,
        timeout_seconds=600,
    )


def test_candidate_training_is_refused_before_baseline(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, dataset: Path, state_directory: Path
) -> None:
    """순서가 뒤집히면 학습을 시작하지 않는다.

    baseline이 candidate 의존성으로 돌면 두 조건의 차이가 코드 변경만이 아니게 되어
    paired 대조의 전제가 깨진다. 재시도로 순서가 어긋나는 경로까지 막아야 한다.
    """
    _stub_runs(monkeypatch)

    with pytest.raises(TrainingError, match="baseline_training_missing"):
        run_training(_input(TrainingStage.CANDIDATE, workspace, dataset, state_directory))


def test_baseline_then_candidate_succeeds_and_leaves_a_marker(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, dataset: Path, state_directory: Path
) -> None:
    _stub_runs(monkeypatch)

    baseline_seeds = run_training(
        _input(TrainingStage.BASELINE, workspace, dataset, state_directory)
    )
    assert baseline_seeds == (42, 43, 44)
    assert (state_directory / "baseline_training_complete").is_file()

    candidate_seeds = run_training(
        _input(TrainingStage.CANDIDATE, workspace, dataset, state_directory)
    )
    assert candidate_seeds == (42, 43, 44)


def test_training_runs_once_per_policy_seed(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, dataset: Path, state_directory: Path
) -> None:
    calls = _stub_runs(monkeypatch, seeds="7,8")

    run_training(_input(TrainingStage.BASELINE, workspace, dataset, state_directory))

    train_calls = [argv for argv in calls if "train-model" in argv]
    assert len(train_calls) == 2
    assert [argv[argv.index("--model-seed") + 1] for argv in train_calls] == ["7", "8"]


def test_seeds_come_from_the_workspace_not_a_local_constant(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    """seed 정책은 workspace 코드가 정한다.

    executor 이미지에 `src/`가 없어 import가 불가능하고, 상수를 복제하면 세 번째 사본이
    되어 드리프트가 생긴다. candidate가 seed 정책을 바꾸는 실험이라면 candidate 학습은
    바뀐 값으로 돌아야 하므로, workspace에게 직접 묻는 것이 의미상으로도 맞다.
    """
    _stub_runs(monkeypatch, seeds="1,2,3,4,5")

    assert resolve_policy_seeds(workspace) == (1, 2, 3, 4, 5)


@pytest.mark.parametrize("raw", ["", "   ", "not-a-seed"])
def test_malformed_seed_probe_output_is_rejected(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, raw: str
) -> None:
    """seed를 못 읽으면 학습을 시작하지 않는다 — 조용히 기본값으로 돌지 않는다."""
    _stub_runs(monkeypatch, seeds=raw)

    with pytest.raises(TrainingError, match="policy_seeds_"):
        resolve_policy_seeds(workspace)


def test_dependency_change_detection_uses_git_diff(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    calls: list[list[str]] = []

    def _fake_run(argv: list[str], *, cwd: Path, timeout_seconds: int) -> str:
        calls.append(argv)
        return "uv.lock\n"

    monkeypatch.setattr(training_module, "_run", _fake_run)

    assert dependencies_changed(workspace, base_ref="abc123") is True
    assert calls[0][:4] == ["git", "diff", "--name-only", "abc123"]
    assert "pyproject.toml" in calls[0]
    assert "uv.lock" in calls[0]


def test_no_dependency_change_skips_sync(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    monkeypatch.setattr(
        training_module, "_run", lambda argv, *, cwd, timeout_seconds: "\n"
    )

    assert dependencies_changed(workspace, base_ref="abc123") is False


def test_missing_dataset_is_rejected_before_spawning(
    workspace: Path, state_directory: Path, tmp_path: Path
) -> None:
    with pytest.raises(TrainingError, match="dataset_missing"):
        TrainingInput(
            stage=TrainingStage.BASELINE,
            workspace=workspace,
            dataset_path=tmp_path / "absent.csv",
            state_directory=state_directory,
            timeout_seconds=600,
        )


def test_feature_definition_change_is_detected(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    """피처 정의가 바뀌면 바뀐 경로를 돌려준다.

    스냅샷은 baseline 코드 기준으로 얼려져 있어 새 피처가 CSV에 없다. 그런데 학습은
    에러 없이 통과하고 candidate가 baseline과 같은 데이터로 학습된다 — 실패로 보이지
    않으면서 아무것도 검증하지 않은 상태가 된다. 그래서 감지가 필요하다.
    """
    calls: list[list[str]] = []

    def _fake_run(argv: list[str], *, cwd: Path, timeout_seconds: int) -> str:
        calls.append(argv)
        return "feature_repo/feature_definitions.py\n"

    monkeypatch.setattr(training_module, "_run", _fake_run)

    changed = feature_definitions_changed(workspace, base_ref="abc123")

    assert changed == ("feature_repo/feature_definitions.py",)
    assert calls[0][:4] == ["git", "diff", "--name-only", "abc123"]
    assert "feature_repo/feature_definitions.py" in calls[0]


def test_unchanged_feature_definitions_return_empty(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    """피처 불변 가설은 빈 tuple로 통과시킨다."""
    monkeypatch.setattr(
        training_module, "_run", lambda argv, *, cwd, timeout_seconds: "\n"
    )

    assert feature_definitions_changed(workspace, base_ref="abc123") == ()
