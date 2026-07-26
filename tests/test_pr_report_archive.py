from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPOSITORY_ROOT / ".github" / "pr-report" / "build_archive.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("build_archive", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_report(
    root: Path,
    number: int,
    *,
    schema_version: int = 1,
    summary: list[str] | None = None,
) -> Path:
    report_dir = root / "pr" / str(number)
    report_dir.mkdir(parents=True)
    payload = {
        "schema_version": schema_version,
        "pr": {
            "number": number,
            "title": f"snapshot {number}",
            "author": "snapshot",
        },
        "summary_ko": summary or ["요약 1", "요약 2", "요약 3"],
    }
    path = report_dir / "index.html"
    path.write_text(
        '<script id="report-data" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script>",
        encoding="utf-8",
    )
    return path


def test_discovers_only_numeric_pr_report_pages(tmp_path):
    module = _load_module()
    expected = _write_report(tmp_path, 345)
    (tmp_path / "pr" / "draft").mkdir(parents=True)
    (tmp_path / "pr" / "draft" / "index.html").write_text(
        "ignored", encoding="utf-8"
    )

    assert module.discover_report_pages(tmp_path) == {345: expected}


@pytest.mark.parametrize("schema_version", [1, 2])
def test_extracts_common_fields_from_v1_and_v2(tmp_path, schema_version):
    module = _load_module()
    path = _write_report(tmp_path, 345, schema_version=schema_version)

    snapshot = module.extract_report_snapshot(path)

    assert snapshot.number == 345
    assert snapshot.summary_ko == ("요약 1", "요약 2", "요약 3")


def test_rejects_invalid_json_and_non_three_line_summary(tmp_path):
    module = _load_module()
    invalid = tmp_path / "invalid.html"
    invalid.write_text(
        '<script id="report-data" type="application/json">{</script>',
        encoding="utf-8",
    )
    short = _write_report(tmp_path, 346, summary=["한 줄"])

    with pytest.raises(module.ArchiveBuildError, match="JSON"):
        module.extract_report_snapshot(invalid)
    with pytest.raises(module.ArchiveBuildError, match="summary_ko"):
        module.extract_report_snapshot(short)


def test_rejects_report_number_that_disagrees_with_directory(tmp_path):
    module = _load_module()
    path = _write_report(tmp_path, 345)
    html = path.read_text(encoding="utf-8").replace('"number": 345', '"number": 344')
    path.write_text(html, encoding="utf-8")

    with pytest.raises(module.ArchiveBuildError, match="PR number"):
        module.extract_report_snapshot(path)


def test_fetches_only_merged_pull_requests(monkeypatch):
    module = _load_module()
    response = [
        [
            {
                "number": 344,
                "title": "closed",
                "user": {"login": "alice"},
                "merged_at": None,
            },
            {
                "number": 345,
                "title": "merged",
                "user": {"login": "bob"},
                "merged_at": "2026-07-25T12:00:00Z",
            },
        ]
    ]
    monkeypatch.setattr(module, "run", lambda command: json.dumps(response))

    merged = module.fetch_merged_pull_requests("SKYAHO/Autoresearch")

    assert merged == {
        345: module.PullRequestMetadata(
            number=345,
            title="merged",
            author="bob",
            merged_at="2026-07-25T12:00:00Z",
        )
    }


def test_builds_only_merged_entries_and_sorts_newest_first(tmp_path):
    module = _load_module()
    _write_report(tmp_path, 344)
    _write_report(tmp_path, 345, schema_version=2)
    merged = {
        344: module.PullRequestMetadata(
            344, "JSON 전환", "alice", "2026-07-24T12:00:00Z"
        ),
        345: module.PullRequestMetadata(
            345, "문서 갱신", "bob", "2026-07-25T12:00:00Z"
        ),
    }

    entries = module.build_archive_entries(tmp_path, merged)

    assert [entry.number for entry in entries] == [345, 344]
    assert entries[0].title == "문서 갱신"
    assert entries[0].report_url == "pr/345/"


def test_does_not_parse_corrupt_unmerged_report(tmp_path):
    module = _load_module()
    corrupt = tmp_path / "pr" / "340"
    corrupt.mkdir(parents=True)
    (corrupt / "index.html").write_text("broken", encoding="utf-8")
    _write_report(tmp_path, 345)
    merged = {
        345: module.PullRequestMetadata(
            345, "정상", "bob", "2026-07-25T12:00:00Z"
        )
    }

    entries = module.build_archive_entries(tmp_path, merged)

    assert [entry.number for entry in entries] == [345]


