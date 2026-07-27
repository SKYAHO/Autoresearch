from __future__ import annotations

import json
import subprocess
from pathlib import Path

ARCHIVE_JS = (
    Path(__file__).resolve().parents[1] / ".github" / "pr-report" / "archive.js"
)

# archive.js's createReportCard()/render() run in a browser DOM. This module
# has no jsdom dependency, so tests stub only the DOM surface createReportCard
# actually touches (document.createElement) rather than pulling in a full DOM
# library. The stub must be installed AFTER require() — archive.js's
# bottom-of-file bootstrap calls initializeArchive() immediately if `document`
# is already defined when the module loads, and that path needs
# document.getElementById, which this minimal stub does not provide.
_DOCUMENT_STUB = """
function makeElement(tag) {
  return {
    tagName: tag,
    className: "",
    textContent: "",
    href: "",
    children: [],
    attrs: {},
    appendChild: function (child) {
      this.children.push(child);
      return child;
    },
    setAttribute: function (name, value) {
      this.attrs[name] = value;
    },
  };
}
global.document = { createElement: makeElement };
"""


def _build_card_outcomes(entries: list[dict]) -> list[str]:
    """Return "card" or "null" for each entry, as produced by buildReportCardSafely."""
    script = (
        f"const archive=require({json.dumps(str(ARCHIVE_JS))});"
        f"{_DOCUMENT_STUB}"
        f"var entries={json.dumps(entries, ensure_ascii=False)};"
        "var outcomes = entries.map(function (entry) {"
        "  return archive.buildReportCardSafely(entry) ? 'card' : 'null';"
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


def test_malformed_entry_missing_summary_ko_is_skipped_not_thrown():
    # Regression for #372 Finding 1: a malformed entry (missing summary_ko)
    # must not raise out of buildReportCardSafely; it must be isolated to a
    # `null` result so the caller (render()) can skip just this one entry.
    bad_entry = dict(_GOOD_ENTRY)
    del bad_entry["summary_ko"]
    outcomes = _build_card_outcomes([bad_entry])
    assert outcomes == ["null"]


def test_valid_entries_around_a_malformed_one_still_build_cards():
    # The malformed entry must not affect sibling entries: this is the crux
    # of "entry-level error isolation" from the spec's 오류 처리 section.
    bad_entry = dict(_GOOD_ENTRY)
    bad_entry["number"] = 373
    del bad_entry["summary_ko"]
    entries = [_GOOD_ENTRY, bad_entry, dict(_GOOD_ENTRY, number=374)]
    outcomes = _build_card_outcomes(entries)
    assert outcomes == ["card", "null", "card"]


def test_well_formed_entry_still_builds_a_card():
    outcomes = _build_card_outcomes([_GOOD_ENTRY])
    assert outcomes == ["card"]
