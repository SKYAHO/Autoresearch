from __future__ import annotations

import json
import subprocess
from pathlib import Path

ARCHIVE_JS = (
    Path(__file__).resolve().parents[1] / ".github" / "pr-report" / "archive.js"
)


def _sanitize(entries: list[dict]) -> list[bool]:
    """Return whether each entry survives sanitizeEntry() (True) or is dropped (False)."""
    script = (
        f"const archive=require({json.dumps(str(ARCHIVE_JS))});"
        f"var entries={json.dumps(entries, ensure_ascii=False)};"
        "var outcomes = entries.map(function (entry) {"
        "  return archive.sanitizeEntry(entry) !== null;"
        "});"
        "process.stdout.write(JSON.stringify(outcomes));"
    )
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


_GOOD_ENTRY = {
    "number": 372,
    "category": "airflow",
    "title": "테스트 PR",
    "author": "octocat",
    "merged_at": "2026-07-27T00:00:00Z",
    "summary_ko": ["요약 한 줄"],
    "report_url": "pr/372/",
}


def test_malformed_entry_missing_summary_ko_is_dropped_not_thrown():
    # Regression for #372 Finding 1: a malformed entry (missing summary_ko)
    # must not raise out of the render pipeline; sanitizeEntry() isolates it
    # to a `null` result so render() can skip just this one entry.
    bad_entry = dict(_GOOD_ENTRY)
    del bad_entry["summary_ko"]
    assert _sanitize([bad_entry]) == [False]


def test_valid_entries_around_a_malformed_one_still_survive():
    # The malformed entry must not affect sibling entries: this is the crux
    # of "entry-level error isolation" from the spec's 오류 처리 section.
    bad_entry = dict(_GOOD_ENTRY)
    bad_entry["number"] = 373
    del bad_entry["summary_ko"]
    entries = [_GOOD_ENTRY, bad_entry, dict(_GOOD_ENTRY, number=374)]
    assert _sanitize(entries) == [True, False, True]


def test_well_formed_entry_survives():
    assert _sanitize([_GOOD_ENTRY]) == [True]


def test_missing_entry_fields_are_each_individually_rejected():
    for field in ("number", "title", "author", "merged_at", "summary_ko"):
        bad_entry = dict(_GOOD_ENTRY)
        del bad_entry[field]
        assert _sanitize([bad_entry]) == [False], f"missing {field} should be rejected"


# ---------------------------------------------------------------------------
# Defense-in-depth: even if a malformed entry somehow reached DOM building
# (e.g. sanitizeEntry's checks are loosened later), buildRailItemSafely and
# buildDetailContentSafely must still isolate a thrown error to `null` rather
# than let it propagate and blank the whole list.
# ---------------------------------------------------------------------------

_DOCUMENT_STUB = """
function makeElement(tag) {
  return {
    tagName: tag,
    className: "",
    textContent: "",
    href: "",
    style: {},
    children: [],
    attrs: {},
    appendChild: function (child) {
      this.children.push(child);
      return child;
    },
    setAttribute: function (name, value) {
      this.attrs[name] = value;
    },
    addEventListener: function () {},
  };
}
global.document = {
  createElement: makeElement,
  createTextNode: function (text) { return { text: text }; },
};
"""


def test_build_rail_item_safely_isolates_a_throwing_entry():
    script = (
        f"const archive=require({json.dumps(str(ARCHIVE_JS))});"
        f"{_DOCUMENT_STUB}"
        "var bad = { get title() { throw new Error('boom'); } };"
        "var ok = archive.buildRailItemSafely(bad, '', false, '#000') === null;"
        "process.stdout.write(String(ok));"
    )
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert result.stdout == "true"


def test_build_detail_content_safely_isolates_a_throwing_entry():
    script = (
        f"const archive=require({json.dumps(str(ARCHIVE_JS))});"
        f"{_DOCUMENT_STUB}"
        "var bad = { number: 1, category: 'airflow', title: 't', author: 'a',"
        " merged_at: '2026-07-27T00:00:00Z', get summary_ko() { throw new Error('boom'); } };"
        "var ok = archive.buildDetailContentSafely(bad, '', '#000', 'Repo', function () {}) === null;"
        "process.stdout.write(String(ok));"
    )
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert result.stdout == "true"
