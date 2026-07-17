"""base/head architecture.json 비교 + git 사실로 pr-delta.json을 만든다."""
from __future__ import annotations

SCHEMA_VERSION = "archmap-v0"


def _collapse_slashes(path: str) -> str:
    while "//" in path:
        path = path.replace("//", "/")
    return path


def _split_rename_path(path: str) -> tuple[str, str] | None:
    """`git diff --numstat`의 rename 축약 표기를 (old_path, new_path)로 분리한다.

    git은 별도 설정 없이도 기본으로 rename을 압축해서 보여준다. 실제 저장소에서
    `git mv` 후 `git diff --cached --numstat`을 실행해 확인한 세 가지 형태:
      1) 공통 접두/접미가 전혀 없으면 중괄호 없이 "old/path.py => new/path.py"
      2) 공통 접두/접미가 있으면 "prefix{old => new}suffix" — prefix 또는 suffix가
         비어 있을 수 있다: "{action_logs => jobs}/schema.py",
         "autoresearch/action_logs/{schema.py => schema_new.py}"
      3) 중괄호 안쪽(old 또는 new) 자체가 빈 문자열일 수도 있다:
         "autoresearch/{ => sub}/schema.py" (디렉터리 계층이 새로 생김),
         "autoresearch/{sub => }/schema.py" (디렉터리 계층이 사라짐)
    rename이 아니면 None을 반환한다.
    """
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


def parse_numstat(text: str) -> dict[str, int]:
    """`git diff --numstat` 출력을 {경로: 추가줄수}로 파싱한다.

    build_delta는 base/head 모듈을 (경로가 아니라) build.py의 _module_id()가 경로에서
    파생시킨 id로 매칭한다. 파일을 rename하면 head id가 base와 달라져 base_mods.get(mid)가
    항상 None이 되고, 해당 모듈이 "완전히 새 모듈"로 처리되어 breaking 시그니처 변경이
    통째로 사라지는 결함이 있었다(직전 라운드에서 head 경로만 정규화했지만 id 매칭 자체는
    고치지 못해 실효가 없었음).
    그래서 rename 줄은 old 경로(추가줄수 0)와 new 경로(실제 추가줄수) 둘 다 changed에
    넣는다 — "삭제 + 추가"로 펼치면 build_delta의 기존 로직이 자연히 old 경로만 있는
    base 모듈을 삭제(모든 심볼 removed + breaking_signatures)로, new 경로만 있는 head
    모듈을 추가로 잡는다. 이것은 은폐가 아니라 진실이다: 모듈 경로가 바뀌면 그것을
    import하던 쪽에는 실제로 breaking 변경이므로, rename을 삭제+추가로 보고하는 것이
    정직한 판정이다.
    """
    changed: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, _, path = parts
        added_n = int(added) if added.isdigit() else 0
        split = _split_rename_path(path)
        if split is not None:
            old_path, new_path = split
            changed[old_path] = 0
            changed[new_path] = added_n
        else:
            changed[path] = added_n
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
        # required_args는 추출기가 새로 채우기 시작한 필드라 옛 architecture.json
        # 픽스처에는 없을 수 있다 — .get()으로 기본값 []를 둔다.
        new_required = set(c.get("required_args", []))
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
            "sidecar_stale": [], "breaking_signatures": breaking_signatures}
