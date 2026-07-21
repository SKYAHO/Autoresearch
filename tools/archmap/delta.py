"""base/head architecture.json 비교 + git 사실로 pr-delta.json을 만든다."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SCHEMA_VERSION = "archmap-v0"


def _collapse_slashes(path: str) -> str:
    while "//" in path:
        path = path.replace("//", "/")
    return path


def _split_rename_path(path: str) -> tuple[str, str] | None:
    """Legacy numstat의 brace/no-brace rename 축약을 두 경로로 펼친다."""
    brace_start = path.find("{")
    if brace_start != -1:
        brace_end = path.find("}", brace_start)
        if brace_end != -1:
            inner = path[brace_start + 1:brace_end]
            if " => " in inner:
                prefix = path[:brace_start]
                suffix = path[brace_end + 1:]
                old_mid, new_mid = inner.split(" => ", 1)
                # old_mid/new_mid가 빈 문자열이면 prefix/suffix 경계의 슬래시가 겹칠
                # 수 있다(예: "autoresearch/" + "" + "/schema.py" -> "autoresearch//schema.py").
                old_path = _collapse_slashes(prefix + old_mid + suffix)
                new_path = _collapse_slashes(prefix + new_mid + suffix)
                return old_path, new_path
    if " => " in path:
        old_path, new_path = path.split(" => ", 1)
        return old_path, new_path
    return None


def parse_numstat(output: str | bytes) -> dict[str, int]:
    """일반 텍스트 또는 path-safe `git diff --numstat -z`를 파싱한다.

    Rename/copy는 old 경로를 추가 0, new 경로를 실제 추가 수로 펼친다.
    """
    text = output.decode("utf-8") if isinstance(output, bytes) else output
    changed: dict[str, int] = {}
    nul_delimited = "\0" in text
    records = iter(text.split("\0") if nul_delimited else text.splitlines())
    for record in records:
        parts = record.split("\t", maxsplit=2)
        if len(parts) != 3:
            continue
        added, _, path = parts
        added_n = int(added) if added.isdigit() else 0
        if nul_delimited and not path:
            old_path, new_path = next(records, ""), next(records, "")
            if old_path:
                changed[old_path] = 0
            if new_path:
                changed[new_path] = added_n
            continue
        split = None if nul_delimited else _split_rename_path(path)
        if split is not None:
            old_path, new_path = split
            changed[old_path] = 0
            changed[new_path] = added_n
        else:
            changed[path] = added_n
    return changed


def parse_name_status(output: str | bytes) -> set[str]:
    """일반 텍스트 또는 path-safe name-status에서 exact D만 추출한다."""
    text = output.decode("utf-8") if isinstance(output, bytes) else output
    deleted: set[str] = set()
    if "\0" in text:
        fields = iter(text.split("\0"))
        for status in fields:
            if not status:
                continue
            path = next(fields, "")
            if status == "D" and path:
                deleted.add(path)
            if status.startswith(("R", "C")):
                _ = next(fields, "")
        return deleted
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) == 2 and parts[0] == "D" and parts[1]:
            deleted.add(parts[1])
    return deleted


def _version_consts_changed(
    base_m: Mapping[str, Any], head_m: Mapping[str, Any]
) -> bool:
    base_values = {name: info["value"] for name, info in base_m["version_consts"].items()}
    head_values = {name: info["value"] for name, info in head_m["version_consts"].items()}
    return base_values != head_values


def _sidecar_meaning(
    module: Mapping[str, Any],
) -> tuple[str | None, str | None, frozenset[str], frozenset[str]]:
    return (
        module.get("stage"),
        module.get("role"),
        frozenset(module.get("owns", [])),
        frozenset(module.get("not_owns", [])),
    )


def _sig_params(sig: str | None) -> list[str]:
    if not sig:
        return []
    inner = sig.strip()[1:-1].strip()
    return [p.strip() for p in inner.split(",")] if inner else []


def _sig_backward_compatible(old: str | None, new: str | None) -> bool:
    """기존 파라미터 열 보존 + 추가분은 전부 기본값/가변 인자면 하위호환.

    쉼표 단순 분할이라 기본값 안에 쉼표가 있으면(예: default=(1, 2)) 보수적으로
    파괴로 판정될 수 있다 — Phase 0 한계로 허용(허위 초록보다 허위 경고가 낫다).
    """
    old_p, new_p = _sig_params(old), _sig_params(new)
    if new_p[:len(old_p)] != old_p:
        return False
    return all("=" in p or p.startswith("*") for p in new_p[len(old_p):])


def _symbols_changed(
    base_m: Mapping[str, Any], head_m: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    base_syms = {s["name"]: s for s in base_m["public_symbols"]}
    head_syms = {s["name"]: s for s in head_m["public_symbols"]}
    changes, breaking = [], []
    for name, s in head_syms.items():
        if name not in base_syms:
            changes.append({"name": name, "change": "added", "line": s["line"]})
            continue
        b = base_syms[name]
        if s.get("kind") != b.get("kind"):
            # kind 자체가 바뀌면(class<->const, function<->const 등) sig 비교는
            # 의미가 없다 — 종류 변경은 무조건 파괴적 API 변경으로 취급한다.
            changes.append({"name": name, "change": "signature", "line": s["line"]})
            breaking.append({"module": head_m["id"], "name": name})
        elif s.get("sig") != b.get("sig"):
            changes.append({"name": name, "change": "signature", "line": s["line"]})
            if not _sig_backward_compatible(b.get("sig"), s.get("sig")):
                breaking.append({"module": head_m["id"], "name": name})
    for name, s in base_syms.items():
        if name not in head_syms:
            changes.append({"name": name, "change": "removed", "line": None})
            breaking.append({"module": base_m["id"], "name": name})
    return changes, breaking


def build_delta(
    base: Mapping[str, Any],
    head: Mapping[str, Any],
    changed: dict[str, int],
    pr: int,
    issue: Mapping[str, Any] | None,
    deleted_paths: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    base_mods = {m["id"]: m for m in base["modules"]}
    head_mods = {m["id"]: m for m in head["modules"]}
    actual_deletions = frozenset() if deleted_paths is None else frozenset(deleted_paths)

    changed_modules, version_changes, unchanged_contracts = [], [], []
    schema_changes, breaking_signatures = [], []
    sidecar_stale: set[str] = set()

    for mid, hm in head_mods.items():
        bm = base_mods.get(mid)
        if hm["path"] not in changed:
            continue
        if bm is None:
            if (hm["public_symbols"] or hm["version_consts"]) \
                    and hm["path"] not in actual_deletions \
                    and hm.get("role") is None:
                sidecar_stale.add(hm["path"])
            changed_modules.append({"id": mid, "path": hm["path"], "stage": hm["stage"],
                                    "symbols_changed": [{"name": s["name"], "change": "added",
                                                          "line": s["line"]}
                                                         for s in hm["public_symbols"]],
                                    "public_surface_changed": bool(hm["public_symbols"])})
            continue
        symbols, breaking = _symbols_changed(bm, hm)
        breaking_signatures.extend(breaking)
        if (symbols or _version_consts_changed(bm, hm)) \
                and hm["path"] not in actual_deletions \
                and (hm.get("role") is None
                     or _sidecar_meaning(bm) == _sidecar_meaning(hm)):
            sidecar_stale.add(hm["path"])
        changed_modules.append({"id": mid, "path": hm["path"], "stage": hm["stage"],
                                "symbols_changed": symbols,
                                "public_surface_changed": bool(symbols)})
        # 스키마 필드 변경을 먼저 계산한다 — 아래 버전 상수 "불변" 판정(스펙 §7 두
        # 번째 조건)이 이 모듈에 스키마 변경이 있었는지를 알아야 하기 때문이다.
        module_schema_changes: list[dict[str, Any]] = []
        for model in set(bm["schema_fields"]) | set(hm["schema_fields"]):
            old_f = set(bm["schema_fields"].get(model, []))
            new_f = set(hm["schema_fields"].get(model, []))
            for f in sorted(new_f - old_f):
                module_schema_changes.append({"model": model, "module": mid, "field": f,
                                              "change": "added", "breaking": False})
            for f in sorted(old_f - new_f):
                module_schema_changes.append({"model": model, "module": mid, "field": f,
                                              "change": "removed", "breaking": True})
        schema_changes.extend(module_schema_changes)

        for const, info in hm["version_consts"].items():
            old = bm["version_consts"].get(const)
            if old is None:
                version_changes.append({"const": const, "module": mid, "from": None,
                                        "to": info["value"], "line": info["line"],
                                        "breaking": False})
            elif old["value"] != info["value"]:
                version_changes.append({"const": const, "module": mid,
                                        "from": old["value"], "to": info["value"],
                                        "line": info["line"], "breaking": False})
            elif not module_schema_changes:
                # 스펙 §7: 버전 상수 값이 그대로여도 같은 모듈에 schema_changes가
                # 하나라도 있으면 "계약 불변"을 주장하지 않는다. 어떤 필드가 어떤
                # 버전 상수와 "관련"인지 추출기가 판별할 방법이 없으므로, 보수적으로
                # 모듈 단위 전체를 묶어 판정한다 — "애매하면 breaking/warning 쪽"
                # 원칙에 부합한다. 이 값은 실제로 안 바뀌었으므로 version_changes에도
                # 넣지 않는다: 아무것도 주장하지 않는 것이 거짓 주장보다 낫다.
                unchanged_contracts.append({"const": const, "module": mid,
                                            "value": info["value"], "line": info["line"]})
        for const, old in bm["version_consts"].items():
            if const not in hm["version_consts"]:
                version_changes.append({"const": const, "module": mid, "from": old["value"],
                                        "to": None, "line": None, "breaking": True})

    for mid, bm in base_mods.items():
        if mid not in head_mods and bm["path"] in changed:
            if (bm["public_symbols"] or bm["version_consts"]) \
                    and bm["path"] not in actual_deletions:
                sidecar_stale.add(bm["path"])
            changed_modules.append({"id": mid, "path": bm["path"], "stage": bm["stage"],
                                    "symbols_changed": [{"name": s["name"], "change": "removed",
                                                          "line": None}
                                                         for s in bm["public_symbols"]],
                                    "public_surface_changed": bool(bm["public_symbols"])})
            breaking_signatures.extend({"module": mid, "name": s["name"]}
                                       for s in bm["public_symbols"])

    cross_repo = []
    base_contracts = {c["name"]: c for c in base["contracts"]}
    for c in head["contracts"]:
        old = base_contracts.get(c["name"])
        if old is None:
            cross_repo.append({"contract": c["name"], "impact": "contract-added",
                               "breaking": False, "details": "새 계약이 추가됨"})
            continue
        added = [a for a in c["cli_args"] if a not in old["cli_args"]]
        removed = [a for a in old["cli_args"] if a not in c["cli_args"]]
        # required_args는 추출기가 새로 채우기 시작한 필드라 옛 architecture.json
        # 픽스처에는 없을 수 있다 — .get()으로 기본값 []를 둔다.
        new_required = set(c.get("required_args", []))
        old_required = set(old.get("required_args", []))
        required_added = [a for a in added if a in new_required]
        optional_added = [a for a in added if a not in new_required]
        if required_added:
            cross_repo.append({"contract": c["name"], "impact": "required-arg-added",
                               "breaking": True,
                               "details": ", ".join(required_added) + " 필수 인자 추가"})
        if optional_added:
            cross_repo.append({"contract": c["name"], "impact": "optional-arg-added",
                               "breaking": False,
                               "details": ", ".join(optional_added) + " 인자 추가"})
        if removed:
            cross_repo.append({"contract": c["name"], "impact": "arg-removed",
                               "breaking": True, "details": ", ".join(removed) + " 인자 제거"})
        # 기존 플래그(added/removed 어디에도 없는, base·head 양쪽에 있는 플래그)의
        # required 소속 대조 — required_added는 "새로 추가된" 플래그만 보므로,
        # 이미 있던 optional 플래그가 required로 뒤집혀도 added에도 removed에도
        # 나타나지 않아 완전히 사라진다(이전 수정 FG-1의 사각지대). base·head
        # 양쪽에 required_args가 이미 있으므로 스키마 변경 없이 대조 가능하다.
        common = [a for a in c["cli_args"] if a in old["cli_args"]]
        became_required = [a for a in common if a in new_required and a not in old_required]
        became_optional = [a for a in common if a not in new_required and a in old_required]
        if became_required:
            cross_repo.append({"contract": c["name"], "impact": "arg-became-required",
                               "breaking": True,
                               "details": ", ".join(became_required) + " 인자가 필수로 변경됨"})
        if became_optional:
            # 반대 방향(required -> optional)은 완화이므로 breaking이 아니다 —
            # 그래도 소비자가 알 수 있도록 사실은 기록한다(INFO 성격, VERIFIED 아님).
            cross_repo.append({"contract": c["name"], "impact": "arg-became-optional",
                               "breaking": False,
                               "details": ", ".join(became_optional) + " 인자가 선택으로 완화됨"})

    head_contract_names = {c["name"] for c in head["contracts"]}
    for name in base_contracts:
        if name not in head_contract_names:
            # base에 있던 계약이 head에서 통째로 사라짐 — 소비 레포(예:
            # Autoresearch-airflow) 입장에서는 arg-removed보다 더 파괴적인 이벤트다.
            cross_repo.append({"contract": name, "impact": "contract-removed",
                               "breaking": True,
                               "details": "계약이 head에서 완전히 삭제됨 — 소비 레포 확인 필요"})

    test_files = sorted(p for p in changed if p.startswith("tests/"))
    return {"schema_version": SCHEMA_VERSION, "repo": head["repo"], "pr": pr,
            "base_sha": base["revision"], "head_sha": head["revision"], "issue": issue,
            "changed_modules": changed_modules, "version_changes": version_changes,
            "unchanged_contracts": unchanged_contracts, "schema_changes": schema_changes,
            "cross_repo": cross_repo,
            "tests": {"files": test_files,
                      "lines_added": sum(changed[p] for p in test_files)},
            "sidecar_stale": sorted(sidecar_stale),
            "breaking_signatures": breaking_signatures}
