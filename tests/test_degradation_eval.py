"""run_rolling_origin 오케스트레이션 테스트 (#471, spec §2.2·§2.3).

BigQuery/LightGBM을 직접 호출하지 않는다 — `build_training_dataset.main`/`train.main`/
`evaluate_held_out_roc_auc`를 monkeypatch로 스텁하고(이 저장소가 `tests/test_cli.py`에서
이미 쓰는 관례), 오케스트레이션 로직(날짜 계산·경로 격리·상태 판정·best_effort·
degradation_point 산출·categorical 정합성)만 검증한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from autoresearch.model_evaluation import degradation_eval  # noqa: E402
from autoresearch.model_training import train as train_module  # noqa: E402
from autoresearch.model_training.training_provenance import (  # noqa: E402
    RegistryProvenance,
    build_snapshot_manifest,
    snapshot_manifest_path,
    write_manifest_atomic,
)
from autoresearch.model_training.model_utils import (  # noqa: E402
    save_categorical_columns,
    save_feature_columns,
    save_model,
)

_REGISTRY = RegistryProvenance(uri="gs://fake/registry.db", generation="1", sha256="0" * 64)


def _day_frame(
    rows: int, *, clicked_ones: int = 1, category_values: list[str] | None = None
) -> pd.DataFrame:
    """실제 평가일 CSV 스키마(MODEL_FEATURE_COLUMNS + clicked)를 흉내낸다.

    실제 조립 계약(`build_training_dataset.py`)은 `video_id`/`event_timestamp` 같은
    엔티티 키를 CSV에 남기지 않는다 — 이 프레임도 그 계약을 따른다(PR #510 리뷰:
    이전 스텁이 엔티티 키를 넣어 High-severity 결함을 못 잡았던 것을 바로잡음).
    """
    if rows == 0:
        columns = ["clicked", *(["category_id"] if category_values else [])]
        return pd.DataFrame(columns=columns)
    clicked = [1] * clicked_ones + [0] * (rows - clicked_ones)
    frame = pd.DataFrame({"clicked": clicked})
    if category_values is not None:
        frame["category_id"] = (list(category_values) * rows)[:rows]
    return frame


class _Harness:
    """`run_rolling_origin`이 부르는 무거운 외부 의존을 스텁으로 대체하는 테스트 하네스."""

    def __init__(
        self,
        monkeypatch,
        tmp_path,
        *,
        day_frames: dict[str, pd.DataFrame],
        val_roc_auc: float,
        categorical_categories: dict | None = None,
    ):
        self.build_calls: list[dict] = []
        self.train_calls: list[dict] = []
        self.evaluate_calls: list[pd.DataFrame] = []
        self.roc_auc_sequence: list = []  # float 또는 Exception 인스턴스를 섞어 넣을 수 있다
        self._day_frames = day_frames
        self._failing_dates: set[str] = set()
        self._categorical_categories = categorical_categories or {}
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
            # 실제 train.main은 항상 categorical_columns.json을 쓴다(#454 계약) —
            # 스텁도 같은 파일을 남겨야 load_categorical_columns가 실패하지 않는다.
            save_categorical_columns(
                self._categorical_categories, kwargs["categorical_columns_output"]
            )
            return train_module.TrainingOutcome(
                sampling_rate=1.0, run_id="run-1", val_roc_auc=val_roc_auc
            )

        def fake_evaluate_held_out_roc_auc(model, dataset, feature_columns):
            self.evaluate_calls.append(dataset)
            outcome = self.roc_auc_sequence.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

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


def test_run_rolling_origin_best_effort_false_aborts_on_assembly_failure(monkeypatch, tmp_path):
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


def test_run_rolling_origin_best_effort_true_continues_after_assembly_failure(monkeypatch, tmp_path):
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


def test_run_rolling_origin_best_effort_false_aborts_on_post_assembly_failure(monkeypatch, tmp_path):
    # PR #510 리뷰 Medium: best_effort가 조립 호출만 감싸던 결함 — 조립 이후 단계
    # (여기서는 evaluate_held_out_roc_auc)에서 실패해도 잡혀야 한다.
    harness = _Harness(
        monkeypatch,
        tmp_path,
        day_frames={"2026-07-20": _day_frame(5), "2026-07-21": _day_frame(5)},
        val_roc_auc=0.80,
    )
    harness.roc_auc_sequence = [ValueError("피처 컬럼 불일치")]

    with pytest.raises(ValueError, match="피처 컬럼 불일치"):
        degradation_eval.run_rolling_origin(
            "2026-07-20",
            window_days=3,
            horizon_days=2,
            run_root=harness.run_root,
            min_rows_per_day=3,
            min_auc_drop=0.05,
            best_effort=False,
        )


def test_run_rolling_origin_best_effort_true_continues_after_post_assembly_failure(monkeypatch, tmp_path):
    harness = _Harness(
        monkeypatch,
        tmp_path,
        day_frames={"2026-07-20": _day_frame(5), "2026-07-21": _day_frame(5)},
        val_roc_auc=0.80,
    )
    harness.roc_auc_sequence = [ValueError("피처 컬럼 불일치"), 0.77]

    result = degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=2,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.05,
        best_effort=True,
    )

    statuses = [day.status for day in result.per_day]
    assert statuses == [
        degradation_eval.EvaluationStatus.EVALUATION_FAILED,
        degradation_eval.EvaluationStatus.VALID,
    ]


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


def test_run_rolling_origin_overwrite_true_clears_stale_files_from_previous_run(monkeypatch, tmp_path):
    # PR #510 리뷰 Low: overwrite=True가 이전 실행 잔재를 정리하지 않던 결함.
    # horizon_days=30으로 먼저 돌린 자국(2026-08-15 디렉터리)이, horizon_days=1로
    # overwrite 재실행한 뒤에는 사라져야 한다.
    harness = _Harness(
        monkeypatch, tmp_path, day_frames={"2026-07-20": _day_frame(5)}, val_roc_auc=0.80
    )
    stale_dir = harness.run_root / "evaluation" / "2026-08-15"
    stale_dir.mkdir(parents=True)
    (stale_dir / "dataset.csv").write_text("stale,data\n1,2\n")
    harness.roc_auc_sequence = [0.79]

    degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=1,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.05,
        overwrite=True,
    )

    assert not stale_dir.exists()


def test_run_rolling_origin_fails_fast_on_nonfinite_baseline(monkeypatch, tmp_path):
    # PR #510 리뷰 Low: val_roc_auc가 NaN이면(TrainingOutcome 기본값) horizon_days
    # 평가를 전부 마친 뒤에야 RollingOriginResult 생성 시 ValidationError로 죽던 결함.
    harness = _Harness(
        monkeypatch,
        tmp_path,
        day_frames={"2026-07-20": _day_frame(5)},
        val_roc_auc=float("nan"),
    )

    with pytest.raises(ValueError, match="유한하지"):
        degradation_eval.run_rolling_origin(
            "2026-07-20",
            window_days=3,
            horizon_days=1,
            run_root=harness.run_root,
            min_rows_per_day=3,
            min_auc_drop=0.05,
        )

    # 학습 호출 1건만 있고, 평가일 조립은 시도조차 되지 않아야 한다(비싼 루프 진입 전 차단).
    assert len(harness.build_calls) == 1


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


def test_run_rolling_origin_aligns_categorical_codes_with_training(monkeypatch, tmp_path):
    # PR #510 리뷰 이해도 확인: evaluate_held_out_roc_auc 내부의 무조건
    # astype("category")는 그날 데이터에 실제 등장한 값만으로 카테고리를 다시
    # 매긴다. 학습이 본 카테고리 전체(["A", "B", "C"])를 categorical_columns.json에
    # 저장해뒀는데, 평가일 데이터에는 "A"만 등장해도(카테고리 구성이 좁아져도)
    # run_rolling_origin이 학습 시점 카테고리 전체를 그대로 재현해야 한다.
    harness = _Harness(
        monkeypatch,
        tmp_path,
        day_frames={
            "2026-07-20": _day_frame(5, category_values=["A", "A", "A", "A", "A"])
        },
        val_roc_auc=0.80,
        categorical_categories={"category_id": ["A", "B", "C"]},
    )
    harness.roc_auc_sequence = [0.79]

    degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=1,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.05,
    )

    (scored_dataset,) = harness.evaluate_calls
    assert list(scored_dataset["category_id"].cat.categories) == ["A", "B", "C"]


# ============================================================================
# Task 1 — §4.3 baseline 재정의 (#485)
# forward_baseline_roc_auc(per_day 중 첫 valid 관측치)를 판정 기준선으로 쓴다.
# baseline_val_roc_auc(랜덤 val)는 결과 필드로만 유지한다(하위호환).
# ============================================================================


def test_forward_baseline_is_first_valid_observation(monkeypatch, tmp_path):
    harness = _Harness(
        monkeypatch,
        tmp_path,
        day_frames={
            "2026-07-20": _day_frame(5),
            "2026-07-21": _day_frame(5),
        },
        val_roc_auc=0.80,
    )
    harness.roc_auc_sequence = [0.76, 0.75]

    result = degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=2,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.05,
    )

    assert result.forward_baseline_roc_auc == pytest.approx(0.76)
    assert result.forward_baseline_source == 0
    # 랜덤 val 지표는 결과 필드로 그대로 남는다(하위호환).
    assert result.baseline_val_roc_auc == pytest.approx(0.80)


def test_forward_baseline_skips_invalid_days(monkeypatch, tmp_path):
    # elapsed_days=0이 무효(행 부족)면 첫 valid는 elapsed_days=1이다.
    harness = _Harness(
        monkeypatch,
        tmp_path,
        day_frames={
            "2026-07-20": _day_frame(1),  # min_rows_per_day=3 미만 → insufficient_rows
            "2026-07-21": _day_frame(5),
        },
        val_roc_auc=0.80,
    )
    harness.roc_auc_sequence = [0.73]

    result = degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=2,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.05,
    )

    assert result.forward_baseline_roc_auc == pytest.approx(0.73)
    assert result.forward_baseline_source == 1


def test_forward_baseline_none_when_no_valid_days(monkeypatch, tmp_path):
    harness = _Harness(
        monkeypatch,
        tmp_path,
        day_frames={"2026-07-20": _day_frame(1)},  # 유효일 0개
        val_roc_auc=0.80,
    )
    harness.roc_auc_sequence = []

    result = degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=1,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.05,
    )

    assert result.forward_baseline_roc_auc is None
    assert result.forward_baseline_source is None
    assert result.degradation_point.reason == "insufficient_valid_points"


def test_degradation_point_uses_forward_baseline_not_val_roc_auc(monkeypatch, tmp_path):
    """이 Task의 리트머스 — 두 기준선이 서로 다른 판정을 내는 시나리오.

    baseline_val_roc_auc=0.80, min_auc_drop=0.05 → 옛 기준 threshold=0.75.
    forward_baseline_roc_auc=per_day[0]=0.76 → 새 기준 threshold=0.71.

    per_day = [0.76, 0.73, 0.72]
    - 옛 기준(0.75): 0.73/0.72가 연속 degraded → elapsed_days=2에서 탐지(오탐)
    - 새 기준(0.71): 셋 다 threshold 위 → 미탐지

    4%p 오프셋이 만드는 정확히 그 오탐이라, 새 기준으로 바뀌었는지 이 하나로 갈린다.
    """
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
    harness.roc_auc_sequence = [0.76, 0.73, 0.72]

    result = degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=3,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.05,
    )

    assert result.forward_baseline_roc_auc == pytest.approx(0.76)
    assert result.degradation_point.elapsed_days is None
    assert result.degradation_point.reason == "no_degradation_detected"


def test_degradation_point_still_detects_real_drop_against_forward_baseline(
    monkeypatch, tmp_path
):
    # 새 기준선으로도 진짜 하락은 잡혀야 한다(위 테스트의 반대편).
    # forward_baseline=0.76, min_auc_drop=0.05 → threshold=0.71.
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
    harness.roc_auc_sequence = [0.76, 0.70, 0.69]

    result = degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=3,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.05,
    )

    assert result.degradation_point.elapsed_days == 2
    assert result.degradation_point.reason is None


# ============================================================================
# Task 2 — §3 최근 구간 vs 전체 구간 지표 분리 (#485)
# 날 동등 가중 평균(#506의 매크로 평균과 같은 근거 — 행 수가 많은 날이 지표를
# 지배하면 "날의 평균"이 아니라 "행의 평균"이 된다).
# ============================================================================


def _five_declining_days() -> dict[str, pd.DataFrame]:
    return {f"2026-07-{20 + i}": _day_frame(5) for i in range(5)}


def test_overall_and_recent_means_differ_when_performance_declines(monkeypatch, tmp_path):
    """분리가 실제로 되는지 — 두 값이 **반드시 달라야** 의미 있는 검증이다.

    per_day = [0.80, 0.78, 0.70, 0.68, 0.66], recent_window_days=3
      overall = 3.62 / 5 = 0.724
      recent  = (0.70 + 0.68 + 0.66) / 3 = 0.68
    뒤로 갈수록 하락하는 시나리오라 두 값이 갈린다 — 우연히 같아지지 않는다.
    """
    harness = _Harness(
        monkeypatch, tmp_path, day_frames=_five_declining_days(), val_roc_auc=0.85
    )
    harness.roc_auc_sequence = [0.80, 0.78, 0.70, 0.68, 0.66]

    result = degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=5,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.50,  # 열화 판정이 이 테스트에 끼어들지 않게 크게 둔다
        recent_window_days=3,
    )

    assert result.overall_roc_auc_mean == pytest.approx(0.724)
    assert result.recent_roc_auc_mean == pytest.approx(0.68)
    # 분리가 실제로 됐는지 — 같으면 이 테스트는 아무것도 검증하지 못한다.
    assert result.overall_roc_auc_mean != pytest.approx(result.recent_roc_auc_mean)
    assert result.recent_window_days == 3


def test_recent_mean_uses_only_last_n_valid_days_skipping_invalid(monkeypatch, tmp_path):
    # 무효일이 중간에 끼어도 "최근 N개 **유효일**"이지 "최근 N일"이 아니다.
    harness = _Harness(
        monkeypatch,
        tmp_path,
        day_frames={
            "2026-07-20": _day_frame(5),
            "2026-07-21": _day_frame(1),  # insufficient_rows → 무효
            "2026-07-22": _day_frame(5),
            "2026-07-23": _day_frame(5),
        },
        val_roc_auc=0.85,
    )
    harness.roc_auc_sequence = [0.90, 0.60, 0.50]  # 유효일 3개

    result = degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=4,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.50,
        recent_window_days=2,
    )

    assert result.overall_roc_auc_mean == pytest.approx((0.90 + 0.60 + 0.50) / 3)
    assert result.recent_roc_auc_mean == pytest.approx((0.60 + 0.50) / 2)


def test_recent_mean_is_none_when_fewer_valid_days_than_window(monkeypatch, tmp_path):
    # 적은 표본으로 평균을 만들어 "최근 성능"이라고 부르지 않는다.
    harness = _Harness(
        monkeypatch,
        tmp_path,
        day_frames={"2026-07-20": _day_frame(5), "2026-07-21": _day_frame(5)},
        val_roc_auc=0.85,
    )
    harness.roc_auc_sequence = [0.80, 0.78]  # 유효일 2개 < recent_window_days=3

    result = degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=2,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.50,
        recent_window_days=3,
    )

    assert result.overall_roc_auc_mean == pytest.approx(0.79)
    assert result.recent_roc_auc_mean is None


def test_means_are_none_when_no_valid_days(monkeypatch, tmp_path):
    harness = _Harness(
        monkeypatch, tmp_path, day_frames={"2026-07-20": _day_frame(1)}, val_roc_auc=0.85
    )
    harness.roc_auc_sequence = []

    result = degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=1,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.05,
    )

    assert result.overall_roc_auc_mean is None
    assert result.recent_roc_auc_mean is None


def test_recent_window_days_defaults_to_three(monkeypatch, tmp_path):
    # 하드코딩이 아니라 키워드 인자 기본값이다(spec §7.3 — 실측 후 재조정 대상).
    harness = _Harness(
        monkeypatch, tmp_path, day_frames={"2026-07-20": _day_frame(5)}, val_roc_auc=0.85
    )
    harness.roc_auc_sequence = [0.80]

    result = degradation_eval.run_rolling_origin(
        "2026-07-20",
        window_days=3,
        horizon_days=1,
        run_root=harness.run_root,
        min_rows_per_day=3,
        min_auc_drop=0.50,
    )

    assert result.recent_window_days == 3
