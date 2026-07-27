from __future__ import annotations

import json
import subprocess
from pathlib import Path

ARCHIVE_JS = (
    Path(__file__).resolve().parents[1] / ".github" / "pr-report" / "archive.js"
)


def _call(function_name: str, *args: object) -> object:
    args_json = ",".join(json.dumps(arg, ensure_ascii=False) for arg in args)
    script = (
        f"const archive=require({json.dumps(str(ARCHIVE_JS))});"
        f"process.stdout.write(JSON.stringify(archive.{function_name}({args_json})));"
    )
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_tag_category_adds_field_without_mutating_input():
    entries = [{"number": 1}, {"number": 2}]
    tagged = _call("tagCategory", entries, "airflow")
    assert tagged == [
        {"number": 1, "category": "airflow"},
        {"number": 2, "category": "airflow"},
    ]


def test_absolutize_report_url_prefixes_base():
    entries = [{"report_url": "pr/153/"}]
    absolute = _call(
        "absolutizeReportUrl",
        entries,
        "https://skyaho.github.io/Autoresearch-airflow/",
    )
    assert absolute == [
        {"report_url": "https://skyaho.github.io/Autoresearch-airflow/pr/153/"}
    ]


def test_merge_and_sort_reports_orders_by_merged_at_desc_then_number_desc():
    application = [{"number": 10, "merged_at": "2026-07-20T00:00:00Z"}]
    airflow = [
        {"number": 5, "merged_at": "2026-07-26T00:00:00Z"},
        {"number": 4, "merged_at": "2026-07-20T00:00:00Z"},
    ]
    merged = _call("mergeAndSortReports", [application, airflow])
    assert [entry["number"] for entry in merged] == [5, 10, 4]
