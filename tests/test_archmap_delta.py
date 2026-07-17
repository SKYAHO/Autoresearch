import copy
from pathlib import Path

from tools.archmap.build import build_architecture
from tools.archmap.delta import build_delta, parse_numstat
from tools.archmap.module_info import extract_module_info

BASE = {
    "schema_version": "archmap-v0", "repo": "Autoresearch", "repo_url": "",
    "revision": "base000", "contract_version": "batch-contract-v1",
    "stages": ["action_logs"],
    "modules": [{
        "id": "action_logs.schema", "stage": "action_logs",
        "path": "autoresearch/action_logs/schema.py",
        "role": None, "owns": [], "not_owns": [],
        "public_symbols": [
            {"name": "run_daily", "kind": "function", "sig": "(request, generator)", "line": 10},
            {"name": "EventLog", "kind": "class", "sig": None, "line": 30},
            {"name": "helper", "kind": "function", "sig": "()", "line": 40},
            {"name": "flag", "kind": "const", "sig": None, "line": 41},
        ],
        "version_consts": {
            "ACTION_LOG_SCHEMA_VERSION": {"value": "action_log_schema_v1", "line": 16},
            "PROMPT_VERSION": {"value": "action_log_ctr_v3", "line": 17},
        },
        "schema_fields": {"EventLog": ["event_id", "clicked"]},
        "imports": [],
    }],
    "contracts": [{"name": "batch-contract-v1", "module": "jobs",
                   "cli_args": ["--mode"], "required_args": [],
                   "consumed_by": ["Autoresearch-airflow"]}],
}


def _head():
    head = copy.deepcopy(BASE)
    head["revision"] = "head000"
    m = head["modules"][0]
    m["public_symbols"][0]["sig"] = "(request, generator, max_users=None)"
    m["version_consts"]["PROMPT_VERSION"]["value"] = "action_log_ctr_v4"
    m["schema_fields"]["EventLog"] = ["event_id", "clicked", "position"]
    head["contracts"][0]["cli_args"] = ["--mode", "--max-users"]
    return head


CHANGED = {"autoresearch/action_logs/schema.py": 12, "tests/test_action_logs_daily.py": 52}


def _delta(head=None):
    return build_delta(BASE, head or _head(), CHANGED, pr=120,
                       issue={"number": 118, "title": "t", "body_excerpt": "b"})


def test_parse_numstat():
    text = "12\t3\tautoresearch/action_logs/schema.py\n52\t0\ttests/test_action_logs_daily.py\n-\t-\tdata/blob.bin\n"
    assert parse_numstat(text) == {"autoresearch/action_logs/schema.py": 12,
                                   "tests/test_action_logs_daily.py": 52,
                                   "data/blob.bin": 0}


def test_changed_modules_and_compatible_signature():
    d = _delta()
    (m,) = d["changed_modules"]
    assert m["id"] == "action_logs.schema" and m["public_surface_changed"]
    assert {"name": "run_daily", "change": "signature", "line": 10} in m["symbols_changed"]


def test_version_change_nonbreaking():
    d = _delta()
    (v,) = d["version_changes"]
    assert v["const"] == "PROMPT_VERSION" and v["from"].endswith("v3") \
        and v["to"].endswith("v4") and v["breaking"] is False


# --- Critical A (최종 전체 리뷰): 필드 타입이 바뀌는 중에도 "계약 불변" 초록이 뜸 ---
# 스펙 §7 두 번째 조건: "X 계약/스키마 불변" 배지는 X의 버전 상수가
# unchanged_contracts에 있을 뿐 아니라, 해당 모듈 schema_changes에 X 관련 필드
# 변경이 "없어야" 부여할 수 있다. 어떤 필드가 어떤 버전 상수와 "관련"인지 추출기가
# 판별할 방법이 없으므로 보수적으로 모듈 단위로 묶는다: 모듈에 schema_changes가
# 하나라도 있으면 그 모듈의 버전 상수는 unchanged_contracts에 넣지 않는다.

