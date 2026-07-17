"""argparse add_argument 호출에서 공개 CLI 인자 표면을 추출한다 (batch-contract)."""
from __future__ import annotations

import ast


def _is_required(node: ast.Call) -> bool:
    for kw in node.keywords:
        if kw.arg == "required" and isinstance(kw.value, ast.Constant) \
                and kw.value.value is True:
            return True
    return False


def extract_cli_args(source: str) -> list[dict]:
    """각 인자를 `{"flag": "--foo", "required": bool}`로 반환한다.

    `add_argument("-m", "--mode")`처럼 짧은 별칭이 먼저 오는 위치 인자 순서를
    대비해 args[0]만 보지 않고, 첫 번째 "--"로 시작하는 위치 인자를 공개 플래그로
    채택한다(짧은 별칭만 있고 긴 플래그가 없으면 공개 계약 표면이 아니므로 제외).
    """
    result: list[dict] = []
    index_by_flag: dict[str, int] = {}
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument" and node.args):
            continue
        flag = None
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                    and arg.value.startswith("--"):
                flag = arg.value
                break
        if flag is None:
            continue
        required = _is_required(node)
        if flag not in index_by_flag:
            index_by_flag[flag] = len(result)
            result.append({"flag": flag, "required": required})
        elif required:
            # 같은 플래그가 여러 add_argument 호출에 걸쳐 나타나면(중복 등록) 한
            # 번이라도 required=True였다면 필수로 취급한다 — 초록을 놓치는 쪽보다
            # 안전한 방향이다.
            result[index_by_flag[flag]]["required"] = True
    return result
