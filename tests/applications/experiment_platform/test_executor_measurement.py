"""조건별 학습 산출물을 채점해 실험 지표로 모으는 계약을 고정한다.

실제 평가를 돌리지 않고 subprocess 경계만 대역으로 바꾼다 — 지표의 정의와 계산은
`autoresearch/model_evaluation/evaluate.py`가 소유하고 `tests/test_pipeline_evaluate.py`가 검증한다.
여기서 지키는 것은 "무엇을 호출하고 무엇을 남기는가"다.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from applications.experiment_platform.executor import measurement as measurement_module  # noqa: E402
from applications.experiment_platform.executor.measurement import (  # noqa: E402
    MeasurementError,
    MeasurementInput,
    build_experiment_metrics,
    build_metric_snapshot,
    evaluate_condition,
    write_experiment_metrics,
)
from applications.experiment_platform.executor.training import TrainingStage  # noqa: E402


SEEDS = (42, 43)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """clone된 저장소를 흉내 내는 디렉터리.

    #754 전환 기간 동안 이 fixture는 재배치 **이전** 트리를 나타낸다 — 채점 명령의 모듈
    이름을 workspace 모양에서 고르기 때문이다(`workspace_layout`).
    """
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

    def _fake_run(argv: list[str], *, cwd: Path, timeout_seconds: int, stage: str) -> None:
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


@pytest.mark.parametrize(
    ("tree_marker", "expected_module"),
    (
        (None, "src.cli"),
        ("autoresearch/cli.py", "autoresearch.cli"),
    ),
)
def test_evaluate_condition_calls_evaluate_model_once_per_seed(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    output_root: Path,
    tree_marker: str | None,
    expected_module: str,
) -> None:
    """seed마다 한 번씩, 학습이 남긴 산출물 경로를 그대로 넘겨 채점한다.

    채점 명령의 모듈 이름은 **봉인된 workspace 트리의 모양**에서 나온다(#754). 이미지는
    릴리스된 digest이고 워크스페이스는 그보다 앞선 `base_dev_sha`일 수 있어, 한쪽만
    검증하면 다른 쪽 실험이 `ModuleNotFoundError`로 전부 죽어도 이 테스트는 통과한다.
    """
    if tree_marker is not None:
        marker = workspace / tree_marker
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")
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
    assert argv[:4] == ["python", "-m", expected_module, "evaluate-model"]
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


def _built_metrics(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    output_root: Path,
    *,
    candidate_test_set: str = "clicked\n1\n",
) -> dict[str, object]:
    """요약 계약을 검증하기 위한 실제 `experiment-metrics-v1` payload를 만든다."""
    _write_training_artifacts(output_root, TrainingStage.BASELINE)
    _write_training_artifacts(
        output_root, TrainingStage.CANDIDATE, test_set_body=candidate_test_set
    )
    _stub_evaluation(monkeypatch, _DEFAULT_VALUES)
    return build_experiment_metrics(
        MeasurementInput(
            workspace=workspace,
            output_root=output_root,
            seeds=SEEDS,
            timeout_seconds=60,
        ),
        coordinates={"experiment_id": "abc", "issue_number": 619},
        dataset_fingerprint="d" * 64,
    )


def test_metric_snapshot_carries_both_conditions_for_every_paired_metric(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, output_root: Path
) -> None:
    """주 지표 하나만 실으면 "주 지표만 오른 손상"이 요약에서 보이지 않는다."""
    metrics = _built_metrics(monkeypatch, workspace, output_root)

    snapshot = build_metric_snapshot(metrics, results_uri="gs://results/metrics.json")

    assert snapshot["contract_version"] == "experiment-metric-snapshot-v1"
    assert snapshot["primary_metric"] == "roc_auc"
    assert snapshot["seeds"] == list(SEEDS)
    baseline = snapshot["conditions"][TrainingStage.BASELINE.value]
    candidate = snapshot["conditions"][TrainingStage.CANDIDATE.value]
    assert set(baseline) == {"roc_auc", "log_loss", "brier"}
    # 대역이 candidate에만 +0.01을 더한다 — seed 평균으로 실린다.
    assert baseline["roc_auc"] == pytest.approx((0.78 + 0.77) / 2)
    assert candidate["roc_auc"] == pytest.approx((0.79 + 0.78) / 2)
    assert snapshot["paired"]["roc_auc"]["mean"] == pytest.approx(0.01)
    assert snapshot["paired"]["roc_auc"]["standard_error"] is not None
    assert snapshot["dataset_fingerprint"] == "d" * 64
    assert snapshot["results_uri"] == "gs://results/metrics.json"


def test_metric_snapshot_reduces_split_matches_to_one_bit(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, output_root: Path
) -> None:
    """seed 하나라도 분할이 다르면 그 비교는 성립하지 않는다 — 요약은 거짓이 된다."""
    matched = _built_metrics(monkeypatch, workspace, output_root)

    assert build_metric_snapshot(matched, results_uri=None)["split_matches"] is True


def test_metric_snapshot_is_false_when_any_seed_split_differs(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, output_root: Path
) -> None:
    """분할이 갈린 실험을 요약만 보고 믿지 않도록 한 비트로 드러낸다."""
    mismatched = _built_metrics(
        monkeypatch, workspace, output_root, candidate_test_set="clicked\n0\n"
    )

    snapshot = build_metric_snapshot(mismatched, results_uri=None)

    assert snapshot["split_matches"] is False
    # 게시하지 않는 배포에서도 요약 자체는 만들어진다.
    assert snapshot["results_uri"] is None


def test_metric_snapshot_stays_far_below_the_api_size_limit(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, output_root: Path
) -> None:
    """요약은 전문의 입구다 — 전문을 그대로 실으면 API가 거부한다."""
    metrics = _built_metrics(monkeypatch, workspace, output_root)

    snapshot = build_metric_snapshot(metrics, results_uri="gs://results/metrics.json")

    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded.encode("utf-8")) < 2048


def test_failed_evaluation_logs_its_stderr_and_call_site(
    workspace: Path, output_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """채점 실패의 본문과 어느 조건·seed였는지가 로그에 남는다(#636).

    채점은 조건 2 × seed 3 = 6회 돈다. 호출 지점 표시가 없으면 여섯 경우가
    `evaluation_command_failed` 하나로 뭉쳐, 어느 조건의 어느 seed가 죽었는지 알 수 없다.
    """
    _write_training_artifacts(output_root, TrainingStage.BASELINE)
    config = MeasurementInput(
        workspace=workspace,
        output_root=output_root,
        seeds=SEEDS,
        timeout_seconds=60,
    )

    with caplog.at_level(
        logging.ERROR, logger="applications.experiment_platform.executor.measurement"
    ):
        with pytest.raises(MeasurementError, match="evaluation_command_failed"):
            evaluate_condition(config, TrainingStage.BASELINE)

    assert "No module named 'src'" in caplog.text
    assert f"stage=evaluate_model:baseline:{SEEDS[0]}" in caplog.text


def test_timed_out_evaluation_logs_its_partial_output(
    workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """채점이 timeout으로 죽어도 부분 출력이 남는다(#636).

    `text=True`를 줘도 `TimeoutExpired`가 싣는 출력은 bytes라 그대로 찍으면 읽을 수 없다.
    """
    with caplog.at_level(
        logging.ERROR, logger="applications.experiment_platform.executor.measurement"
    ):
        with pytest.raises(MeasurementError, match="evaluation_timeout"):
            measurement_module._run(
                [
                    sys.executable,
                    "-c",
                    "import sys, time; sys.stderr.write('PARTIAL-MARKER\\n'); "
                    "sys.stderr.flush(); time.sleep(30)",
                ],
                cwd=workspace,
                timeout_seconds=1,
                stage="evaluate_model:baseline:42",
            )

    assert "PARTIAL-MARKER" in caplog.text
    assert "stage=evaluate_model:baseline:42" in caplog.text
    assert "b'" not in caplog.text


def test_raised_failure_survives_the_phase2_redaction_filter(workspace: Path) -> None:
    """**실제로 던져진** 채점 예외가 `phase2._safe_failure_reason`을 통과해야 한다(#636)."""
    # phase2는 GitHub App·GCS 의존성을 끌어오므로 이 테스트 안에서만 import한다.
    from applications.experiment_platform.executor import phase2

    with pytest.raises(MeasurementError) as raised:
        measurement_module._run(
            [sys.executable, "-c", "import sys; sys.stderr.write('detail\\n'); sys.exit(1)"],
            cwd=workspace,
            timeout_seconds=30,
            stage="evaluate_model:baseline:42",
        )

    assert phase2._safe_failure_reason(raised.value) == "evaluation_command_failed"


def test_spawn_failure_logs_why_the_process_could_not_start(
    workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """채점 프로세스가 아예 못 떠도 그 사유가 로그에 남는다(#636)."""
    with caplog.at_level(
        logging.ERROR, logger="applications.experiment_platform.executor.measurement"
    ):
        with pytest.raises(MeasurementError, match="evaluation_spawn_failed"):
            measurement_module._run(
                ["definitely-not-a-real-command-636"],
                cwd=workspace,
                timeout_seconds=30,
                stage="evaluate_model:baseline:42",
            )

    assert "stage=evaluate_model:baseline:42" in caplog.text
    assert "error_type=FileNotFoundError" in caplog.text