def test_unchanged_contract_suppressed_when_module_has_schema_changes():
    # 기본 _head()는 PROMPT_VERSION 값 변경 + EventLog.position 필드 추가를 함께
    # 가진다 -> action_logs.schema 모듈에 schema_changes가 존재한다. 이 상태에서
    # ACTION_LOG_SCHEMA_VERSION 값 자체는 안 바뀌었지만, 같은 모듈에 스키마 변경이
    # 있으므로 "계약 불변"을 주장해서는 안 된다(허위 초록 금지).
    d = _delta()
    assert d["schema_changes"], "이 테스트의 전제: 모듈에 스키마 변경이 있어야 함"
    consts = {u["const"] for u in d["unchanged_contracts"]}
    assert "ACTION_LOG_SCHEMA_VERSION" not in consts
    # 값이 바뀌지 않았으므로 version_changes에도 들어가면 안 된다 — 아무것도
    # 주장하지 않는 것이 거짓을 주장하는 것보다 낫다.
    assert not any(v["const"] == "ACTION_LOG_SCHEMA_VERSION" for v in d["version_changes"])


def test_unchanged_contract_kept_when_module_has_no_schema_changes():
    # 정상 케이스 회귀 (PR #120 시나리오): 모듈에 schema_changes가 전혀 없으면
    # 버전 상수 불변 판정은 그대로 unchanged_contracts에 남아야 한다.
    head = _head()
    head["modules"][0]["schema_fields"] = copy.deepcopy(BASE["modules"][0]["schema_fields"])
    d = _delta(head)
    assert d["schema_changes"] == []
    consts = {u["const"] for u in d["unchanged_contracts"]}
    assert "ACTION_LOG_SCHEMA_VERSION" in consts


def test_schema_field_added_nonbreaking_removed_breaking():
    d = _delta()
    (s,) = d["schema_changes"]
    assert s == {"model": "EventLog", "module": "action_logs.schema",
                 "field": "position", "change": "added", "breaking": False}
    head = _head()
    head["modules"][0]["schema_fields"]["EventLog"] = ["event_id"]
    removed = [s for s in _delta(head)["schema_changes"] if s["change"] == "removed"]
    assert removed and all(s["breaking"] for s in removed)


def test_breaking_signature_when_required_param_added():
    head = _head()
    head["modules"][0]["public_symbols"][0]["sig"] = "(request, generator, must_have)"
    d = _delta(head)
    assert any(v["breaking"] for v in d["version_changes"]) is False  # 버전은 그대로 비파괴
    (m,) = d["changed_modules"]
    assert m["public_surface_changed"]
    # 파괴적 시그니처는 cross_repo가 아니라 배지 근거인 breaking_signatures로 남는다
    assert d["breaking_signatures"] == [{"module": "action_logs.schema", "name": "run_daily"}]


def test_cross_repo_optional_arg_added_stays_nonbreaking():
    # 선택 인자 추가는 기존 동작 그대로 optional-arg-added/breaking=False 여야 한다.
    d = _delta()
    (x,) = d["cross_repo"]
    assert x["contract"] == "batch-contract-v1" and x["impact"] == "optional-arg-added" \
        and x["breaking"] is False
    head = _head()
    head["contracts"][0]["cli_args"] = []
    removed = [x for x in _delta(head)["cross_repo"] if x["impact"] == "arg-removed"]
    assert removed and removed[0]["breaking"] is True


def test_cross_repo_required_arg_added_is_breaking():
    # FG-1: extract_cli_args가 required 여부를 보존한 뒤, cross_repo 판정도
    # required 인자 추가를 optional-arg-added가 아니라 breaking으로 봐야 한다.
    head = _head()
    head["contracts"][0]["cli_args"] = ["--mode", "--max-users"]
    head["contracts"][0]["required_args"] = ["--max-users"]
    d = _delta(head)
    required = [x for x in d["cross_repo"] if x["impact"] == "required-arg-added"]
    assert required and required[0]["breaking"] is True
    assert required[0]["contract"] == "batch-contract-v1"
    assert "--max-users" in required[0]["details"]
    # 같은 인자가 optional-arg-added로도 이중 보고되면 안 된다.
    assert not any(x["impact"] == "optional-arg-added" for x in d["cross_repo"])


def test_cross_repo_mixed_required_and_optional_args_added():
    # 같은 계약에 필수 인자와 선택 인자가 동시에 추가되면 둘 다 각자 판정으로 갈라진다.
    head = _head()
    head["contracts"][0]["cli_args"] = ["--mode", "--max-users", "--dry-run"]
    head["contracts"][0]["required_args"] = ["--max-users"]
    d = _delta(head)
    by_impact = {x["impact"]: x for x in d["cross_repo"]}
    assert by_impact["required-arg-added"]["breaking"] is True
    assert "--max-users" in by_impact["required-arg-added"]["details"]
    assert by_impact["optional-arg-added"]["breaking"] is False
    assert "--dry-run" in by_impact["optional-arg-added"]["details"]


