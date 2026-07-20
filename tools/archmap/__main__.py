"""archmap 추출기 CLI: build / delta / comment."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.archmap.build import build_architecture
from tools.archmap.comment import render_comment
from tools.archmap.delta import build_delta, parse_name_status, parse_numstat


@dataclass(frozen=True, slots=True)
class _SidecarInputError(ValueError):
    path: Path
    reason: str

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}"


def _read_sidecar_stale(path: Path) -> list[str]:
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise _SidecarInputError(path, f"invalid UTF-8: {error}") from None
    except OSError as error:
        raise _SidecarInputError(path, f"cannot read delta JSON: {error}") from None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise _SidecarInputError(path, f"invalid JSON: {error.msg}") from None

    if not isinstance(payload, dict):
        raise _SidecarInputError(path, "top-level JSON must be an object")
    if "sidecar_stale" not in payload:
        raise _SidecarInputError(path, "missing sidecar_stale")

    stale = payload["sidecar_stale"]
    if not isinstance(stale, list):
        raise _SidecarInputError(path, "sidecar_stale must be an array")
    if any(not isinstance(item, str) for item in stale):
        raise _SidecarInputError(path, "sidecar_stale entries must be strings")
    if any(item == "" for item in stale):
        raise _SidecarInputError(path, "sidecar_stale entries must be non-empty")
    if len(stale) != len(set(stale)):
        raise _SidecarInputError(path, "sidecar_stale entries must be unique")
    return stale


def _write(doc: dict[str, Any], out: str) -> None:
    text = json.dumps(doc, ensure_ascii=False, indent=2)
    if out == "-":
        sys.stdout.write(text)
    else:
        Path(out).write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tools.archmap")
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="architecture.json 생성")
    p_build.add_argument("--repo-root", default=".")
    p_build.add_argument("--repo", required=True)
    p_build.add_argument("--repo-url", default="")
    p_build.add_argument("--revision", required=True)
    p_build.add_argument("--out", default="-")

    p_delta = sub.add_parser("delta", help="pr-delta.json 생성")
    p_delta.add_argument("--base", required=True, help="base architecture.json 경로")
    p_delta.add_argument("--head", required=True, help="head architecture.json 경로")
    p_delta.add_argument("--numstat", required=True, help="git diff --numstat -z 출력 파일")
    p_delta.add_argument("--name-status", required=True,
                         help="git diff --name-status -z 출력 파일")
    p_delta.add_argument("--pr", type=int, required=True)
    p_delta.add_argument("--issue-json", default=None, help="이슈 정보 JSON 파일(선택)")
    p_delta.add_argument("--out", default="-")

    p_comment = sub.add_parser("comment", help="PR 코멘트 마크다운 생성")
    p_comment.add_argument("--delta", required=True)
    p_comment.add_argument("--report-url", default=None)
    p_comment.add_argument("--out", default="-")

    p_check = sub.add_parser("check-sidecar", help="sidecar_stale 게이트 검사")
    p_check.add_argument("--delta", required=True)

    args = parser.parse_args(argv)
    if args.command == "build":
        _write(build_architecture(Path(args.repo_root), args.repo,
                                  args.revision, args.repo_url), args.out)
    elif args.command == "delta":
        base = json.loads(Path(args.base).read_text(encoding="utf-8"))
        head = json.loads(Path(args.head).read_text(encoding="utf-8"))
        changed = parse_numstat(Path(args.numstat).read_bytes())
        deleted = parse_name_status(Path(args.name_status).read_bytes())
        issue = None
        if args.issue_json:
            issue = json.loads(Path(args.issue_json).read_text(encoding="utf-8"))
        _write(build_delta(base, head, changed, args.pr, issue, deleted_paths=deleted), args.out)
    elif args.command == "comment":
        delta = json.loads(Path(args.delta).read_text(encoding="utf-8"))
        text = render_comment(delta, args.report_url)
        if args.out == "-":
            sys.stdout.write(text)
        else:
            Path(args.out).write_text(text, encoding="utf-8")
    elif args.command == "check-sidecar":
        try:
            stale = _read_sidecar_stale(Path(args.delta))
        except _SidecarInputError as error:
            parser.exit(2, f"check-sidecar: {error}\n")
        if stale:
            sys.stderr.write("\n".join(sorted(stale)) + "\n")
            raise SystemExit(1)


if __name__ == "__main__":
    main()
