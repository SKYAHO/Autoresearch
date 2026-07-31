"""정책 시뮬레이션 라운드 리포트의 자기완결 HTML 렌더러.

[파이프라인] 정책 노출·판정·event log 생성 뒤, 라운드 결과를 사람이 읽을 수
있는 비교 리포트로 변환하는 구간이다.

[기능] 외부 의존성·네트워크 요청 없이 정책별 스탯 타일·가로 막대·데이터
테이블을 가진 단일 HTML 문서를 만든다. baseline/model의 기존 파랑·초록을
선두로 하는 고정 팔레트를 정책 입력 순서대로 적용하고 반복 판정 신뢰도
경고를 표시한다.

[비책임] 정책 선택, 이벤트 생성, JSON 리포트 저장은
`src.pipeline.simulate_policy_round`가 담당한다.
"""

from __future__ import annotations

from collections.abc import Callable
from html import escape

_CSS = """
:root {
  color-scheme: light;
  --surface: #fcfcfb; --card: #ffffff; --border: #e4e3df;
  --text-primary: #0b0b0b; --text-secondary: #52514e;
  --series-0: #2a78d6; --series-1: #008300; --series-2: #b05a00;
  --series-3: #7b55c7; --series-4: #b52f67; --series-5: #007f83;
  --track: #eeede9;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --surface: #1a1a19; --card: #232322; --border: #3a3a38;
    --text-primary: #ffffff; --text-secondary: #c3c2b7;
    --series-0: #3987e5; --series-1: #30a84a; --series-2: #e18a3a;
    --series-3: #a98ae6; --series-4: #e16a9c; --series-5: #3db5b8;
    --track: #2e2e2c;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface: #1a1a19; --card: #232322; --border: #3a3a38;
  --text-primary: #ffffff; --text-secondary: #c3c2b7;
  --series-0: #3987e5; --series-1: #30a84a; --series-2: #e18a3a;
  --series-3: #a98ae6; --series-4: #e16a9c; --series-5: #3db5b8;
  --track: #2e2e2c;
}
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; background: var(--surface); color: var(--text-primary);
       font: 14px/1.5 system-ui, sans-serif; }
h1 { font-size: 20px; margin: 0 0 4px; }
.meta { color: var(--text-secondary); margin-bottom: 20px; }
.tiles { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 24px; }
.tile { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
        padding: 12px 16px; min-width: 140px; }
.tile .label { color: var(--text-secondary); font-size: 12px; }
.tile .value { font-size: 24px; font-weight: 600; }
.chart { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
         padding: 16px; margin-bottom: 16px; max-width: 640px; }
.chart h2 { font-size: 14px; margin: 0 0 12px; }
.row { display: flex; align-items: center; gap: 8px; margin-bottom: 2px; }
.row .name { width: 90px; color: var(--text-secondary); flex-shrink: 0; }
.row .track { flex: 1; background: var(--track); height: 20px; border-radius: 0 4px 4px 0; }
.row .fill { height: 100%; border-radius: 0 4px 4px 0; min-width: 2px; }
.row .val { width: 70px; text-align: right; font-variant-numeric: tabular-nums; }
.legend { display: flex; gap: 16px; margin: 4px 0 16px; color: var(--text-secondary); font-size: 12px; }
.chip { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 4px; }
table { border-collapse: collapse; margin-top: 8px; }
th, td { border: 1px solid var(--border); padding: 6px 10px; text-align: right; }
th:first-child, td:first-child { text-align: left; }
"""


def _pct(value: float) -> str:
    """0.025 → '2.50%' 표기."""
    return f"{value * 100:.2f}%"


def _bar_rows(values: dict[str, float], fmt: Callable[[float], str]) -> str:
    """정책별 가로 막대 행 HTML. 최대값 기준 폭, 값은 직접 라벨."""
    vmax = max(values.values()) or 1.0
    rows = []
    for index, (policy, value) in enumerate(values.items()):
        width = max(1.0, value / vmax * 100)
        rows.append(
            f'<div class="row" title="{escape(policy)}: {escape(fmt(value))}">'
            f'<span class="name">{escape(policy)}</span>'
            f'<span class="track"><span class="fill" '
            f'style="width:{width:.1f}%;background:var(--series-{index % 6})">'
            "</span></span>"
            f'<span class="val">{escape(fmt(value))}</span></div>'
        )
    return "".join(rows)