def test_tests_section():
    d = _delta()
    assert d["tests"] == {"files": ["tests/test_action_logs_daily.py"], "lines_added": 52}
    assert d["sidecar_stale"] == []


# --- Critical 1: git 기본 rename 축약 표기 (실제 `git diff --numstat` 출력으로 확인) ---
# 아래 문자열은 임시 저장소에서 실제로 `git mv` + `git diff --cached --numstat`을
# 실행해 얻은 값이다 (-M 플래그 없이도 git 2.34는 기본으로 rename을 압축한다).
# rename 줄은 old 경로(추가줄수 0)와 new 경로(실제 추가줄수) 둘 다 changed에 들어간다
# (라운드 2 수정: id 매칭 실패로 breaking이 사라지는 결함을 "삭제 + 추가"로 펼쳐서 해결).

def test_parse_numstat_rename_cross_dir_no_common_affix():
    # 공통 접두/접미가 전혀 없으면 "old => new" 형태 (중괄호 없음)
    text = "3\t0\tautoresearch/action_logs/schema.py => other_dir/schema_new.py\n"
    assert parse_numstat(text) == {"autoresearch/action_logs/schema.py": 0,
                                   "other_dir/schema_new.py": 3}


def test_parse_numstat_rename_same_dir_braces():
    # 같은 디렉터리 내 rename: "dir/{old => new}" (접미 없음)
    text = "3\t0\tautoresearch/action_logs/{schema.py => schema_new.py}\n"
    assert parse_numstat(text) == {"autoresearch/action_logs/schema.py": 0,
                                   "autoresearch/action_logs/schema_new.py": 3}


def test_parse_numstat_rename_prefix_and_suffix_common():
    # 접두("autoresearch/")와 접미("/schema.py")가 모두 있는 디렉터리 rename
    text = "3\t0\tautoresearch/{action_logs => jobs}/schema.py\n"
    assert parse_numstat(text) == {"autoresearch/action_logs/schema.py": 0,
                                   "autoresearch/jobs/schema.py": 3}


def test_parse_numstat_rename_empty_old_inner():
    # 중괄호 안 old 쪽이 빈 문자열: 디렉터리 계층이 새로 생기는 경우
    text = "3\t0\tautoresearch/{ => sub}/schema.py\n"
    assert parse_numstat(text) == {"autoresearch/schema.py": 0,
                                   "autoresearch/sub/schema.py": 3}


def test_parse_numstat_rename_empty_new_inner():
    # 중괄호 안 new 쪽이 빈 문자열: 디렉터리 계층이 사라지는 경우 (이중 슬래시 방지 확인)
    text = "3\t0\tautoresearch/{sub => }/schema.py\n"
    assert parse_numstat(text) == {"autoresearch/sub/schema.py": 0,
                                   "autoresearch/schema.py": 3}


def test_parse_numstat_rename_top_level_no_brace_degenerate():
    # 공통 접두/접미가 없는 최상위 rename은 git이 중괄호 없이 "old => new"로 낸다
    text = "0\t0\tsub/schema.py => schema.py\n"
    assert parse_numstat(text) == {"sub/schema.py": 0, "schema.py": 0}


# --- Critical 2: kind 변경 탐지 (class<->const, function<->const) ---

def test_kind_change_class_to_const_both_sig_none():
    # class Foo -> Foo = "value" : 둘 다 sig=None이라 기존 코드는 흔적조차 못 남겼다
    head = _head()
    head["modules"][0]["public_symbols"][1] = {
        "name": "EventLog", "kind": "const", "sig": None, "line": 30}
    d = _delta(head)
    (m,) = d["changed_modules"]
    assert {"name": "EventLog", "change": "signature", "line": 30} in m["symbols_changed"]
    assert {"module": "action_logs.schema", "name": "EventLog"} in d["breaking_signatures"]


def test_kind_change_function_to_const_no_args_sig_backward_compatible_trap():
    # 무인자 function "()" -> const None : _sig_backward_compatible만 보면 비파괴로 오판
    head = _head()
    head["modules"][0]["public_symbols"][2] = {
        "name": "helper", "kind": "const", "sig": None, "line": 40}
    d = _delta(head)
    assert {"module": "action_logs.schema", "name": "helper"} in d["breaking_signatures"]


