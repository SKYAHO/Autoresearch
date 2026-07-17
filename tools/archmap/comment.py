"""pr-delta 사실만으로 PR 요약 코멘트(마크다운)를 만든다 — 서버 없이도 성립."""
from __future__ import annotations

MARKER = "<!-- archmap-report -->"


def _cell(value: object) -> str:
    """표 셀 값을 이스케이프한다 — `|`는 열 구분을 깨고 개행은 행을 쪼갠다."""
    text = str(value)
    return text.replace("|", r"\|").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def render_comment(delta: dict, report_url: str | None) -> str:
    lines = [MARKER, "## 🗺️ PR 이해 리포트 — 결정론 사실 요약", ""]

    stages = sorted({m["stage"] for m in delta["changed_modules"]})
    if stages:
        lines.append(f'**흐름 위치**: `{" · ".join(stages)}` 스테이지를 변경합니다.')
    mods = ", ".join(f'`{m["id"]}`' for m in delta["changed_modules"])
    if mods:
        lines.append(f"**변경 모듈**: {mods}")
    lines.append("")

    rows = []
    for u in delta["unchanged_contracts"]:
        rows.append(f'| ✅ 검증됨 | `{_cell(u["const"])}` = `{_cell(u["value"])}` 불변 |')
    for v in delta["version_changes"]:
        mark = "⚠️ 파괴적" if v["breaking"] else "🔵 비파괴"
        rows.append(f'| {mark} | `{_cell(v["const"])}`: `{_cell(v["from"])}` → `{_cell(v["to"])}` |')
    for s in delta["schema_changes"]:
        mark = "⚠️ 파괴적" if s["breaking"] else "🔵 비파괴"
        rows.append(f'| {mark} | `{_cell(s["model"])}.{_cell(s["field"])}` {_cell(s["change"])} |')
    for x in delta["cross_repo"]:
        mark = "⚠️ 파괴적" if x["breaking"] else "🔵 비파괴"
        rows.append(f'| {mark} | `{_cell(x["contract"])}` {_cell(x["impact"])}'
                    f' — {_cell(x.get("details", ""))} |')
    for b in delta.get("breaking_signatures", []):
        rows.append(f'| ⚠️ 파괴적 | `{_cell(b["module"])}.{_cell(b["name"])}`'
                    f' 시그니처가 하위호환되지 않습니다 |')
    if rows:
        lines += ["| 판정 | 계약 · 영향 |", "| --- | --- |", *rows, ""]

    t = delta["tests"]
    lines.append(f'**테스트**: {len(t["files"])}개 파일 변경, +{t["lines_added"]}줄')
    lines.append("")
    if report_url:
        lines.append(f"[전체 이해 리포트 보기]({report_url})")
    else:
        lines.append("_아카이브 서버 미연결 — 위 결정론 사실 요약만 제공합니다._")
    return "\n".join(lines) + "\n"
