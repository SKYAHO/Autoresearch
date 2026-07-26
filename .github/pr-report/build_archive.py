"""Build the merged-PR report archive data for the CI/reporting stage.

This module owns the deterministic boundary between immutable PR report pages
on ``gh-pages`` and the merged-only archive published at the Pages root. It
discovers report pages, reads their embedded schema-v1/v2 JSON snapshots,
combines them with authoritative GitHub merge metadata, and returns validated
archive entries. Static rendering and the command-line deployment entry point
are added in later sections of this module; it never generates PR analysis or
modifies individual ``pr/<number>/`` reports.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Mapping, Sequence

ARCHIVE_PLACEHOLDER = "/*__ARCHIVE_DATA__*/"


class ArchiveBuildError(RuntimeError):
    """Prevent publication of an incomplete or corrupt archive."""


@dataclass(frozen=True)
class ReportSnapshot:
    """Common archive fields embedded in report schema v1 and v2 pages."""

    number: int
    summary_ko: tuple[str, str, str]


@dataclass(frozen=True)
class PullRequestMetadata:
    """Authoritative metadata for a merged GitHub pull request."""

    number: int
    title: str
    author: str
    merged_at: str


@dataclass(frozen=True)
class ArchiveEntry:
    """One merged report card in the public archive."""

    number: int
    title: str
    author: str
    merged_at: str
    summary_ko: tuple[str, str, str]
    report_url: str


class _ReportDataParser(HTMLParser):
    """Extract JSON text from the report page's canonical data script."""

    def __init__(self) -> None:
        super().__init__()
        self._inside_report_data = False
        self._current_parts: list[str] = []
        self.payloads: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        if tag == "script" and values.get("id") == "report-data":
            if self._inside_report_data:
                raise ArchiveBuildError("nested report-data script")
            self._inside_report_data = True
            self._current_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._inside_report_data:
            self.payloads.append("".join(self._current_parts))
            self._inside_report_data = False
            self._current_parts = []

    def handle_data(self, data: str) -> None:
        if self._inside_report_data:
            self._current_parts.append(data)


def run(command: list[str]) -> str:
    """Run one external command and return stdout."""

    return subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


def discover_report_pages(pages_root: Path) -> dict[int, Path]:
    """Return numeric ``pr/<number>/index.html`` pages keyed by PR number."""

    reports_root = pages_root / "pr"
    if not reports_root.is_dir():
        return {}

    discovered: dict[int, Path] = {}
    for directory in reports_root.iterdir():
        if not directory.is_dir() or not directory.name.isdecimal():
            continue
        report_path = directory / "index.html"
        if report_path.is_file():
            discovered[int(directory.name)] = report_path
    return discovered


def _parse_report_payload(path: Path) -> dict:
    parser = _ReportDataParser()
    try:
        parser.feed(path.read_text(encoding="utf-8"))
        parser.close()
    except (OSError, UnicodeError) as error:
        raise ArchiveBuildError(f"cannot read report HTML {path}: {error}") from error

    if len(parser.payloads) != 1:
        raise ArchiveBuildError(
            f"report HTML {path} must contain exactly one report-data script"
        )
    try:
        payload = json.loads(parser.payloads[0])
    except json.JSONDecodeError as error:
        raise ArchiveBuildError(f"invalid report JSON in {path}: {error}") from error
    if not isinstance(payload, dict):
        raise ArchiveBuildError(f"report JSON in {path} must be an object")
    return payload


def extract_report_snapshot(path: Path) -> ReportSnapshot:
    """Validate and read common archive fields from one report HTML page."""

    payload = _parse_report_payload(path)
    pr = payload.get("pr")
    if not isinstance(pr, dict):
        raise ArchiveBuildError(f"report JSON in {path} has no object field 'pr'")

    number = pr.get("number")
    try:
        path_number = int(path.parent.name)
    except ValueError as error:
        raise ArchiveBuildError(f"report path has no numeric PR number: {path}") from error
    if not isinstance(number, int) or isinstance(number, bool) or number != path_number:
        raise ArchiveBuildError(
            f"PR number in {path} does not match directory {path_number}"
        )

    summary = payload.get("summary_ko")
    if (
        not isinstance(summary, list)
        or len(summary) != 3
        or not all(isinstance(line, str) and line.strip() for line in summary)
    ):
        raise ArchiveBuildError(
            f"summary_ko in PR #{number} must contain exactly three non-empty strings"
        )
    return ReportSnapshot(number=number, summary_ko=tuple(summary))