def test_kind_change_const_to_function_no_args():
    # const None -> 무인자 function "()" : 역방향도 동일하게 오판되던 경로
    head = _head()
    head["modules"][0]["public_symbols"][3] = {
        "name": "flag", "kind": "function", "sig": "()", "line": 41}
    d = _delta(head)
    assert {"module": "action_logs.schema", "name": "flag"} in d["breaking_signatures"]


def test_kind_unchanged_signature_change_still_uses_backward_compat_check():
    # kind가 같을 때는 기존 sig 하위호환 판정이 그대로 적용돼야 한다 (회귀 방지)
    d = _delta()  # run_daily: (request, generator) -> (request, generator, max_users=None)
    assert d["breaking_signatures"] == []


# --- Important 3: 계약 삭제가 cross_repo에 잡히는지 ---

def test_cross_repo_contract_removed_entirely():
    head = _head()
    head["contracts"] = []
    d = _delta(head)
    removed = [x for x in d["cross_repo"] if x["contract"] == "batch-contract-v1"]
    assert len(removed) == 1
    assert removed[0]["breaking"] is True
    assert removed[0]["impact"] == "contract-removed"
    assert removed[0]["details"]


# --- 미검증 분기: version_consts 신규 추가 / 완전 제거 ---

def test_version_const_added_from_none():
    head = _head()
    head["modules"][0]["version_consts"]["NEW_THING_VERSION"] = {"value": "v1", "line": 99}
    d = _delta(head)
    added = [v for v in d["version_changes"] if v["const"] == "NEW_THING_VERSION"]
    assert added == [{"const": "NEW_THING_VERSION", "module": "action_logs.schema",
                      "from": None, "to": "v1", "line": 99, "breaking": False}]


def test_version_const_removed_entirely_is_breaking():
    head = _head()
    del head["modules"][0]["version_consts"]["ACTION_LOG_SCHEMA_VERSION"]
    d = _delta(head)
    removed = [v for v in d["version_changes"] if v["const"] == "ACTION_LOG_SCHEMA_VERSION"]
    assert removed == [{"const": "ACTION_LOG_SCHEMA_VERSION", "module": "action_logs.schema",
                        "from": "action_log_schema_v1", "to": None, "line": None,
                        "breaking": True}]


# --- 미검증 분기: 모듈 전체 추가 / 삭제 ---

def test_module_added_entirely():
    head = _head()
    head["modules"].append({
        "id": "action_logs.new_module", "stage": "action_logs",
        "path": "autoresearch/action_logs/new_module.py",
        "role": None, "owns": [], "not_owns": [],
        "public_symbols": [{"name": "run_new", "kind": "function", "sig": "()", "line": 5}],
        "version_consts": {}, "schema_fields": {}, "imports": [],
    })
    changed = dict(CHANGED)
    changed["autoresearch/action_logs/new_module.py"] = 20
    d = build_delta(BASE, head, changed, pr=120, issue=None)
    added = [m for m in d["changed_modules"] if m["id"] == "action_logs.new_module"]
    assert len(added) == 1
    assert added[0]["symbols_changed"] == [{"name": "run_new", "change": "added", "line": 5}]
    assert added[0]["public_surface_changed"] is True


def test_module_removed_entirely():
    head = _head()
    head["modules"] = []
    changed = {"autoresearch/action_logs/schema.py": 0}
    d = build_delta(BASE, head, changed, pr=120, issue=None)
    removed = [m for m in d["changed_modules"] if m["id"] == "action_logs.schema"]
    assert len(removed) == 1
    assert removed[0]["symbols_changed"][0]["change"] == "removed"
    assert removed[0]["public_surface_changed"] is True
    assert {"module": "action_logs.schema", "name": "run_daily"} in d["breaking_signatures"]


# --- Critical (라운드 2): rename이 id 매칭 실패로 breaking 시그니처를 통째로 삼킴 ---
# build_delta는 모듈을 path가 아니라 build.py의 _module_id()가 만드는 id로 매칭한다.
# rename하면 head id가 base와 달라지므로, 수작업 dict(양쪽 id를 동일하게 유지)로는
# 이 결함이 절대 드러나지 않는다. 반드시 build_architecture로 실제 base/head를 만들어
# id가 실제로 달라지는 것을 확인한 뒤 build_delta에 넘겨야 한다.

def _write_module(root: Path, rel_path: str, source: str) -> None:
    p = root / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(source, encoding="utf-8")


