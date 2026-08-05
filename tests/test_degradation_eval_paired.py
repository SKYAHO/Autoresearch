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