def fetch_merged_pull_requests(
    repository: str,
) -> dict[int, PullRequestMetadata]:
    """Fetch all merged pull requests from GitHub's paginated Pulls API."""

    command = [
        "gh",
        "api",
        "--paginate",
        "--slurp",
        f"repos/{repository}/pulls?state=closed&per_page=100",
    ]
    try:
        raw = run(command)
        pages = json.loads(raw)
    except (subprocess.CalledProcessError, OSError, json.JSONDecodeError) as error:
        raise ArchiveBuildError(
            f"failed to fetch merged pull requests for {repository}: {error}"
        ) from error
    if not isinstance(pages, list) or not all(
        isinstance(page, list) for page in pages
    ):
        raise ArchiveBuildError("GitHub Pulls API response must be a list of pages")

    merged: dict[int, PullRequestMetadata] = {}
    try:
        for page in pages:
            for item in page:
                if not isinstance(item, dict):
                    raise TypeError("pull request item must be an object")
                if item.get("merged_at") is None:
                    continue
                number = item["number"]
                title = item["title"]
                author = item["user"]["login"]
                merged_at = item["merged_at"]
                if (
                    not isinstance(number, int)
                    or isinstance(number, bool)
                    or not isinstance(title, str)
                    or not title
                    or not isinstance(author, str)
                    or not author
                    or not isinstance(merged_at, str)
                    or not merged_at
                ):
                    raise TypeError("invalid merged pull request fields")
                merged[number] = PullRequestMetadata(
                    number=number,
                    title=title,
                    author=author,
                    merged_at=merged_at,
                )
    except (KeyError, TypeError) as error:
        raise ArchiveBuildError(
            f"invalid GitHub Pulls API response: {error}"
        ) from error
    return merged


def build_archive_entries(
    pages_root: Path,
    merged_prs: Mapping[int, PullRequestMetadata],
) -> list[ArchiveEntry]:
    """Build newest-first cards for report pages whose PRs were merged."""

    report_pages = discover_report_pages(pages_root)
    entries: list[ArchiveEntry] = []
    for number in sorted(report_pages.keys() & merged_prs.keys()):
        metadata = merged_prs[number]
        try:
            snapshot = extract_report_snapshot(report_pages[number])
        except ArchiveBuildError as error:
            raise ArchiveBuildError(
                f"cannot archive merged PR #{number}: {error}"
            ) from error
        entries.append(
            ArchiveEntry(
                number=number,
                title=metadata.title,
                author=metadata.author,
                merged_at=metadata.merged_at,
                summary_ko=snapshot.summary_ko,
                report_url=f"pr/{number}/",
            )
        )
    return sorted(
        entries,
        key=lambda entry: (entry.merged_at, entry.number),
        reverse=True,
    )


def serialize_archive(
    entries: Sequence[ArchiveEntry],
    generated_at: str,
) -> dict[str, object]:
    """Convert validated entries to the public archive JSON contract."""

    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "reports": [
            {
                **asdict(entry),
                "summary_ko": list(entry.summary_ko),
            }
            for entry in entries
        ],
    }


def render_archive(
    template_path: Path,
    payload: Mapping[str, object],
) -> str:
    """Inject archive JSON into the inert data block of the HTML template."""

    try:
        template = template_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ArchiveBuildError(
            f"cannot read archive template {template_path}: {error}"
        ) from error
    if ARCHIVE_PLACEHOLDER not in template:
        raise ArchiveBuildError(
            f"{ARCHIVE_PLACEHOLDER} not found in archive template {template_path}"
        )
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return template.replace(ARCHIVE_PLACEHOLDER, data, 1)


def write_archive(
    output_dir: Path,
    template_path: Path,
    javascript_path: Path,
    payload: Mapping[str, object],
) -> None:
    """Write a complete archive bundle after every source has been validated."""

    html = render_archive(template_path, payload)
    archive_json = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        javascript = javascript_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ArchiveBuildError(
            f"cannot read archive JavaScript {javascript_path}: {error}"
        ) from error

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "index.html").write_text(html, encoding="utf-8")
    (output_dir / "archive.json").write_text(archive_json, encoding="utf-8")
    (output_dir / "archive.js").write_text(javascript, encoding="utf-8")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the merged PR report archive for GitHub Pages."
    )
    parser.add_argument("--pages-root", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--javascript", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repository", required=True)
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    merged_prs: Mapping[int, PullRequestMetadata] | None = None,
) -> int:
    """Build a complete bundle and replace output files only after validation."""

    args = _parse_args(argv)
    try:
        authoritative_prs = (
            fetch_merged_pull_requests(args.repository)
            if merged_prs is None
            else merged_prs
        )
        entries = build_archive_entries(args.pages_root, authoritative_prs)
        generated_at = (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        payload = serialize_archive(entries, generated_at)

        output_dir: Path = args.output_dir
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{output_dir.name}-",
            dir=output_dir.parent,
        ) as temporary_directory:
            staged_dir = Path(temporary_directory)
            write_archive(staged_dir, args.template, args.javascript, payload)
            output_dir.mkdir(parents=True, exist_ok=True)
            for filename in ("index.html", "archive.json", "archive.js"):
                (staged_dir / filename).replace(output_dir / filename)
    except (ArchiveBuildError, OSError, ValueError) as error:
        print(f"[pr-report-archive] {error}", file=sys.stderr)
        return 1

    print(
        f"[pr-report-archive] generated {len(entries)} merged reports",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