def test_build_delta_end_to_end_rename_across_directory_is_breaking(tmp_path):
    # 시나리오 A: 디렉터리 이동(action_logs -> jobs) + 필수 인자 추가(breaking).
    # 실제 `git mv` 후 `git diff --numstat`이 내는 형태: "autoresearch/{action_logs => jobs}/schema.py"
    base_root, head_root = tmp_path / "base_repo", tmp_path / "head_repo"
    _write_module(base_root, "autoresearch/action_logs/schema.py",
                  "def run_daily(request, generator):\n    return None\n")
    _write_module(head_root, "autoresearch/jobs/schema.py",
                  "def run_daily(request, generator, must_have):\n    return None\n")

    base = build_architecture(base_root, "Autoresearch", "base_sha", "")
    head = build_architecture(head_root, "Autoresearch", "head_sha", "")
    (base_mod,), (head_mod,) = base["modules"], head["modules"]
    # id가 실제로 달라짐을 먼저 확인 — 이것이 결함의 근본 원인이다.
    assert base_mod["id"] == "action_logs.schema"
    assert head_mod["id"] == "jobs.schema"
    assert base_mod["id"] != head_mod["id"]

    changed = parse_numstat("1\t1\tautoresearch/{action_logs => jobs}/schema.py\n")
    d = build_delta(base, head, changed, pr=165, issue=None)

    assert d["breaking_signatures"] == [{"module": "action_logs.schema", "name": "run_daily"}]
    added = [m for m in d["changed_modules"] if m["id"] == "jobs.schema"]
    assert added and added[0]["symbols_changed"] == [
        {"name": "run_daily", "change": "added", "line": 1}]


def test_build_delta_end_to_end_rename_same_directory_is_breaking(tmp_path):
    # 시나리오 B: 같은 디렉터리 내 파일명 변경(schema.py -> schema_v2.py) + 필수 인자 추가.
    # 실제 numstat 형태: "autoresearch/action_logs/{schema.py => schema_v2.py}"
    base_root, head_root = tmp_path / "base_repo", tmp_path / "head_repo"
    _write_module(base_root, "autoresearch/action_logs/schema.py",
                  "def run_daily(request, generator):\n    return None\n")
    _write_module(head_root, "autoresearch/action_logs/schema_v2.py",
                  "def run_daily(request, generator, must_have):\n    return None\n")

    base = build_architecture(base_root, "Autoresearch", "base_sha", "")
    head = build_architecture(head_root, "Autoresearch", "head_sha", "")
    (base_mod,), (head_mod,) = base["modules"], head["modules"]
    assert base_mod["id"] == "action_logs.schema"
    assert head_mod["id"] == "action_logs.schema_v2"
    assert base_mod["id"] != head_mod["id"]

    changed = parse_numstat("1\t1\tautoresearch/action_logs/{schema.py => schema_v2.py}\n")
    d = build_delta(base, head, changed, pr=165, issue=None)

    assert d["breaking_signatures"] == [{"module": "action_logs.schema", "name": "run_daily"}]
    added = [m for m in d["changed_modules"] if m["id"] == "action_logs.schema_v2"]
    assert added and added[0]["symbols_changed"] == [
        {"name": "run_daily", "change": "added", "line": 1}]


# --- FG-1 (최종 전체 리뷰): required=True CLI 인자 추가가 "하위호환" 초록을 받음 ---
# 수작업 dict로는 extract_cli_args의 실제 파싱 경로를 거치지 않으므로, 실제
# jobs/__init__.py + jobs/*.py 소스를 build_architecture로 빌드해 cross_repo가
# required 인자 추가를 optional-arg-added가 아니라 required-arg-added(breaking)로
# 판정하는지 전 층위(추출→델타)로 확인한다.

