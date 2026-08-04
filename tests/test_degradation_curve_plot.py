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
        "forward_baseline_roc_auc": 0.80,
        "min_auc_drop": 0.05,
        "per_day": [
            {"elapsed_days": 0, "date": "2026-07-20", "roc_auc": 0.80, "status": "valid"},
            {"elapsed_days": 1, "date": "2026-07-21", "roc_auc": None, "status": "missing_date"},
            {"elapsed_days": 2, "date": "2026-07-22", "roc_auc": 0.70, "status": "valid"},
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


def test_build_figure_hover_text_shows_status_for_invalid_days():
    # PR #510 리뷰 Low: 곡선의 결측 지점만 보고는 missing_date/insufficient_rows/
    # single_class/evaluation_failed를 구분할 수 없었다 — hover text로 구분한다.
    figure = build_figure(_result())

    trace = figure.data[0]
    assert "missing_date" in trace.text[1]


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


def test_build_figure_draws_forward_baseline_not_val_baseline():
    """PR #520 리뷰 High#1 — 그림의 기준선이 판정에 쓰인 값과 같아야 한다.

    baseline_val_roc_auc(0.80)를 그리면 점선과 열화 마커가 4%p 어긋난 축에 놓인다.
    """
    figure = build_figure(
        _result(baseline_val_roc_auc=0.80, forward_baseline_roc_auc=0.76, min_auc_drop=0.05)
    )

    horizontal = [s for s in figure.layout.shapes if s.y0 == s.y1]
    ys = {round(s.y0, 4) for s in horizontal}

    assert 0.76 in ys  # forward baseline
    assert 0.80 not in ys  # 옛 기준선은 그리지 않는다
    assert 0.71 in ys  # threshold = 0.76 - 0.05


def test_build_figure_falls_back_to_val_baseline_for_legacy_results():
    # #485 이전 결과 JSON에는 forward 필드가 없다 — 깨지지 않고 옛 값으로 그린다.
    legacy = _result()
    legacy.pop("forward_baseline_roc_auc", None)

    figure = build_figure(legacy)

    horizontal = [s for s in figure.layout.shapes if s.y0 == s.y1]
    assert any(round(s.y0, 4) == 0.80 for s in horizontal)
