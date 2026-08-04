#!/usr/bin/env python3
"""`measure-degradation` 결과 JSON을 Plotly 열화 곡선으로 시각화한다(#471).

[파이프라인] 열화 측정 구간의 **시각화 보조** 도구다. `measure-degradation`(src/cli.py)이
낸 `RollingOriginResult` JSON만 읽어 그린다 — 측정·열화 판정 로직은 여기서 다시
구현하지 않고 정본(``src/pipeline/degradation_eval.py``) 산출물을 그대로 옮긴다.

[기능] x축 ``elapsed_days``(cutoff 기준 달력 일수, 관측 순번이 아니다 — spec §2.1),
y축 ROC-AUC. ``valid``가 아닌 날은 ``roc_auc=null``이라 Plotly가 그 지점에서 선을
잇지 않는다(``connectgaps=False``) — 결손일을 있는 그대로 드러낸다. baseline
기준선과 ``degradation_point``(탐지됐을 때만) 마커를 함께 그린다.

사용:
    python scripts/bench/degradation_curve_plot.py \\
        --result result.json --output curve.html
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import plotly.graph_objects as go


def build_figure(result: dict) -> go.Figure:
    """`RollingOriginResult` dict(측정 결과 JSON을 파싱한 것)로 열화 곡선을 그린다."""
    per_day = sorted(result["per_day"], key=lambda day: day["elapsed_days"])
    elapsed_days = [day["elapsed_days"] for day in per_day]
    roc_auc = [day["roc_auc"] for day in per_day]

    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=elapsed_days,
            y=roc_auc,
            mode="lines+markers",
            name="ROC-AUC",
            connectgaps=False,
        )
    )

    baseline = result["baseline_val_roc_auc"]
    figure.add_hline(
        y=baseline,
        line_dash="dot",
        annotation_text=f"baseline={baseline:.4f}",
    )

    degradation_point = result.get("degradation_point") or {}
    if degradation_point.get("elapsed_days") is not None:
        figure.add_vline(
            x=degradation_point["elapsed_days"],
            line_dash="dash",
            line_color="red",
            annotation_text=f"degradation_point(elapsed_days={degradation_point['elapsed_days']})",
        )

    figure.update_layout(
        title=f"모델 성능 열화 곡선 (cutoff={result['cutoff_date']})",
        xaxis_title="경과일(elapsed_days, cutoff 기준 달력 일수)",
        yaxis_title="ROC-AUC",
    )
    return figure


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", required=True, help="measure-degradation 결과 JSON 경로")
    parser.add_argument("--output", required=True, help="저장할 HTML 경로")
    args = parser.parse_args()

    result = json.loads(Path(args.result).read_text(encoding="utf-8"))
    figure = build_figure(result)
    figure.write_html(args.output)
    print(f"[저장] {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
