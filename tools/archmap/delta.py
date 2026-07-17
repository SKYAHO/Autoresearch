"""base/head architecture.json 비교 + git 사실로 pr-delta.json을 만든다."""
from __future__ import annotations

SCHEMA_VERSION = "archmap-v0"


def parse_numstat(text: str) -> dict[str, int]:
    changed: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, _, path = parts
        changed[path] = int(added) if added.isdigit() else 0
    return changed


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


def _symbols_changed(base_m: dict, head_m: dict) -> tuple[list[dict], list[dict]]:
    base_syms = {s["name"]: s for s in base_m["public_symbols"]}
    head_syms = {s["name"]: s for s in head_m["public_symbols"]}
    changes, breaking = [], []
    for name, s in head_syms.items():
        if name not in base_syms:
            changes.append({"name": name, "change": "added", "line": s["line"]})
        elif s.get("sig") != base_syms[name].get("sig"):
            changes.append({"name": name, "change": "signature", "line": s["line"]})
            if not _sig_backward_compatible(base_syms[name].get("sig"), s.get("sig")):
                breaking.append({"module": head_m["id"], "name": name})
    for name, s in base_syms.items():
        if name not in head_syms:
            changes.append({"name": name, "change": "removed", "line": None})
            breaking.append({"module": base_m["id"], "name": name})
    return changes, breaking


def build_delta(base: dict, head: dict, changed: dict[str, int],
                pr: int, issue: dict | None) -> dict:
    base_mods = {m["id"]: m for m in base["modules"]}
    head_mods = {m["id"]: m for m in head["modules"]}

    changed_modules, version_changes, unchanged_contracts = [], [], []
    schema_changes, breaking_signatures = [], []

    for mid, hm in head_mods.items():
        bm = base_mods.get(mid)
        if hm["path"] not in changed:
            continue
        if bm is None:
            changed_modules.append({"id": mid, "path": hm["path"], "stage": hm["stage"],
                                    "symbols_changed": [{"name": s["name"], "change": "added",
                                                          "line": s["line"]}
                                                         for s in hm["public_symbols"]],
                                    "public_surface_changed": bool(hm["public_symbols"])})
            continue
        symbols, breaking = _symbols_changed(bm, hm)
        breaking_signatures.extend(breaking)
        changed_modules.append({"id": mid, "path": hm["path"], "stage": hm["stage"],
                                "symbols_changed": symbols,
                                "public_surface_changed": bool(symbols)})
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
            else:
                unchanged_contracts.append({"const": const, "module": mid,
                                            "value": info["value"], "line": info["line"]})
        for const, old in bm["version_consts"].items():
            if const not in hm["version_consts"]:
                version_changes.append({"const": const, "module": mid, "from": old["value"],
                                        "to": None, "line": None, "breaking": True})
        for model in set(bm["schema_fields"]) | set(hm["schema_fields"]):
            old_f = set(bm["schema_fields"].get(model, []))
            new_f = set(hm["schema_fields"].get(model, []))
            for f in sorted(new_f - old_f):
                schema_changes.append({"model": model, "module": mid, "field": f,
                                       "change": "added", "breaking": False})
            for f in sorted(old_f - new_f):
                schema_changes.append({"model": model, "module": mid, "field": f,
                                       "change": "removed", "breaking": True})

    for mid, bm in base_mods.items():
        if mid not in head_mods and bm["path"] in changed:
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
            cross_repo.append({"contract": c["name"], "impact": "contract-renamed",
                               "breaking": True,
                               "details": "base에 없는 계약 이름 — 이름 변경 여부를 확인하십시오"})
            continue
        added = [a for a in c["cli_args"] if a not in old["cli_args"]]
        removed = [a for a in old["cli_args"] if a not in c["cli_args"]]
        if added:
            cross_repo.append({"contract": c["name"], "impact": "optional-arg-added",
                               "breaking": False, "details": ", ".join(added) + " 인자 추가"})
        if removed:
            cross_repo.append({"contract": c["name"], "impact": "arg-removed",
                               "breaking": True, "details": ", ".join(removed) + " 인자 제거"})

    test_files = sorted(p for p in changed if p.startswith("tests/"))
    return {"schema_version": SCHEMA_VERSION, "repo": head["repo"], "pr": pr,
            "base_sha": base["revision"], "head_sha": head["revision"], "issue": issue,
            "changed_modules": changed_modules, "version_changes": version_changes,
            "unchanged_contracts": unchanged_contracts, "schema_changes": schema_changes,
            "cross_repo": cross_repo,
            "tests": {"files": test_files,
                      "lines_added": sum(changed[p] for p in test_files)},
            "sidecar_stale": [], "breaking_signatures": breaking_signatures}