def render_report_html(report: dict) -> str:
    """리포트 dict를 자기완결 HTML 문서 문자열로 렌더링한다."""

    policies = report["policies"]
    policy_names = list(policies)
    if len(policy_names) < 2:
        raise ValueError("HTML 리포트는 비교할 정책이 두 개 이상 필요합니다")
    legend = '<div class="legend">' + "".join(
        f'<span><span class="chip" '
        f'style="background:var(--series-{index % 6})"></span>'
        f"{escape(name)}</span>"
        for index, name in enumerate(policy_names)
    ) + "</div>"

    if len(policy_names) == 2:
        baseline_name = "baseline" if "baseline" in policies else policy_names[0]
        model_name = "model" if "model" in policies else next(
            name for name in policy_names if name != baseline_name
        )
        baseline, model = policies[baseline_name], policies[model_name]
        lift = model["ctr"] - baseline["ctr"]
        tile_values = (
            (f"{model_name} CTR", _pct(model["ctr"])),
            (f"{baseline_name} CTR", _pct(baseline["ctr"])),
            ("CTR lift", f"{'+' if lift >= 0 else ''}{_pct(lift)}"),
            ("노출 겹침 (Jaccard)", f"{report['overlap_jaccard_mean']:.2f}"),
            ("유저 수", str(report["users"])),
        )
        explo_imps = model["exploration_impressions"]
        explo_ctr = model["exploration_clicks"] / explo_imps if explo_imps else 0.0
        exploration_summary = (
            f"exploration CTR ({escape(model_name)}): "
            f"{escape(_pct(explo_ctr))}"
        )
    else:
        tile_values = (
            *(
                (f"{name} CTR", _pct(policy["ctr"]))
                for name, policy in policies.items()
            ),
            ("노출 겹침 평균 (Jaccard)", f"{report['overlap_jaccard_mean']:.2f}"),
            ("유저 수", str(report["users"])),
        )
        exploration_values = []
        for name, policy in policies.items():
            impressions = policy["exploration_impressions"]
            ctr = (
                policy["exploration_clicks"] / impressions
                if impressions
                else 0.0
            )
            exploration_values.append(f"{escape(name)}={escape(_pct(ctr))}")
        exploration_summary = "exploration CTRs: " + ", ".join(
            exploration_values
        )

    tiles = "".join(
        f'<div class="tile"><div class="label">{escape(label)}</div>'
        f'<div class="value">{escape(value)}</div></div>'
        for label, value in tile_values
    )
    table_rows = "".join(
        f"<tr><td>{escape(name)}</td><td>{p['impressions']}</td><td>{p['clicks']}</td>"
        f"<td>{escape(_pct(p['ctr']))}</td><td>{p['mean_click_propensity']:.4f}</td>"
        f"<td>{p['exploration_impressions']}</td><td>{p['exploration_clicks']}</td>"
        f"<td>{p.get('judgment_repeats', 1)}</td>"
        f"<td>{escape(_pct(float(p.get('ctr_mean', p['ctr']))))}</td></tr>"
        for name, p in policies.items()
    )
    reliability = report.get("reliability", {})
    reliability_warning = reliability.get("warning")
    reliability_summary = (
        f"judgment repeats: {report.get('judgment_repeats', 1)} · "
        f"unique intended impressions: "
        f"{reliability.get('unique_intended_impressions', '-') }"
    )
    if reliability_warning:
        reliability_summary += f" · 신뢰도 경고: {escape(str(reliability_warning))}"
    overlap_by_pair = report.get("overlap_jaccard_by_pair", {})
    pairwise_section = ""
    if len(policy_names) > 2 and overlap_by_pair:
        pair_rows = "".join(
            f"<tr><td>{escape(pair.replace('|', ' ↔ ', 1))}</td>"
            f"<td>{float(value):.4f}</td></tr>"
            for pair, value in overlap_by_pair.items()
        )
        pairwise_section = f"""<div class="chart"><h2>정책 쌍별 노출 겹침</h2>
<table>
<tr><th>policy pair</th><th>Jaccard mean</th></tr>
{pair_rows}
</table></div>"""
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>정책 시뮬레이션 라운드 리포트</title>
<style>{_CSS}</style>
</head>
<body>
<h1>정책 시뮬레이션 라운드 리포트</h1>
<div class="meta">policy_version={escape(str(report["policy_version"]))} ·
k={report["k"]} · ε={report["exploration_ratio"]} · click_threshold={report["click_threshold"]} ·
seed={report["seed"]}</div>
<div class="tiles">{tiles}</div>
{legend}
<div class="chart"><h2>정책별 CTR (합동 커트라인 판정 후)</h2>
{_bar_rows({name: policy["ctr"] for name, policy in policies.items()}, _pct)}</div>
<div class="chart"><h2>정책별 평균 click propensity (커트라인 판정 전 raw)</h2>
{_bar_rows({name: policy["mean_click_propensity"] for name, policy in policies.items()}, lambda v: f"{v:.4f}")}</div>
<div class="chart"><h2>데이터 테이블</h2>
<table>
<tr><th>policy</th><th>impressions</th><th>clicks</th><th>CTR</th>
<th>mean propensity</th><th>explo imps</th><th>explo clicks</th>
<th>judgment repeats</th><th>CTR mean</th></tr>
{table_rows}
</table>
<p class="meta">{exploration_summary} ·
skipped users: {len(report["skipped_users"])} ·
dropped exposures: {report["dropped_exposures_without_judgment"]} ·
quarantined chunks: {report["quarantined_chunks"]} ·
replay: {str(bool(report.get("replay", False))).lower()} ·
llm_model={escape(str(report.get("llm_model", "-")))} ·
{reliability_summary}</p></div>
{pairwise_section}
</body>
</html>
"""
