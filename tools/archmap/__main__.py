"""archmap 추출기 CLI: build / delta / comment."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools.archmap.build import build_architecture


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

    args = parser.parse_args(argv)
    if args.command == "build":
        _write(build_architecture(Path(args.repo_root), args.repo,
                                  args.revision, args.repo_url), args.out)


if __name__ == "__main__":
    main()
