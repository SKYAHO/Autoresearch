"""AST 기반 모듈 정보 추출 — 모듈을 import 하지 않는다 (부작용 회피)."""
from __future__ import annotations

import ast

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
    fields = []
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if not stmt.target.id.startswith("_") and stmt.target.id != "model_config":
                fields.append(stmt.target.id)
    return fields


def _normalize_import(module: str) -> str | None:
    if module == INTERNAL_ROOT:
        return None
    if module.startswith(INTERNAL_ROOT + "."):
        return module.removeprefix(INTERNAL_ROOT + ".")
    return None


def extract_module_info(source: str, module_id: str, stage: str, path: str) -> dict:
    tree = ast.parse(source)
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
            "role": None, "owns": [], "not_owns": [],
            "public_symbols": public_symbols, "version_consts": version_consts,
            "schema_fields": schema_fields, "imports": sorted(imports)}