def test_build_delta_end_to_end_required_cli_arg_added_is_breaking(tmp_path):
    base_root, head_root = tmp_path / "base_repo", tmp_path / "head_repo"
    _write_module(base_root, "autoresearch/jobs/__init__.py",
                  'BATCH_CONTRACT_VERSION = "batch-contract-v1"\n')
    _write_module(base_root, "autoresearch/jobs/action_log.py",
                  'import argparse\n\n'
                  'def _p():\n    p = argparse.ArgumentParser()\n'
                  '    p.add_argument("--mode", required=True)\n    return p\n')
    _write_module(head_root, "autoresearch/jobs/__init__.py",
                  'BATCH_CONTRACT_VERSION = "batch-contract-v1"\n')
    _write_module(head_root, "autoresearch/jobs/action_log.py",
                  'import argparse\n\n'
                  'def _p():\n    p = argparse.ArgumentParser()\n'
                  '    p.add_argument("--mode", required=True)\n'
                  '    p.add_argument("--must-have", required=True)\n    return p\n')

    base = build_architecture(base_root, "Autoresearch", "base_sha", "")
    head = build_architecture(head_root, "Autoresearch", "head_sha", "")
    assert base["contracts"][0]["required_args"] == ["--mode"]
    assert head["contracts"][0]["required_args"] == ["--mode", "--must-have"]

    changed = {"autoresearch/jobs/action_log.py": 1}
    d = build_delta(base, head, changed, pr=165, issue=None)

    required = [x for x in d["cross_repo"] if x["impact"] == "required-arg-added"]
    assert required == [{"contract": "batch-contract-v1", "impact": "required-arg-added",
                         "breaking": True, "details": "--must-have 필수 인자 추가"}]
    assert not any(x["impact"] == "optional-arg-added" for x in d["cross_repo"])


# --- Critical 2 (라운드 3): 기존 optional 인자의 required 뒤집기가 불가시 ---
# 기존 코드는 required_added = [a for a in added if a in new_required]로 "새로
# 추가된" 플래그만 본다. 기존 플래그가 optional -> required로 뒤집혀도 added에도
# removed에도 나타나지 않으므로 완전히 사라진다 — airflow가 소비하는 계약이
# 파괴됐는데 optional-arg-added(무관 플래그)만 잡혀 "하위호환" 초록이 거짓으로 뜬다.

def test_cross_repo_existing_optional_arg_becomes_required_is_breaking():
    head = _head()
    # cli_args 집합 자체는 이미 --max-users가 추가된 상태(added) — 여기서는 기존에도
    # 있던 --mode가 required로 뒤집히는 것만 별도로 확인한다.
    head["contracts"][0]["required_args"] = ["--mode"]
    d = _delta(head)
    flipped = [x for x in d["cross_repo"] if x["impact"] == "arg-became-required"]
    assert flipped and flipped[0]["breaking"] is True
    assert flipped[0]["contract"] == "batch-contract-v1"
    assert "--mode" in flipped[0]["details"]


def test_cross_repo_existing_required_arg_becomes_optional_is_nonbreaking():
    # 반대 방향(required -> optional)은 완화이므로 breaking이면 안 된다.
    base = copy.deepcopy(BASE)
    base["contracts"][0]["required_args"] = ["--mode"]
    head = copy.deepcopy(base)
    head["revision"] = "head000"
    head["contracts"][0]["required_args"] = []
    d = build_delta(base, head, CHANGED, pr=120,
                    issue={"number": 118, "title": "t", "body_excerpt": "b"})
    assert not any(x["impact"] == "arg-became-required" for x in d["cross_repo"])
    relaxed = [x for x in d["cross_repo"] if x["impact"] == "arg-became-optional"]
    assert relaxed and relaxed[0]["breaking"] is False
    assert "--mode" in relaxed[0]["details"]


def test_build_delta_end_to_end_existing_optional_arg_becomes_required_is_breaking(tmp_path):
    # 라이브 재현: 선택 인자(--dry-run)를 새로 추가하면서 동시에 기존
    # --youtube-base-path를 required=True로 뒤집는 PR. 유일한 배지가 "하위호환"
    # 초록이면 안 된다 — 실제 소스를 build_architecture로 추출해 전 층위로 확인한다.
    base_root, head_root = tmp_path / "base_repo", tmp_path / "head_repo"
    _write_module(base_root, "autoresearch/jobs/__init__.py",
                  'BATCH_CONTRACT_VERSION = "batch-contract-v1"\n')
    _write_module(base_root, "autoresearch/jobs/action_log.py",
                  'import argparse\n\n'
                  'def _p():\n    p = argparse.ArgumentParser()\n'
                  '    p.add_argument("--mode", required=True)\n'
                  '    p.add_argument("--youtube-base-path")\n    return p\n')
    _write_module(head_root, "autoresearch/jobs/__init__.py",
                  'BATCH_CONTRACT_VERSION = "batch-contract-v1"\n')
    _write_module(head_root, "autoresearch/jobs/action_log.py",
                  'import argparse\n\n'
                  'def _p():\n    p = argparse.ArgumentParser()\n'
                  '    p.add_argument("--mode", required=True)\n'
                  '    p.add_argument("--youtube-base-path", required=True)\n'
                  '    p.add_argument("--dry-run")\n    return p\n')

    base = build_architecture(base_root, "Autoresearch", "base_sha", "")
    head = build_architecture(head_root, "Autoresearch", "head_sha", "")
    assert base["contracts"][0]["required_args"] == ["--mode"]
    assert head["contracts"][0]["required_args"] == ["--mode", "--youtube-base-path"]

    changed = {"autoresearch/jobs/action_log.py": 1}
    d = build_delta(base, head, changed, pr=165, issue=None)

    flipped = [x for x in d["cross_repo"] if x["impact"] == "arg-became-required"]
    assert flipped == [{"contract": "batch-contract-v1", "impact": "arg-became-required",
                        "breaking": True, "details": "--youtube-base-path 인자가 필수로 변경됨"}]
    # --dry-run은 새로 추가된 선택 인자이므로 별도로 optional-arg-added여야 한다.
    optional = [x for x in d["cross_repo"] if x["impact"] == "optional-arg-added"]
    assert optional and "--dry-run" in optional[0]["details"]
    # 뒤집힌 기존 플래그가 required-arg-added(신규 추가 취급)로 이중 보고되면 안 된다.
    assert not any(x["impact"] == "required-arg-added" for x in d["cross_repo"])


