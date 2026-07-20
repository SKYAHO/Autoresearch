"""AST 기반 모듈 정보 추출 — 모듈을 import 하지 않는다 (부작용 회피)."""
from __future__ import annotations

import ast

from tools.archmap.sidecar import extract_arch_sidecar

VERSION_CONST_ALLOWLIST = {"TARGET_COUNTRY"}
INTERNAL_ROOT = "autoresearch"


def _is_version_const(name: str) -> bool:
    return name.isupper() and (name.endswith("_VERSION") or name in VERSION_CONST_ALLOWLIST)


def _format_sig(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    a = fn.args
    parts: list[str] = []
    pos = list(a.posonlyargs) + list(a.args)
    defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    for arg, default in zip(pos, defaults):
        parts.append(arg.arg if default is None else f"{arg.arg}={ast.unparse(default)}")
    if a.vararg:
        parts.append(f"*{a.vararg.arg}")
    elif a.kwonlyargs:
        parts.append("*")
    for arg, default in zip(a.kwonlyargs, a.kw_defaults):
        parts.append(arg.arg if default is None else f"{arg.arg}={ast.unparse(default)}")
    if a.kwarg:
        parts.append(f"**{a.kwarg.arg}")
    return f"({', '.join(parts)})"


def _is_basemodel(node: ast.ClassDef) -> bool:
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id == "BaseModel":
            return True
        if isinstance(base, ast.Attribute) and base.attr == "BaseModel":
            return True
    return False


def _class_fields(node: ast.ClassDef) -> list[str]:
    """필드를 "이름: 타입[ = 우변]" 문자열로 반환한다.

    FG-2는 타입 변경 침묵을 막으려고 어노테이션을 이름에 붙였지만, 우변
    (`stmt.value`)은 여전히 버렸다. `Field(ge=0.0, le=1.0)` -> `Field(ge=0.5,
    le=1.0)`처럼 타입은 그대로고 제약(우변)만 바뀌는 흔한 pydantic 관용구는
    어노테이션 문자열이 변경 전후 동일해 delta.py의 집합 비교에서 완전히
    침묵한다. 마찬가지로 `rank: int | None = None`(선택, 기본값 있음) ->
    `rank: int | None`(우변 제거로 필수화)도 우변을 버리면 구분이 안 된다.
    우변이 있으면 `ast.unparse(stmt.value)`까지 문자열에 포함시켜, 제약/기본값
    변경이 이름이 같아도 다른 문자열이 되어 자동으로 removed(옛 값, breaking)+
    added(새 값, non-breaking) 쌍으로 나타나게 한다 — schema_fields가 여전히
    array of strings이므로 서버 스키마 변경도 필요 없다.
    우변이 없는 필드(`event_id: str`)는 " = ..." 을 붙이지 않고 기존 형식을
    그대로 유지한다 — 없는 기본값을 지어내지 않는다.
    ast.AnnAssign은 문법상 annotation이 항상 있으므로(`x: int`처럼 콜론 뒤가
    비는 문법은 없다) stmt.annotation이 None인 경우는 실질적으로 발생하지
    않지만, 방어적으로 이름만 남기는 분기를 유지한다.
    """
    fields = []
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            name = stmt.target.id
            if not name.startswith("_") and name != "model_config":
                if stmt.annotation is not None:
                    text = f"{name}: {ast.unparse(stmt.annotation)}"
                    if stmt.value is not None:
                        text += f" = {ast.unparse(stmt.value)}"
                    fields.append(text)
                else:
                    fields.append(name)
    return fields


def _normalize_import(module: str) -> str | None:
    if module == INTERNAL_ROOT:
        return None
    if module.startswith(INTERNAL_ROOT + "."):
        return module.removeprefix(INTERNAL_ROOT + ".")
    return None


def extract_module_info(source: str, module_id: str, stage: str, path: str) -> dict:
    tree = ast.parse(source)
    sidecar = extract_arch_sidecar(tree, path, stage)
    public_symbols: list[dict] = []
    version_consts: dict[str, dict] = {}
    schema_fields: dict[str, list[str]] = {}
    imports: set[str] = set()

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                public_symbols.append({"name": node.name, "kind": "function",
                                       "sig": _format_sig(node), "line": node.lineno})
        elif isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                public_symbols.append({"name": node.name, "kind": "class",
                                       "sig": None, "line": node.lineno})
            if _is_basemodel(node):
                schema_fields[node.name] = _class_fields(node)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if not name.startswith("_"):
                public_symbols.append({"name": name, "kind": "const",
                                       "sig": None, "line": node.lineno})
            if _is_version_const(name) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str):
                version_consts[name] = {"value": node.value.value, "line": node.lineno}
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if (norm := _normalize_import(alias.name)) is not None:
                    imports.add(norm)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if (norm := _normalize_import(node.module)) is not None:
                imports.add(norm)

    return {"id": module_id, "stage": stage, "path": path,
            "role": sidecar["role"] if sidecar is not None else None,
            "owns": sidecar["owns"] if sidecar is not None else [],
            "not_owns": sidecar["not_owns"] if sidecar is not None else [],
            "public_symbols": public_symbols, "version_consts": version_consts,
            "schema_fields": schema_fields, "imports": sorted(imports)}
