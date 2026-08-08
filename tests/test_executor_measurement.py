"""조건별 학습 산출물을 채점해 실험 지표로 모으는 계약을 고정한다.

실제 평가를 돌리지 않고 subprocess 경계만 대역으로 바꾼다 — 지표의 정의와 계산은
`src/pipeline/evaluate.py`가 소유하고 `tests/test_pipeline_evaluate.py`가 검증한다.
여기서 지키는 것은 "무엇을 호출하고 무엇을 남기는가"다.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_orchestration.executor import measurement as measurement_module  # noqa: E402
from agent_orchestration.executor.measurement import (  # noqa: E402
    MeasurementError,
    MeasurementInput,
    build_experiment_metrics,
    evaluate_condition,
    write_experiment_metrics,
)
from agent_orchestration.executor.training import TrainingStage  # noqa: E402


SEEDS = (42, 43)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """clone된 저장소를 흉내 내는 디렉터리."""
    repository = tmp_path / "repository"
    repository.mkdir()
    return repository


@pytest.fixture
def output_root(tmp_path: Path) -> Path:
    """clone 밖 산출물 루트. 실제 Pod 배치와 같다(#603)."""
    root = tmp_path / "training-output"
    root.mkdir()
    return root


def _write_training_artifacts(
    output_root: Path, stage: TrainingStage, *, test_set_body: str = "clicked\n1\n"
) -> Path:
    """학습이 남기는 seed별 파일 4종을 흉내 낸다."""
    directory = output_root / stage.value
    directory.mkdir(parents=True, exist_ok=True)
    for seed in SEEDS:
        (directory / f"model_{seed}.txt").write_text("model", encoding="utf-8")
        (directory / f"test_{seed}.csv").write_text(test_set_body, encoding="utf-8")
        (directory / f"features_{seed}.json").write_text("[]", encoding="utf-8")
        (directory / f"categories_{seed}.json").write_text("{}", encoding="utf-8")
    return directory


def _stub_evaluation(
    monkeypatch: pytest.MonkeyPatch, values: dict[str, dict[int, float]]
) -> list[list[str]]:
    """`_run`을 대역으로 바꾸고, 호출 argv의 `--metrics-output`에 지표를 써 둔다.

    실제 `evaluate-model`이 하는 일(파일 기록)만 흉내 낸다.
    """
    calls: list[list[str]] = []

    def _fake_run(argv: list[str], *, cwd: Path, timeout_seconds: int) -> None:
        calls.append(argv)
        destination = Path(argv[argv.index("--metrics-output") + 1])
        seed = int(destination.stem.rsplit("_", 1)[1])
        stage = destination.parent.name
        destination.write_text(
            json.dumps(
                {
                    "contract_version": "held-out-metrics-v1",
                    "roc_auc": values["roc_auc"][seed]
                    + (0.01 if stage == TrainingStage.CANDIDATE.value else 0.0),
                    "log_loss": values["log_loss"][seed],
                    "brier": values["brier"][seed],
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(measurement_module, "_run", _fake_run)
    return calls


_DEFAULT_VALUES = {
    "roc_auc": {42: 0.78, 43: 0.77},
    "log_loss": {42: 0.087, 43: 0.088},
    "brier": {42: 0.013, 43: 0.014},
}


def test_evaluate_condition_calls_evaluate_model_once_per_seed(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, output_root: Path
) -> None:
    """seed마다 한 번씩, 학습이 남긴 산출물 경로를 그대로 넘겨 채점한다."""
    _write_training_artifacts(output_root, TrainingStage.BASELINE)
    calls = _stub_evaluation(monkeypatch, _DEFAULT_VALUES)

    collected = evaluate_condition(
        MeasurementInput(
            workspace=workspace,
            output_root=output_root,
            seeds=SEEDS,
            timeout_seconds=60,
        ),
        TrainingStage.BASELINE,
    )

    assert sorted(collected) == list(SEEDS)
    assert len(calls) == len(SEEDS)
    argv = calls[0]
    assert argv[:4] == ["python", "-m", "src.cli", "evaluate-model"]
    # 학습이 만든 테스트셋으로 채점해야 한다 — 전체 데이터셋으로 재면 학습에 쓴 행이
    # 섞여 지표가 부풀려진다.
    assert argv[argv.index("--data-path") + 1].endswith("baseline/test_42.csv")
    assert argv[argv.index("--model-path") + 1].endswith("baseline/model_42.txt")


def test_evaluate_condition_rejects_missing_training_artifact(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, output_root: Path
) -> None:
    """산출물이 없으면 채점을 시작하지 않는다.

    없는 경로로 subprocess를 띄우면 실패 사유가 "평가 명령 실패"로 뭉개져, 학습이
    안 돈 것인지 평가가 깨진 것인지 구분할 수 없다.
    """
    directory = _write_training_artifacts(output_root, TrainingStage.BASELINE)
    (directory / "model_43.txt").unlink()
    _stub_evaluation(monkeypatch, _DEFAULT_VALUES)

    with pytest.raises(MeasurementError, match="training_artifact_missing"):
        evaluate_condition(
            MeasurementInput(
                workspace=workspace,
                output_root=output_root,
                seeds=SEEDS,
                timeout_seconds=60,
            ),
            TrainingStage.BASELINE,
        )


def test_build_experiment_metrics_pairs_conditions_by_seed(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, output_root: Path
) -> None:
    """같은 seed끼리 짝지어 delta를 낸다.

    짝을 지어야 seed가 만드는 변동(분할·초기화·샘플링)이 상쇄되고 코드 변경의 효과만
    남는다. 조건별 평균을 따로 낸 뒤 빼면 그 성질이 사라진다.
    """
    _write_training_artifacts(output_root, TrainingStage.BASELINE)
    _write_training_artifacts(output_root, TrainingStage.CANDIDATE)
    _stub_evaluation(monkeypatch, _DEFAULT_VALUES)

    payload = build_experiment_metrics(
        MeasurementInput(
            workspace=workspace,
            output_root=output_root,
            seeds=SEEDS,
            timeout_seconds=60,
        ),
        coordinates={"experiment_id": "abc", "issue_number": 619},
        dataset_fingerprint="d" * 64,
    )

    roc = payload["paired"]["roc_auc"]
    # 대역이 candidate에만 +0.01을 더한다.
    assert roc["per_seed"][42] == pytest.approx(0.01)
    assert roc["per_seed"][43] == pytest.approx(0.01)
    assert roc["mean"] == pytest.approx(0.01)
    assert payload["coordinates"]["issue_number"] == 619
    assert payload["seeds"] == list(SEEDS)


def test_build_experiment_metrics_reports_split_mismatch(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, output_root: Path
) -> None:
    """두 조건의 테스트셋이 다르면 그 사실을 싣는다.

    분할 코드도 candidate가 바꿀 수 있는 `src/**`다. 분할이 갈리면 두 숫자는 애초에
    비교 대상이 아닌데 **지표는 멀쩡해 보인다** — 리크보다 알아채기 어려우므로 사실을
    남겨야 읽는 쪽이 "이 비교는 성립하지 않는다"를 말할 수 있다.
    """
    _write_training_artifacts(output_root, TrainingStage.BASELINE)
    _write_training_artifacts(
        output_root, TrainingStage.CANDIDATE, test_set_body="clicked\n0\n"
    )
    _stub_evaluation(monkeypatch, _DEFAULT_VALUES)

    payload = build_experiment_metrics(
        MeasurementInput(
            workspace=workspace,
            output_root=output_root,
            seeds=SEEDS,
            timeout_seconds=60,
        ),
        coordinates={},
        dataset_fingerprint="d" * 64,
    )

    assert payload["split_matches"] == {"42": False, "43": False}


def test_build_experiment_metrics_marks_split_match_when_identical(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, output_root: Path
) -> None:
    """같은 테스트셋이면 일치로 표시한다 — 불일치 보고가 상시 참이 아님을 고정한다."""
    _write_training_artifacts(output_root, TrainingStage.BASELINE)
    _write_training_artifacts(output_root, TrainingStage.CANDIDATE)
    _stub_evaluation(monkeypatch, _DEFAULT_VALUES)

    payload = build_experiment_metrics(
        MeasurementInput(
            workspace=workspace,
            output_root=output_root,
            seeds=SEEDS,
            timeout_seconds=60,
        ),
        coordinates={},
        dataset_fingerprint="d" * 64,
    )

    assert payload["split_matches"] == {"42": True, "43": True}


def test_paired_summary_leaves_standard_error_null_for_single_seed(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, output_root: Path
) -> None:
    """seed가 하나면 표준오차를 0이 아니라 null로 남긴다.

    0으로 채우면 "변동이 없다"로 읽혀 신뢰도를 과장한다. 계산하지 않았음이 드러나야
    한다.
    """
    _write_training_artifacts(output_root, TrainingStage.BASELINE)
    _write_training_artifacts(output_root, TrainingStage.CANDIDATE)
    _stub_evaluation(monkeypatch, _DEFAULT_VALUES)

    payload = build_experiment_metrics(
        MeasurementInput(
            workspace=workspace,
            output_root=output_root,
            seeds=(42,),
            timeout_seconds=60,
        ),
        coordinates={},
        dataset_fingerprint="d" * 64,
    )

    assert payload["paired"]["roc_auc"]["standard_error"] is None


def test_write_experiment_metrics_leaves_no_partial_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """게시 도중 실패하면 부분 파일도 임시 파일도 남기지 않는다."""
    destination = tmp_path / "result" / "metrics.json"

    def _explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", _explode)

    with pytest.raises(MeasurementError, match="metrics_publish_failed"):
        write_experiment_metrics({"a": 1}, destination)

    assert not destination.exists()
    assert list(destination.parent.iterdir()) == []


def test_measurement_input_rejects_empty_seeds(
    workspace: Path, output_root: Path
) -> None:
    """seed가 없으면 채점을 시작하지 않는다 — 빈 결과가 "측정했다"로 오인된다."""
    with pytest.raises(MeasurementError, match="seeds_missing"):
        MeasurementInput(
            workspace=workspace,
            output_root=output_root,
            seeds=(),
            timeout_seconds=60,
        )