def test_build_delta_end_to_end_optional_cli_arg_added_stays_nonbreaking(tmp_path):
    base_root, head_root = tmp_path / "base_repo", tmp_path / "head_repo"
    _write_module(base_root, "autoresearch/jobs/__init__.py",
                  'BATCH_CONTRACT_VERSION = "batch-contract-v1"\n')
    _write_module(base_root, "autoresearch/jobs/action_log.py",
                  'import argparse\n\n'
                  'def _p():\n    p = argparse.ArgumentParser()\n'
                  '    p.add_argument("--mode", required=True)\n    return p\n')
    _write_module(head_root, "autoresearch/jobs/__init__.py",
                  'BATCH_CONTRACT_VERSION = "batch-contract-v1"\n')
    _write_module(head_root, "autoresearch/jobs/action_log.py",
                  'import argparse\n\n'
                  'def _p():\n    p = argparse.ArgumentParser()\n'
                  '    p.add_argument("--mode", required=True)\n'
                  '    p.add_argument("--dry-run")\n    return p\n')

    base = build_architecture(base_root, "Autoresearch", "base_sha", "")
    head = build_architecture(head_root, "Autoresearch", "head_sha", "")
    changed = {"autoresearch/jobs/action_log.py": 1}
    d = build_delta(base, head, changed, pr=165, issue=None)

    assert not any(x["impact"] == "required-arg-added" for x in d["cross_repo"])
    optional = [x for x in d["cross_repo"] if x["impact"] == "optional-arg-added"]
    assert optional == [{"contract": "batch-contract-v1", "impact": "optional-arg-added",
                         "breaking": False, "details": "--dry-run 인자 추가"}]


# --- FG-2 (최종 전체 리뷰): pydantic 필드 타입 변경이 "계약 무변경" 초록을 만듦 ---
# 수작업 dict(필드 이름만 문자열)로는 extract_module_info의 실제 어노테이션
# 직렬화 경로를 거치지 않으므로, 실제 소스를 extract_module_info로 추출해
# event_id: str -> event_id: int 변경이 schema_changes에 removed(breaking)+
# added(non-breaking) 쌍으로 침묵 없이 나타나는지 확인한다.

def test_schema_field_type_change_surfaces_via_real_extraction():
    base_src = "from pydantic import BaseModel\n\nclass EventLog(BaseModel):\n    event_id: str\n"
    head_src = "from pydantic import BaseModel\n\nclass EventLog(BaseModel):\n    event_id: int\n"
    base_mod = extract_module_info(base_src, "action_logs.schema", "action_logs",
                                   "autoresearch/action_logs/schema.py")
    head_mod = extract_module_info(head_src, "action_logs.schema", "action_logs",
                                   "autoresearch/action_logs/schema.py")
    assert base_mod["schema_fields"] == {"EventLog": ["event_id: str"]}
    assert head_mod["schema_fields"] == {"EventLog": ["event_id: int"]}

    base = {**BASE, "modules": [base_mod]}
    head = {**BASE, "revision": "head000", "modules": [head_mod]}
    changed = {"autoresearch/action_logs/schema.py": 1}
    d = build_delta(base, head, changed, pr=165, issue=None)

    removed = [s for s in d["schema_changes"] if s["change"] == "removed"]
    added = [s for s in d["schema_changes"] if s["change"] == "added"]
    assert removed == [{"model": "EventLog", "module": "action_logs.schema",
                        "field": "event_id: str", "change": "removed", "breaking": True}]
    assert added == [{"model": "EventLog", "module": "action_logs.schema",
                      "field": "event_id: int", "change": "added", "breaking": False}]
    # ACTION_LOG_SCHEMA_VERSION 등 버전 상수가 없는 최소 픽스처라 unchanged_contracts는
    # 비어 있다 — 이 테스트의 요점은 "타입 변경이 침묵하지 않는다"는 것 자체다.


