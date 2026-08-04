"""run_rolling_origin 오케스트레이션 테스트 (#471, spec §2.2·§2.3).

BigQuery/LightGBM을 직접 호출하지 않는다 — `build_training_dataset.main`/`train.main`/
`evaluate_held_out_roc_auc`를 monkeypatch로 스텁하고(이 저장소가 `tests/test_cli.py`에서
이미 쓰는 관례), 오케스트레이션 로직(날짜 계산·경로 격리·상태 판정·best_effort·
degradation_point 산출)만 검증한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.pipeline import degradation_eval  # noqa: E402
from src.pipeline import train as train_module  # noqa: E402
from src.pipeline.training_provenance import (  # noqa: E402
    RegistryProvenance,
    build_snapshot_manifest,
    snapshot_manifest_path,
    write_manifest_atomic,
)
from src.utils.model_utils import save_feature_columns, save_model  # noqa: E402

_REGISTRY = RegistryProvenance(uri="gs://fake/registry.db", generation="1", sha256="0" * 64)


def _day_frame(rows: int, *, clicked_ones: int = 1) -> pd.DataFrame:
    if rows == 0:
        return pd.DataFrame(columns=["video_id", "user_id", "event_timestamp", "clicked"])
    clicked = [1] * clicked_ones + [0] * (rows - clicked_ones)
    return pd.DataFrame(
        {
            "video_id": [f"v{i}" for i in range(rows)],
            "user_id": [f"u{i}" for i in range(rows)],
            "event_timestamp": ["2026-07-20T00:00:00Z"] * rows,
            "clicked": clicked,
        }
    )


class _Harness:
    """`run_rolling_origin`이 부르는 무거운 외부 의존을 스텁으로 대체하는 테스트 하네스."""

    def __init__(self, monkeypatch, tmp_path, *, day_frames: dict[str, pd.DataFrame], val_roc_auc: float):
        self.build_calls: list[dict] = []
        self.train_calls: list[dict] = []
        self.evaluate_calls: list[pd.DataFrame] = []
        self.roc_auc_sequence: list[float] = []
        self._day_frames = day_frames
        self._failing_dates: set[str] = set()
        self.run_root = tmp_path / "run"

        def fake_build_training_dataset_main(
            *,
            output_path,
            events_start_date,
            events_end_date,
            min_coverage_days=None,
            feature_service=None,
            extra_features=None,
        ):
            self.build_calls.append(
                {
                    "output_path": output_path,
                    "events_start_date": events_start_date,
                    "events_end_date": events_end_date,
                    "min_coverage_days": min_coverage_days,
                }
            )
            if events_start_date in self._failing_dates:
                raise RuntimeError(f"조립 실패 시뮬레이션: {events_start_date}")
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            frame = self._day_frames.get(events_start_date, _day_frame(0))
            frame.to_csv(path, index=False)
            manifest = build_snapshot_manifest(
                dataset_path=path,
                events_start_date=events_start_date,
                events_end_date=events_end_date,
                feature_service=feature_service or "ctr_training_v1",
                registry=_REGISTRY,
                code_archive_sha=None,
            )
            write_manifest_atomic(manifest, snapshot_manifest_path(path))
            return None

        def fake_train_main(**kwargs):
            self.train_calls.append(kwargs)
            save_model("dummy-model", kwargs["model_output"])
            save_feature_columns(["f1", "f2"], kwargs["feature_columns_output"])
            return train_module.TrainingOutcome(
                sampling_rate=1.0, run_id="run-1", val_roc_auc=val_roc_auc
            )

        def fake_evaluate_held_out_roc_auc(model, dataset, feature_columns):
            self.evaluate_calls.append(dataset)
            return self.roc_auc_sequence.pop(0)

        monkeypatch.setattr(degradation_eval.build_training_dataset, "main", fake_build_training_dataset_main)
        monkeypatch.setattr(degradation_eval.train, "main", fake_train_main)
        monkeypatch.setattr(degradation_eval, "evaluate_held_out_roc_auc", fake_evaluate_held_out_roc_auc)

    def fail_on(self, date: str) -> None:
        self._failing_dates.add(date)


def test_run_rolling_origin_computes_training_and_evaluation_windows(monkeypatch, tmp_path):
    harness = _Harness(
        monkeypatch,
        tmp_path,
        day_frames={"2026-07-20": _day_frame(5), "2026-07-21": _day_frame(5)},
        val_roc_auc=0.80,
    )
    harness.roc_auc_sequence = [0.80, 0.79]

    degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=2,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.05,
    )

    training_call = harness.build_calls[0]
    assert training_call["events_start_date"] == "2026-07-17"
    assert training_call["events_end_date"] == "2026-07-19"  # cutoff-1, 학습에 cutoff 당일 미포함

    eval_calls = harness.build_calls[1:]
    assert [c["events_start_date"] for c in eval_calls] == ["2026-07-20", "2026-07-21"]
    assert all(c["events_start_date"] == c["events_end_date"] for c in eval_calls)
    assert all(c["min_coverage_days"] == 0 for c in eval_calls)  # 좁은 단일 날짜 조회는 가드 우회

    assert harness.train_calls[0]["data_path"] == training_call["output_path"]


def test_run_rolling_origin_isolates_output_paths_per_day(monkeypatch, tmp_path):
    harness = _Harness(
        monkeypatch,
        tmp_path,
        day_frames={"2026-07-20": _day_frame(5), "2026-07-21": _day_frame(5)},
        val_roc_auc=0.80,
    )
    harness.roc_auc_sequence = [0.80, 0.79]

    degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=2,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.05,
    )

    output_paths = [c["output_path"] for c in harness.build_calls]
    assert len(output_paths) == len(set(str(p) for p in output_paths))  # 전부 서로 다른 경로
    assert str(harness.run_root / "training") in str(output_paths[0])
    assert str(harness.run_root / "evaluation" / "2026-07-20") in str(output_paths[1])
    assert str(harness.run_root / "evaluation" / "2026-07-21") in str(output_paths[2])


def test_run_rolling_origin_writes_evaluation_provenance_manifest(monkeypatch, tmp_path):
    harness = _Harness(
        monkeypatch, tmp_path, day_frames={"2026-07-20": _day_frame(5, clicked_ones=2)}, val_roc_auc=0.80
    )
    harness.roc_auc_sequence = [0.79]

    result = degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=1,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.05,
    )

    day = result.per_day[0]
    assert day.evaluation_provenance is not None
    assert day.evaluation_provenance.row_count == 5
    assert day.evaluation_provenance.positive_count == 2
    assert day.evaluation_provenance.negative_count == 3
    manifest_path = harness.run_root / "evaluation" / "2026-07-20" / "dataset_manifest.json"
    assert manifest_path.is_file()


def test_run_rolling_origin_skips_roc_auc_for_invalid_days(monkeypatch, tmp_path):
    harness = _Harness(
        monkeypatch,
        tmp_path,
        day_frames={"2026-07-20": _day_frame(1)},  # min_rows_per_day=3보다 적음
        val_roc_auc=0.80,
    )
    harness.roc_auc_sequence = []  # evaluate가 아예 호출되지 않아야 통과

    result = degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=1,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.05,
    )

    assert result.per_day[0].status == degradation_eval.EvaluationStatus.INSUFFICIENT_ROWS
    assert result.per_day[0].roc_auc is None
    assert harness.evaluate_calls == []


def test_run_rolling_origin_best_effort_false_aborts_on_failure(monkeypatch, tmp_path):
    harness = _Harness(
        monkeypatch,
        tmp_path,
        day_frames={"2026-07-20": _day_frame(5), "2026-07-22": _day_frame(5)},
        val_roc_auc=0.80,
    )
    harness.fail_on("2026-07-21")
    harness.roc_auc_sequence = [0.79]  # 07-20만 소비되고 07-22는 시도되지 않아야 함

    with pytest.raises(RuntimeError, match="조립 실패 시뮬레이션"):
        degradation_eval.run_rolling_origin(
            "2026-07-20",
            window_days=3,
            horizon_days=3,
            run_root=harness.run_root,
            min_rows_per_day=3,
            min_auc_drop=0.05,
            best_effort=False,
        )

    # 07-20(성공) → 07-21(실패, 중단) 순서. 07-22는 시도조차 되지 않는다.
    assembled_dates = [c["events_start_date"] for c in harness.build_calls[1:]]
    assert assembled_dates == ["2026-07-20", "2026-07-21"]


def test_run_rolling_origin_best_effort_true_continues_after_failure(monkeypatch, tmp_path):
    harness = _Harness(
        monkeypatch,
        tmp_path,
        day_frames={"2026-07-20": _day_frame(5), "2026-07-22": _day_frame(5)},
        val_roc_auc=0.80,
    )
    harness.fail_on("2026-07-21")
    harness.roc_auc_sequence = [0.79, 0.78]

    result = degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=3,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.05,
        best_effort=True,
    )

    statuses = [day.status for day in result.per_day]
    assert statuses == [
        degradation_eval.EvaluationStatus.VALID,
        degradation_eval.EvaluationStatus.EVALUATION_FAILED,
        degradation_eval.EvaluationStatus.VALID,
    ]
    assert result.per_day[1].evaluation_provenance is None


def test_run_rolling_origin_refuses_to_overwrite_nonempty_run_root(monkeypatch, tmp_path):
    harness = _Harness(
        monkeypatch, tmp_path, day_frames={"2026-07-20": _day_frame(5)}, val_roc_auc=0.80
    )
    harness.run_root.mkdir(parents=True)
    (harness.run_root / "leftover.txt").write_text("stale")

    with pytest.raises(FileExistsError):
        degradation_eval.run_rolling_origin(
            "2026-07-20",
            window_days=3,
            horizon_days=1,
            run_root=harness.run_root,
            min_rows_per_day=3,
            min_auc_drop=0.05,
        )

    assert harness.build_calls == []  # 아무 조립도 시도하지 않고 먼저 막아야 한다


def test_run_rolling_origin_overwrite_true_proceeds(monkeypatch, tmp_path):
    harness = _Harness(
        monkeypatch, tmp_path, day_frames={"2026-07-20": _day_frame(5)}, val_roc_auc=0.80
    )
    harness.run_root.mkdir(parents=True)
    (harness.run_root / "leftover.txt").write_text("stale")
    harness.roc_auc_sequence = [0.79]

    result = degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=1,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.05,
        overwrite=True,
    )

    assert result.per_day[0].status == degradation_eval.EvaluationStatus.VALID


def test_run_rolling_origin_computes_degradation_point_from_result(monkeypatch, tmp_path):
    harness = _Harness(
        monkeypatch,
        tmp_path,
        day_frames={
            "2026-07-20": _day_frame(5),
            "2026-07-21": _day_frame(5),
            "2026-07-22": _day_frame(5),
        },
        val_roc_auc=0.80,
    )
    harness.roc_auc_sequence = [0.80, 0.70, 0.68]  # 2번째부터 연속 degraded

    result = degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=3,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.05,  # threshold = 0.75
    )

    assert result.degradation_point.elapsed_days == 2
    assert result.baseline_val_roc_auc == pytest.approx(0.80)
