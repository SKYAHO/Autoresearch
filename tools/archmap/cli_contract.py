"""argparse add_argument 호출에서 공개 CLI 인자 표면을 추출한다 (batch-contract)."""
from __future__ import annotations

import ast


def extract_cli_args(source: str) -> list[str]:
    flags: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument" and node.args):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str) \
                and first.value.startswith("--") and first.value not in flags:
            flags.append(first.value)
    return flags
