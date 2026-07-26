from __future__ import annotations

import json
import subprocess
from pathlib import Path

ARCHIVE_JS = (
    Path(__file__).resolve().parents[1] / ".github" / "pr-report" / "archive.js"
)


def _matches(query: str) -> bool:
    entry = {
        "number": 345,
        "title": "ONNX 배포 문서 갱신",
        "author": "Waieiches",
    }
    script = (
        f"const archive=require({json.dumps(str(ARCHIVE_JS))});"
        "process.stdout.write(String(archive.matchesArchiveEntry("
        f"{json.dumps(entry, ensure_ascii=False)},"
        f"{json.dumps(query, ensure_ascii=False)})));"
    )
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout == "true"


def test_search_matches_number_title_and_author_case_insensitively():
    assert _matches("345")
    assert _matches("onnx")
    assert _matches("WAIEICHES")
    assert not _matches("redis")


def test_blank_search_matches_every_entry():
    assert _matches("")
    assert _matches("   ")
