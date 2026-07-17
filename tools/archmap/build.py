"""소스 트리를 걸어 architecture.json 문서를 조립한다."""
from __future__ import annotations

import ast
from pathlib import Path

from tools.archmap.cli_contract import extract_cli_args
from tools.archmap.module_info import extract_module_info

SCHEMA_VERSION = "archmap-v0"
STAGES = ["youtube_collection", "virtual_users", "action_logs", "orchestration", "training"]
STAGE_BY_SUBPACKAGE = {
    "youtube_collection": "youtube_collection",
    "virtual_users": "virtual_users",
    "action_logs": "action_logs",
    "jobs": "orchestration",
}
CONSUMED_BY = ["Autoresearch-airflow"]


def _module_id(rel: Path) -> str:
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if parts and parts[0] == "autoresearch":
        parts = parts[1:]
    return ".".join(parts)


def _stage_for(rel: Path) -> str | None:
    if rel.parts[0] == "autoresearch" and len(rel.parts) > 1:
        return STAGE_BY_SUBPACKAGE.get(rel.parts[1])
    if rel.parts[0] == "src":
        return "training"
    return None


def _iter_py_files(repo_root: Path):
    for base in ("autoresearch", "src"):
        root = repo_root / base
        if root.is_dir():
            yield from sorted(root.rglob("*.py"))


def _batch_contract(repo_root: Path) -> list[dict]:
    jobs_init = repo_root / "autoresearch" / "jobs" / "__init__.py"
    if not jobs_init.exists():
        return []
    name = None
    for node in ast.parse(jobs_init.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id == "BATCH_CONTRACT_VERSION" \
                and isinstance(node.value, ast.Constant):
            name = node.value.value
    if name is None:
        return []
    cli_args: list[str] = []
    required_flags: set[str] = set()
    for path in sorted((repo_root / "autoresearch" / "jobs").glob("*.py")):
        if path.name.startswith("_"):
            continue
        for arg in extract_cli_args(path.read_text(encoding="utf-8")):
            flag = arg["flag"]
            if flag not in cli_args:
                cli_args.append(flag)
            if arg["required"]:
                required_flags.add(flag)
    # required_args는 cli_args의 부분집합 문자열 배열이다 — 서버 스키마는
    # contracts[]에 대해 additionalProperties를 막지 않으므로(breaking_signatures
    # 선례) cli_args 타입(array of strings)을 바꾸지 않고 새 필드로만 추가한다.
    required_args = [f for f in cli_args if f in required_flags]
    return [{"name": name, "module": "jobs", "cli_args": cli_args,
             "required_args": required_args, "consumed_by": CONSUMED_BY}]


def build_architecture(repo_root: Path, repo: str, revision: str, repo_url: str) -> dict:
    repo_root = Path(repo_root)
    modules = []
    for path in _iter_py_files(repo_root):
        rel = path.relative_to(repo_root)
        stage = _stage_for(rel)
        if stage is None:
            continue
        info = extract_module_info(path.read_text(encoding="utf-8"),
                                   _module_id(rel), stage, str(rel).replace("\\", "/"))
        if rel.name == "__init__.py" and not info["public_symbols"] \
                and not info["version_consts"]:
            continue
        modules.append(info)
    return {"schema_version": SCHEMA_VERSION, "repo": repo, "repo_url": repo_url,
            "revision": revision, "contract_version": "batch-contract-v1",
            "stages": STAGES, "modules": modules,
            "contracts": _batch_contract(repo_root)}
