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


# ---------------------------------------------------------------------------
# fmtDate
# ---------------------------------------------------------------------------


def test_fmt_date_renders_korean_year_month_day():
    assert _call("fmtDate", "2026-07-27T11:36:08Z") == "2026년 7월 27일"


def test_fmt_date_does_not_zero_pad_month_or_day():
    assert _call("fmtDate", "2026-01-05T00:00:00Z") == "2026년 1월 5일"


# ---------------------------------------------------------------------------
# highlightParts
# ---------------------------------------------------------------------------


def test_highlight_parts_blank_query_returns_single_plain_segment():
    assert _call("highlightParts", "ONNX 배포", "") == [{"text": "ONNX 배포", "hit": False}]


def test_highlight_parts_marks_matching_substring_case_insensitively():
    result = _call("highlightParts", "ONNX 배포 문서", "onnx")
    assert result == [
        {"text": "ONNX", "hit": True},
        {"text": " 배포 문서", "hit": False},
    ]


def test_highlight_parts_marks_every_occurrence():
    result = _call("highlightParts", "a-a-a", "a")
    assert [p["hit"] for p in result] == [True, False, True, False, True]


# ---------------------------------------------------------------------------
# sortEntries
# ---------------------------------------------------------------------------

_APP = {"number": 10, "category": "application", "merged_at": "2026-07-20T00:00:00Z"}
_AIR_LATER = {"number": 5, "category": "airflow", "merged_at": "2026-07-26T00:00:00Z"}
_AIR_SAME_DAY = {"number": 4, "category": "airflow", "merged_at": "2026-07-20T00:00:00Z"}


def test_sort_entries_recent_orders_by_merged_at_desc_then_number_desc():
    result = _call("sortEntries", [_APP, _AIR_LATER, _AIR_SAME_DAY], "recent")
    assert [e["number"] for e in result] == [5, 10, 4]


def test_sort_entries_repo_groups_by_category_before_date():
    result = _call("sortEntries", [_AIR_LATER, _APP, _AIR_SAME_DAY], "repo")
    # "airflow" < "application" alphabetically, so airflow entries come first,
    # sorted by merged_at desc within the category.
    assert [e["category"] for e in result] == ["airflow", "airflow", "application"]
    assert [e["number"] for e in result[:2]] == [5, 4]


def test_sort_entries_does_not_mutate_input():
    original = [dict(_APP), dict(_AIR_LATER)]
    snapshot = json.loads(json.dumps(original))
    _call("sortEntries", original, "recent")
    assert original == snapshot


# ---------------------------------------------------------------------------
# groupEntries
# ---------------------------------------------------------------------------


def test_group_entries_recent_groups_consecutive_same_day_entries():
    sorted_entries = [_AIR_LATER, _APP, _AIR_SAME_DAY]  # already date-desc
    groups = _call("groupEntries", sorted_entries, "recent")
    assert [g["key"] for g in groups] == ["2026-07-26", "2026-07-20"]
    assert [e["number"] for e in groups[1]["items"]] == [10, 4]


def test_group_entries_repo_groups_by_category():
    sorted_entries = [_AIR_LATER, _AIR_SAME_DAY, _APP]
    groups = _call("groupEntries", sorted_entries, "repo")
    assert [g["key"] for g in groups] == ["airflow", "application"]
    assert len(groups[0]["items"]) == 2


# ---------------------------------------------------------------------------
# pickSelectedId
# ---------------------------------------------------------------------------


def test_pick_selected_id_keeps_current_selection_if_still_present():
    entries = [_APP, _AIR_LATER]
    assert _call("pickSelectedId", entries, 5) == 5


def test_pick_selected_id_falls_back_to_first_entry_if_selection_filtered_out():
    entries = [_APP, _AIR_LATER]
    assert _call("pickSelectedId", entries, 999) == 10


def test_pick_selected_id_returns_null_for_empty_list():
    assert _call("pickSelectedId", [], 5) is None
