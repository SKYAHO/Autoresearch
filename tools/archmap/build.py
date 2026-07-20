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
    """jobs/*.py 각 파일을 **독립된 CLI 계약**으로 추출한다.

    jobs/*.py(action_log.py, action_log_quality.py, youtube_backfill.py,
    youtube_trending.py)는 airflow가 개별 호출하는 서로 다른 CLI 4개다. 예전에는
    이 넷의 CLI 인자를 합집합으로 묶어 계약 하나(`batch-contract-v1`)로 냈는데,
    그러면 한 job에서 플래그를 뒤집거나 지워도 다른 job에 같은 플래그가 남아
    있는 한 합집합 자체는 안 바뀌어 delta.py가 그 변경을 완전히 놓쳤다(실측:
    --overwrite/--virtual-users-path/--youtube-base-path가 job마다 optional/
    required가 갈리고, 5개 플래그가 2개 이상 job에 걸쳐 있어 삭제해도 합집합에
    남는다). 그래서 job 파일 하나당 계약 하나를 만든다 — 실제 호출 구조와
    일치하고, 각 CLI의 표면 변경이 다른 job에 가려지지 않는다.

    name 충돌 방지: BATCH_CONTRACT_VERSION(`batch-contract-v1`)은 모든 job이
    공유하는 계약 "버전" 문자열이라, 계약 이름을 그대로 쓰면 job 4개가 전부
    같은 name을 갖게 되어 delta.py의 `{c["name"]: c for c in base["contracts"]}`
    매칭이 서로를 덮어써 3개 job의 계약이 통째로 사라진다. name을
    `f"{version}:{job_id}"`(예: `batch-contract-v1:jobs.action_log`)로 job별
    유일하게 만들어 이 충돌을 없앤다 — delta.py의 매칭 로직 자체는 그대로 두고
    입력을 정직하게 만드는 쪽이, 매칭 키를 (name, module) 튜플로 바꿔 두 필드가
    항상 함께 다뤄져야 한다는 암묵적 불변식을 여기저기 심는 것보다 변경 범위가
    작다.
    """
    jobs_init = repo_root / "autoresearch" / "jobs" / "__init__.py"
    if not jobs_init.exists():
        return []
    version = None
    for node in ast.parse(jobs_init.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id == "BATCH_CONTRACT_VERSION" \
                and isinstance(node.value, ast.Constant):
            version = node.value.value
    if version is None:
        return []
    contracts: list[dict] = []
    for path in sorted((repo_root / "autoresearch" / "jobs").glob("*.py")):
        if path.name.startswith("_"):
            continue
        job_id = f"jobs.{path.stem}"
        cli_args: list[str] = []
        required_flags: set[str] = set()
        for arg in extract_cli_args(path.read_text(encoding="utf-8")):
            flag = arg["flag"]
            if flag not in cli_args:
                cli_args.append(flag)
            if arg["required"]:
                required_flags.add(flag)
        # required_args는 cli_args의 부분집합 문자열 배열이다 — 서버 스키마는
        # contracts[]에 대해 additionalProperties를 막지 않으므로
        # (breaking_signatures 선례) cli_args 타입(array of strings)을 바꾸지
        # 않고 새 필드로만 추가한다.
        required_args = [f for f in cli_args if f in required_flags]
        contracts.append({"name": f"{version}:{job_id}", "module": job_id,
                          "cli_args": cli_args, "required_args": required_args,
                          "consumed_by": CONSUMED_BY})
    return contracts


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
