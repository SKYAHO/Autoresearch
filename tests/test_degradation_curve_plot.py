"""degradation_curve_plot.build_figure 테스트 (#471).

측정 로직(run_rolling_origin)은 다시 검증하지 않는다 — 이미 만들어진
RollingOriginResult 형태의 dict를 Plotly Figure로 정확히 옮기는지만 본다.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "bench"))

from degradation_curve_plot import build_figure  # noqa: E402


def _result(**overrides) -> dict:
    defaults = {
        "cutoff_date": "2026-07-20",
        "baseline_val_roc_auc": 0.80,
        "per_day": [
            {"elapsed_days": 0, "roc_auc": 0.80, "status": "valid"},
            {"elapsed_days": 1, "roc_auc": None, "status": "missing_date"},
            {"elapsed_days": 2, "roc_auc": 0.70, "status": "valid"},
        ],
        "degradation_point": {"elapsed_days": None, "date": None, "reason": "no_degradation_detected"},
    }
    defaults.update(overrides)
    return defaults


def test_build_figure_plots_roc_auc_with_gaps_for_invalid_days():
    figure = build_figure(_result())

    trace = figure.data[0]
    assert list(trace.x) == [0, 1, 2]
    assert list(trace.y) == [0.80, None, 0.70]


def test_build_figure_draws_baseline_line():
    figure = build_figure(_result(baseline_val_roc_auc=0.80))

    baseline_lines = [
        shape for shape in figure.layout.shapes if shape.type == "line" and shape.y0 == shape.y1
    ]
    assert any(line.y0 == 0.80 for line in baseline_lines)


def test_build_figure_omits_degradation_marker_when_not_detected():
    figure = build_figure(_result(degradation_point={"elapsed_days": None, "date": None, "reason": "x"}))

    vertical_lines = [shape for shape in figure.layout.shapes if shape.x0 == shape.x1]
    assert vertical_lines == []


def test_build_figure_draws_degradation_marker_when_detected():
    figure = build_figure(
        _result(degradation_point={"elapsed_days": 2, "date": "2026-07-22", "reason": None})
    )

    vertical_lines = [shape for shape in figure.layout.shapes if shape.x0 == shape.x1]
    assert any(line.x0 == 2 for line in vertical_lines)
