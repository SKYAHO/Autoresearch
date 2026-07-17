"""archmap 추출기 CLI: build / delta / comment."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools.archmap.build import build_architecture
from tools.archmap.delta import build_delta, parse_numstat


def _write(doc: dict, out: str) -> None:
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
    p_delta.add_argument("--numstat", required=True, help="git diff --numstat 출력 파일")
    p_delta.add_argument("--pr", type=int, required=True)
    p_delta.add_argument("--issue-json", default=None, help="이슈 정보 JSON 파일(선택)")
    p_delta.add_argument("--out", default="-")

    args = parser.parse_args(argv)
    if args.command == "build":
        _write(build_architecture(Path(args.repo_root), args.repo,
                                  args.revision, args.repo_url), args.out)
    elif args.command == "delta":
        base = json.loads(Path(args.base).read_text(encoding="utf-8"))
        head = json.loads(Path(args.head).read_text(encoding="utf-8"))
        changed = parse_numstat(Path(args.numstat).read_text(encoding="utf-8"))
        issue = None
        if args.issue_json:
            issue = json.loads(Path(args.issue_json).read_text(encoding="utf-8"))
        _write(build_delta(base, head, changed, args.pr, issue), args.out)


if __name__ == "__main__":
    main()
