"""시간축 paired 평가 — 조건 식별 정보 기록 (#514 Task 1, spec §2.4).

`verify_training_comparison`은 MLflow run id 두 개를 받는데(`training_comparison.py:508`)
`run_rolling_origin`이 그 id를 버리고 있었다. 두 조건 비교의 전제 조건이라 결과에
기록한다. `seed`도 같은 이유다 — spec §3.1의 "동일 시드 고정"을 검증하려면 결과에
남아야 하는데 현재 없다.

**두 필드 모두 기본값 `None`이어야 한다.** `#510`/`#520`이 이미 만든 결과 JSON을 읽는
경로(`degradation_curve_plot.py`, `#472` 소비 경로)가 깨지면 안 된다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.pipeline.degradation_eval import (  # noqa: E402
    DegradationPoint,
    RollingOriginResult,
)
from src.pipeline.training_provenance import (  # noqa: E402
    DatasetColumn,
    TrainingSnapshotManifest,
)


def _manifest() -> TrainingSnapshotManifest:
    return TrainingSnapshotManifest(
        dataset_sha256="0" * 64,
        schema_sha256="1" * 64,
        row_count=10,
        columns=[DatasetColumn(name="clicked", dtype="int64")],
        created_at="2026-07-20T00:00:00Z",
        events_start_date="2026-07-17",
        events_end_date="2026-07-19",
        feature_service="ctr_training_v1",
        registry_uri="gs://fake/registry.db",
        registry_generation="1",
        registry_sha256="2" * 64,
    )


def _payload() -> dict:
    """`#510`/`#520`이 만든 형태 — 신규 두 필드가 **없는** 결과 JSON."""
    return {
        "cutoff_date": "2026-07-20",
        "window_days": 3,
        "horizon_days": 5,
        "baseline_val_roc_auc": 0.80,
        "forward_baseline_roc_auc": 0.76,
        "forward_baseline_source": 0,
        "min_auc_drop": 0.05,
        "per_day": [],
        "degradation_point": {"elapsed_days": 7, "date": "2026-07-27", "reason": None},
        "training_snapshot_manifest": json.loads(_manifest().model_dump_json()),
    }


def test_existing_result_json_without_new_fields_still_loads():
    """하위호환 가드 — 기존 산출물을 읽는 경로가 깨지면 안 된다."""
    result = RollingOriginResult.model_validate(_payload())

    assert result.training_run_id is None
    assert result.seed is None


def test_training_run_id_and_seed_are_recorded_when_given():
    result = RollingOriginResult(
        cutoff_date="2026-07-20",
        window_days=3,
        horizon_days=5,
        baseline_val_roc_auc=0.80,
        forward_baseline_roc_auc=0.76,
        forward_baseline_source=0,
        min_auc_drop=0.05,
        per_day=[],
        degradation_point=DegradationPoint(elapsed_days=7, date="2026-07-27"),
        training_snapshot_manifest=_manifest(),
        training_run_id="0e08cb1bb5234dcbacd0e068149c49ef",
        seed=42,
    )

    assert result.training_run_id == "0e08cb1bb5234dcbacd0e068149c49ef"
    assert result.seed == 42


def test_new_fields_survive_json_round_trip():
    """`#472`·`#514`가 결과 JSON을 파일로 주고받으므로 직렬화에 실려야 한다."""
    payload = _payload() | {"training_run_id": "a" * 32, "seed": 42}

    restored = RollingOriginResult.model_validate(
        json.loads(RollingOriginResult.model_validate(payload).model_dump_json())
    )

    assert restored.training_run_id == "a" * 32
    assert restored.seed == 42


# ---------------------------------------------------------------------------
# 측정 run 표식 (#514 Task 2, spec §8.3(4))
#
# `verify_training_comparison`이 challenger run에 comparison manifest를 되쓰므로
# (`training_comparison.py:487-492`), 그것만 보면 승격 절차를 밟은 run처럼 읽힌다.
# `defer_registration`은 run 어디에도 기록되지 않아 구분할 방법이 없었다.
# ---------------------------------------------------------------------------


def test_rolling_origin_marks_training_run_as_measurement_only(monkeypatch):
    """`run_rolling_origin`의 cutoff 학습이 측정용 표식을 run에 남긴다."""
    from src.pipeline import degradation_eval

    captured: dict = {}

    def _fake_main(**kwargs):
        captured.update(kwargs)
        raise _StopAfterTraining

    monkeypatch.setattr(degradation_eval.build_training_dataset, "main", lambda **_: None)
    monkeypatch.setattr(
        degradation_eval, "load_training_snapshot_manifest", lambda _: _manifest()
    )
    monkeypatch.setattr(degradation_eval.train, "main", _fake_main)

    try:
        degradation_eval.run_rolling_origin(
            "2026-07-20",
            window_days=3,
            horizon_days=2,
            run_root=Path("__unused__"),
            min_rows_per_day=1,
            min_auc_drop=0.01,
            seed=42,
            overwrite=True,
        )
    except _StopAfterTraining:
        pass

    params = captured.get("extra_params") or {}
    assert params.get("measurement_only") == "true"
    # `defer_registration`은 train.main의 분기에만 쓰여 run에 남지 않았다 — 여기서 남긴다.
    assert params.get("defer_registration") == "true"
    assert captured.get("defer_registration") is True


class _StopAfterTraining(RuntimeError):
    """학습 호출 인자만 보고 멈춘다 — 평가일 조립(BigQuery)까지 가지 않는다."""


# ---------------------------------------------------------------------------
# 조건 동일성 검증 (#514 Task 2, spec §3)
#
# 항목마다 **독립적으로** 실패해야 한다 — 한 항목만 검사하고 나머지를 통과시키는
# 구현을 잡기 위해서다. "다르다"만 남기면 원인 추적이 끊기므로 어긋난 항목을
# 이름으로 남긴다(#485 spec §4.2 "조용히 뭉개지 않는다"와 같은 결).
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from src.pipeline.degradation_eval import (  # noqa: E402
    ConditionMismatchField,
    EvaluationStatus,
    PerDayResult,
    TemporalHoldReason,
    compare_conditions,
)


def _day(elapsed: int) -> PerDayResult:
    return PerDayResult(
        date=f"2026-07-{20 + elapsed:02d}",
        elapsed_days=elapsed,
        status=EvaluationStatus.VALID,
        roc_auc=0.70 - 0.01 * elapsed,
    )


def _condition() -> RollingOriginResult:
    """두 조건의 공통 기준. 여기서 한 항목씩만 어긋뜨려 독립 실패를 확인한다."""
    return RollingOriginResult(
        cutoff_date="2026-07-20",
        window_days=15,
        horizon_days=3,
        training_run_id="a" * 32,
        seed=42,
        baseline_val_roc_auc=0.72,
        forward_baseline_roc_auc=0.70,
        forward_baseline_source=0,
        min_auc_drop=0.00933,
        per_day=[_day(0), _day(1), _day(2)],
        degradation_point=DegradationPoint(elapsed_days=2, date="2026-07-22"),
        training_snapshot_manifest=_manifest(),
    )


def _with_manifest(result: RollingOriginResult, **updates) -> RollingOriginResult:
    manifest = result.training_snapshot_manifest.model_copy(update=updates)
    return result.model_copy(update={"training_snapshot_manifest": manifest})


def test_identical_conditions_match():
    match = compare_conditions(_condition(), _condition())

    assert match.matched is True
    assert match.mismatched_fields == ()


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"cutoff_date": "2026-07-21"}, ConditionMismatchField.CUTOFF_DATE),
        ({"window_days": 10}, ConditionMismatchField.WINDOW_DAYS),
        ({"horizon_days": 5}, ConditionMismatchField.HORIZON_DAYS),
        ({"seed": 43}, ConditionMismatchField.SEED),
        ({"per_day": [_day(0), _day(1)]}, ConditionMismatchField.EVALUATION_DATES),
    ],
)
def test_each_top_level_field_mismatch_is_detected_independently(updates, expected):
    challenger = _condition().model_copy(update=updates)

    match = compare_conditions(_condition(), challenger)

    assert match.matched is False
    assert match.mismatched_fields == (expected,)


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"dataset_sha256": "9" * 64}, ConditionMismatchField.DATASET_SHA256),
        ({"schema_sha256": "9" * 64}, ConditionMismatchField.SCHEMA_SHA256),
        ({"registry_generation": "999"}, ConditionMismatchField.REGISTRY_GENERATION),
        ({"registry_sha256": "9" * 64}, ConditionMismatchField.REGISTRY_SHA256),
        ({"feature_service": "other_service"}, ConditionMismatchField.FEATURE_SERVICE),
    ],
)
def test_each_snapshot_field_mismatch_is_detected_independently(updates, expected):
    challenger = _with_manifest(_condition(), **updates)

    match = compare_conditions(_condition(), challenger)

    assert match.matched is False
    assert match.mismatched_fields == (expected,)


def test_multiple_mismatches_are_all_reported():
    """하나만 찾고 멈추면 두 번째 실행에서야 나머지가 드러난다."""
    challenger = _condition().model_copy(update={"window_days": 10, "seed": 43})

    match = compare_conditions(_condition(), challenger)

    assert set(match.mismatched_fields) == {
        ConditionMismatchField.WINDOW_DAYS,
        ConditionMismatchField.SEED,
    }


def test_evaluation_dates_compare_as_set_not_order():
    """순서는 `_ordered_by_elapsed`가 정규화하므로 집합으로 본다."""
    challenger = _condition().model_copy(update={"per_day": [_day(2), _day(0), _day(1)]})

    match = compare_conditions(_condition(), challenger)

    assert match.matched is True


def test_condition_mismatch_has_its_own_hold_reason():
    """spec §3.2 — 불일치는 통계 추정 없이 hold로 끝낸다."""
    assert TemporalHoldReason.CONDITION_MISMATCH.value == "condition_mismatch"


def test_seed_none_on_legacy_result_is_a_mismatch_not_a_pass():
    """`#510`/`#520` 결과는 seed가 None이다 — "같다"로 통과시키면 안 된다.

    두 조건 모두 None이어도 "동일 시드를 썼다"는 증거가 아니다. 관측되지 않은 것을
    "안전"으로 바꾸지 않는다(#485 spec §4.1과 같은 결).
    """
    legacy = _condition().model_copy(update={"seed": None})

    match = compare_conditions(legacy, legacy)

    assert match.matched is False
    assert ConditionMismatchField.SEED in match.mismatched_fields
