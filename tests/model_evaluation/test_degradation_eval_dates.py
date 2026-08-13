"""degradation_eval의 날짜 구간·평가일 상태 순수 함수 테스트 (#471, spec §2.1/§2.3).

BigQuery 없이 검증 가능한 순수 함수만 다룬다. `run_rolling_origin` 오케스트레이션은
test_degradation_eval.py(Task 3)에서 다룬다.
"""

import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from autoresearch.model_evaluation.degradation_eval import (  # noqa: E402
    EvaluationStatus,
    classify_evaluation_day,
    evaluation_dates,
    training_window,
)


def test_training_window_excludes_cutoff_day():
    # spec §2.1: [cutoff-W, cutoff) — cutoff 당일은 학습에 포함하지 않는다.
    # load_training_entity_spine의 BETWEEN이 inclusive-inclusive라 종료일을 cutoff-1로 당긴다.
    start, end = training_window("2026-07-20", window_days=5)

    assert start == "2026-07-15"
    assert end == "2026-07-19"


def test_training_window_single_day():
    start, end = training_window("2026-07-20", window_days=1)

    assert start == "2026-07-19"
    assert end == "2026-07-19"


def test_evaluation_dates_cutoff_is_first_day_with_elapsed_zero():
    # spec §2.1: [cutoff, cutoff+H) — cutoff 당일이 첫 평가일이다(elapsed_days=0).
    dates = evaluation_dates("2026-07-20", horizon_days=3)

    assert dates == [
        ("2026-07-20", 0),
        ("2026-07-21", 1),
        ("2026-07-22", 2),
    ]


def test_evaluation_dates_single_day_horizon():
    dates = evaluation_dates("2026-07-20", horizon_days=1)

    assert dates == [("2026-07-20", 0)]


def test_classify_evaluation_day_valid():
    dataset = pd.DataFrame({"clicked": [1, 0, 0, 1, 0]})

    assert classify_evaluation_day(dataset, min_rows=3) == EvaluationStatus.VALID


def test_classify_evaluation_day_missing_date_when_empty():
    dataset = pd.DataFrame({"clicked": []})

    assert classify_evaluation_day(dataset, min_rows=3) == EvaluationStatus.MISSING_DATE


def test_classify_evaluation_day_insufficient_rows_below_threshold():
    dataset = pd.DataFrame({"clicked": [1, 0]})

    assert classify_evaluation_day(dataset, min_rows=3) == EvaluationStatus.INSUFFICIENT_ROWS


def test_classify_evaluation_day_single_class_when_all_clicked():
    dataset = pd.DataFrame({"clicked": [1, 1, 1, 1, 1]})

    assert classify_evaluation_day(dataset, min_rows=3) == EvaluationStatus.SINGLE_CLASS


def test_classify_evaluation_day_single_class_when_all_non_clicked():
    dataset = pd.DataFrame({"clicked": [0, 0, 0, 0, 0]})

    assert classify_evaluation_day(dataset, min_rows=3) == EvaluationStatus.SINGLE_CLASS


def test_classify_evaluation_day_insufficient_rows_takes_precedence_over_single_class():
    # 행이 임계치 미만이면서 동시에 단일 클래스인 경우 — 더 근본적인 결손(행 부족)을 먼저 알린다.
    dataset = pd.DataFrame({"clicked": [1, 1]})

    assert classify_evaluation_day(dataset, min_rows=3) == EvaluationStatus.INSUFFICIENT_ROWS