# --- Critical 1 (라운드 3): Field(ge=...) 제약 변경·선택→필수화가 우변을 버려 침묵함 ---
# 라이브 재현: autoresearch/action_logs/schema.py의 click_propensity 필드가
# Field(ge=0.0, le=1.0) -> Field(ge=0.5, le=1.0)로 바뀌어도(구 모델은 0.2를 수용,
# 신 모델은 거부 — 계약이 명백히 파괴됨) 어노테이션만 문자열화하면 schema_changes에
# 아무것도 나타나지 않아 "계약 불변" 초록이 거짓으로 뜬다.

def test_schema_field_constraint_change_surfaces_via_real_extraction():
    base_src = ("from pydantic import BaseModel, Field\n\n"
                "class ImpressionDraft(BaseModel):\n"
                "    click_propensity: float = Field(ge=0.0, le=1.0)\n")
    head_src = ("from pydantic import BaseModel, Field\n\n"
                "class ImpressionDraft(BaseModel):\n"
                "    click_propensity: float = Field(ge=0.5, le=1.0)\n")
    base_mod = extract_module_info(base_src, "action_logs.schema", "action_logs",
                                   "autoresearch/action_logs/schema.py")
    head_mod = extract_module_info(head_src, "action_logs.schema", "action_logs",
                                   "autoresearch/action_logs/schema.py")
    base = {**BASE, "modules": [base_mod]}
    head = {**BASE, "revision": "head000", "modules": [head_mod]}
    changed = {"autoresearch/action_logs/schema.py": 1}
    d = build_delta(base, head, changed, pr=165, issue=None)

    removed = [s for s in d["schema_changes"] if s["change"] == "removed"]
    added = [s for s in d["schema_changes"] if s["change"] == "added"]
    assert removed == [{"model": "ImpressionDraft", "module": "action_logs.schema",
                        "field": "click_propensity: float = Field(ge=0.0, le=1.0)",
                        "change": "removed", "breaking": True}]
    assert added == [{"model": "ImpressionDraft", "module": "action_logs.schema",
                      "field": "click_propensity: float = Field(ge=0.5, le=1.0)",
                      "change": "added", "breaking": False}]


def test_schema_field_optional_becomes_required_surfaces_via_real_extraction():
    # 기본값 있는 선택 필드(`rank: int | None = None`)가 필수 필드(`rank: int | None`)로
    # 바뀌는 것도 같은 침묵 경로 — 우변(`= None`)이 사라지는 것 자체가 신호다.
    base_src = "from pydantic import BaseModel\n\nclass EventLog(BaseModel):\n    rank: int | None = None\n"
    head_src = "from pydantic import BaseModel\n\nclass EventLog(BaseModel):\n    rank: int | None\n"
    base_mod = extract_module_info(base_src, "action_logs.schema", "action_logs",
                                   "autoresearch/action_logs/schema.py")
    head_mod = extract_module_info(head_src, "action_logs.schema", "action_logs",
                                   "autoresearch/action_logs/schema.py")
    base = {**BASE, "modules": [base_mod]}
    head = {**BASE, "revision": "head000", "modules": [head_mod]}
    changed = {"autoresearch/action_logs/schema.py": 1}
    d = build_delta(base, head, changed, pr=165, issue=None)

    removed = [s for s in d["schema_changes"] if s["change"] == "removed"]
    added = [s for s in d["schema_changes"] if s["change"] == "added"]
    assert removed == [{"model": "EventLog", "module": "action_logs.schema",
                        "field": "rank: int | None = None", "change": "removed",
                        "breaking": True}]
    assert added == [{"model": "EventLog", "module": "action_logs.schema",
                      "field": "rank: int | None", "change": "added", "breaking": False}]