def test_fails_when_merged_report_is_corrupt(tmp_path):
    module = _load_module()
    corrupt = tmp_path / "pr" / "345"
    corrupt.mkdir(parents=True)
    (corrupt / "index.html").write_text("broken", encoding="utf-8")
    merged = {
        345: module.PullRequestMetadata(
            345, "정상", "bob", "2026-07-25T12:00:00Z"
        )
    }

    with pytest.raises(module.ArchiveBuildError, match="345"):
        module.build_archive_entries(tmp_path, merged)


def test_writes_json_html_and_javascript_without_script_breakout(tmp_path):
    module = _load_module()
    template = tmp_path / "template.html"
    template.write_text(
        '<script id="archive-data" type="application/json">'
        "/*__ARCHIVE_DATA__*/</script>",
        encoding="utf-8",
    )
    javascript = tmp_path / "archive.js"
    javascript.write_text("window.archiveLoaded = true;", encoding="utf-8")
    entry = module.ArchiveEntry(
        number=345,
        title="안전성 </script><script>alert(1)</script>",
        author="bob",
        merged_at="2026-07-25T12:00:00Z",
        summary_ko=("요약 1", "요약 2", "요약 3"),
        report_url="pr/345/",
    )
    payload = module.serialize_archive([entry], "2026-07-26T00:00:00Z")

    module.write_archive(tmp_path / "out", template, javascript, payload)

    archive = json.loads(
        (tmp_path / "out" / "archive.json").read_text(encoding="utf-8")
    )
    html = (tmp_path / "out" / "index.html").read_text(encoding="utf-8")
    assert archive["schema_version"] == 1
    assert archive["generated_at"] == "2026-07-26T00:00:00Z"
    assert archive["reports"][0]["number"] == 345
    assert "</script><script>alert(1)" not in html
    assert "<\\/script><script>alert(1)" in html
    assert (
        tmp_path / "out" / "archive.js"
    ).read_text(encoding="utf-8") == "window.archiveLoaded = true;"


def test_refuses_template_without_archive_placeholder(tmp_path):
    module = _load_module()
    template = tmp_path / "template.html"
    template.write_text("<html></html>", encoding="utf-8")

    with pytest.raises(module.ArchiveBuildError, match="ARCHIVE_DATA"):
        module.render_archive(template, {"schema_version": 1, "reports": []})


def test_cli_writes_complete_archive(tmp_path):
    module = _load_module()
    _write_report(tmp_path / "pages", 345, schema_version=2)
    template = tmp_path / "template.html"
    template.write_text("/*__ARCHIVE_DATA__*/", encoding="utf-8")
    javascript = tmp_path / "archive.js"
    javascript.write_text('"use strict";', encoding="utf-8")
    merged = {
        345: module.PullRequestMetadata(
            345, "완료", "bob", "2026-07-25T12:00:00Z"
        )
    }

    exit_code = module.main(
        [
            "--pages-root",
            str(tmp_path / "pages"),
            "--template",
            str(template),
            "--javascript",
            str(javascript),
            "--output-dir",
            str(tmp_path / "site"),
            "--repository",
            "SKYAHO/Autoresearch",
        ],
        merged_prs=merged,
    )

    assert exit_code == 0
    archive = json.loads(
        (tmp_path / "site" / "archive.json").read_text(encoding="utf-8")
    )
    assert [report["number"] for report in archive["reports"]] == [345]
    assert (tmp_path / "site" / "index.html").exists()
    assert (tmp_path / "site" / "archive.js").exists()


def test_cli_keeps_existing_output_when_generation_fails(tmp_path):
    module = _load_module()
    site = tmp_path / "site"
    site.mkdir()
    existing = site / "index.html"
    existing.write_text("last-known-good", encoding="utf-8")

    exit_code = module.main(
        [
            "--pages-root",
            str(tmp_path / "missing"),
            "--template",
            str(tmp_path / "missing-template"),
            "--javascript",
            str(tmp_path / "missing-js"),
            "--output-dir",
            str(site),
            "--repository",
            "SKYAHO/Autoresearch",
        ],
        merged_prs={},
    )

    assert exit_code == 1
    assert existing.read_text(encoding="utf-8") == "last-known-good"
