from __future__ import annotations

import json
import subprocess
from pathlib import Path

ARCHIVE_JS = (
    Path(__file__).resolve().parents[1] / ".github" / "pr-report" / "archive.js"
)


def _matches_category(entry: dict, category: str) -> bool:
    script = (
        f"const archive=require({json.dumps(str(ARCHIVE_JS))});"
        "process.stdout.write(String(archive.matchesCategory("
        f"{json.dumps(entry, ensure_ascii=False)},"
        f"{json.dumps(category, ensure_ascii=False)})));"
    )
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout == "true"


def test_all_category_matches_every_entry():
    assert _matches_category({"category": "application"}, "all")
    assert _matches_category({"category": "airflow"}, "all")


def test_specific_category_matches_only_itself():
    assert _matches_category({"category": "application"}, "application")
    assert not _matches_category({"category": "airflow"}, "application")
    assert _matches_category({"category": "airflow"}, "airflow")
    assert not _matches_category({"category": "application"}, "airflow")
