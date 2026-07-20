import copy
from pathlib import Path
from typing import Protocol, TypedDict

import pytest

from tools.archmap import delta as delta_module
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
    assert d["sidecar_stale"] == ["autoresearch/action_logs/schema.py"]


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
    assert required == [{"contract": "batch-contract-v1:jobs.action_log",
                         "impact": "required-arg-added",
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
    assert flipped == [{"contract": "batch-contract-v1:jobs.action_log",
                        "impact": "arg-became-required",
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
    assert optional == [{"contract": "batch-contract-v1:jobs.action_log",
                         "impact": "optional-arg-added",
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


# --- 검사관 원본 재현 A/B (합집합이 job별 변경을 은폐) ---
# _batch_contract가 jobs/*.py 전체의 CLI 인자를 합집합으로 묶어 계약 하나를
# 만들면, 한 job에서 플래그를 뒤집거나 지워도 다른 job에 같은 플래그가 남아 있는
# 한 합집합 자체는 안 바뀐다 — 실제 레포가 정확히 이 모양이다:
# --youtube-base-path는 action_log.py에서는 선택, youtube_backfill.py/
# action_log_quality.py/youtube_trending.py에서는 필수. 두 job을 함께 넣어야
# (action_log.py만으로는 애초에 겹치는 필수 플래그가 없어 재현이 안 됨) 실제
# 은폐가 재현된다.

_YOUTUBE_BACKFILL_SRC = ('import argparse\n\n'
                         'def _p():\n    p = argparse.ArgumentParser()\n'
                         '    p.add_argument("--source-path", required=True)\n'
                         '    p.add_argument("--youtube-base-path", required=True)\n'
                         '    return p\n')


def _write_two_job_repo(root: Path, action_log_src: str) -> None:
    _write_module(root, "autoresearch/jobs/__init__.py",
                  'BATCH_CONTRACT_VERSION = "batch-contract-v1"\n')
    _write_module(root, "autoresearch/jobs/action_log.py", action_log_src)
    _write_module(root, "autoresearch/jobs/youtube_backfill.py", _YOUTUBE_BACKFILL_SRC)


def test_repro_a_required_flip_hidden_by_union_now_surfaces_as_breaking(tmp_path):
    # 재현 A: action_log.py의 기존 --youtube-base-path를 required=True로 뒤집고,
    # 동시에 새 선택 인자 --region을 추가. airflow가 --youtube-base-path 없이
    # action_log job을 호출하면 실제로 실패하므로 breaking 경고가 떠야 한다.
    base_root, head_root = tmp_path / "base_repo", tmp_path / "head_repo"
    base_action_log = ('import argparse\n\n'
                       'def _p():\n    p = argparse.ArgumentParser()\n'
                       '    p.add_argument("--mode", required=True)\n'
                       '    p.add_argument("--youtube-base-path")\n    return p\n')
    head_action_log = ('import argparse\n\n'
                       'def _p():\n    p = argparse.ArgumentParser()\n'
                       '    p.add_argument("--mode", required=True)\n'
                       '    p.add_argument("--youtube-base-path", required=True)\n'
                       '    p.add_argument("--region")\n    return p\n')
    _write_two_job_repo(base_root, base_action_log)
    _write_two_job_repo(head_root, head_action_log)

    base = build_architecture(base_root, "Autoresearch", "base_sha", "")
    head = build_architecture(head_root, "Autoresearch", "head_sha", "")
    changed = {"autoresearch/jobs/action_log.py": 2}
    d = build_delta(base, head, changed, pr=165, issue=None)

    action_log_events = [x for x in d["cross_repo"]
                         if x["contract"].endswith("jobs.action_log")]
    flipped = [x for x in action_log_events if x["impact"] == "arg-became-required"]
    assert flipped and flipped[0]["breaking"] is True, \
        f"거짓 초록: --youtube-base-path 필수화가 침묵함. cross_repo={d['cross_repo']}"
    assert "--youtube-base-path" in flipped[0]["details"]
    optional = [x for x in action_log_events if x["impact"] == "optional-arg-added"]
    assert optional and "--region" in optional[0]["details"]
    # youtube_backfill 계약 자체는 변경이 없었으므로 cross_repo에 나타나면 안 된다
    # — job별 분리가 무관한 계약까지 오염시키지 않는지 확인.
    assert not [x for x in d["cross_repo"] if x["contract"].endswith("jobs.youtube_backfill")]


def test_repro_b_arg_removed_hidden_by_union_now_surfaces_as_breaking(tmp_path):
    # 재현 B: action_log.py에서 --youtube-base-path를 완전 삭제하고, 동시에 새
    # 선택 인자를 추가. 다른 job(youtube_backfill.py)에 같은 이름의 필수 플래그가
    # 남아 있으므로, 합집합 설계에서는 cli_args 합집합에 플래그가 그대로 남아
    # arg-removed가 완전히 침묵한다.
    base_root, head_root = tmp_path / "base_repo", tmp_path / "head_repo"
    base_action_log = ('import argparse\n\n'
                       'def _p():\n    p = argparse.ArgumentParser()\n'
                       '    p.add_argument("--mode", required=True)\n'
                       '    p.add_argument("--youtube-base-path")\n    return p\n')
    head_action_log = ('import argparse\n\n'
                       'def _p():\n    p = argparse.ArgumentParser()\n'
                       '    p.add_argument("--mode", required=True)\n'
                       '    p.add_argument("--region")\n    return p\n')
    _write_two_job_repo(base_root, base_action_log)
    _write_two_job_repo(head_root, head_action_log)

    base = build_architecture(base_root, "Autoresearch", "base_sha", "")
    head = build_architecture(head_root, "Autoresearch", "head_sha", "")
    changed = {"autoresearch/jobs/action_log.py": 2}
    d = build_delta(base, head, changed, pr=165, issue=None)

    action_log_events = [x for x in d["cross_repo"]
                         if x["contract"].endswith("jobs.action_log")]
    removed = [x for x in action_log_events if x["impact"] == "arg-removed"]
    assert removed and removed[0]["breaking"] is True, \
        f"거짓 초록: --youtube-base-path 삭제가 침묵함. cross_repo={d['cross_repo']}"
    assert "--youtube-base-path" in removed[0]["details"]
    assert not [x for x in d["cross_repo"] if x["contract"].endswith("jobs.youtube_backfill")]


class _SidecarWritable(Protocol):
    def __setitem__(
        self,
        key: str,
        value: str | list[str] | None,
        /,
    ) -> None: ...


class _PublicSymbolFixture(TypedDict):
    name: str
    kind: str
    sig: str | None
    line: int


class _VersionConstFixture(TypedDict):
    value: str
    line: int


def _set_sidecar(module: _SidecarWritable, role: str | None,
                 owns: list[str] | None = None,
                 not_owns: list[str] | None = None,
                 stage: str | None = None) -> None:
    if stage is not None:
        module["stage"] = stage
    module["role"] = role
    module["owns"] = [] if owns is None else owns
    module["not_owns"] = [] if not_owns is None else not_owns


def test_stage_only_sidecar_change_is_not_treated_as_unchanged() -> None:
    # Given: public/version surface changes while the sidecar stage changes with it.
    base = copy.deepcopy(BASE)
    head = _head()
    _set_sidecar(base["modules"][0], "역할", ["하나"], ["둘"], stage="action_logs")
    _set_sidecar(head["modules"][0], "역할", ["하나"], ["둘"], stage="new_stage")
    head["modules"][0]["stage"] = "new_stage"

    # When: the delta compares the sidecar meaning.
    delta = build_delta(base, head, CHANGED, pr=120, issue=None)

    # Then: stage is part of the meaning, so this changed sidecar is fresh.
    assert delta["sidecar_stale"] == []


def test_parse_name_status_counts_only_exact_delete_and_deduplicates() -> None:
    # Given: 실제 name-status에 삭제·rename·copy·수정 상태가 섞여 있다.
    text = (
        "D\tautoresearch/action_logs/removed.py\n"
        "D\tautoresearch/action_logs/removed.py\n"
        "R100\tautoresearch/action_logs/old.py\tautoresearch/action_logs/new.py\n"
        "C100\tautoresearch/action_logs/source.py\tautoresearch/action_logs/copy.py\n"
        "M\tautoresearch/action_logs/changed.py\n"
    )

    # When: name-status 사실을 삭제 집합으로 파싱한다.
    deleted = delta_module.parse_name_status(text)

    # Then: 정확히 D 상태만 중복 없이 삭제로 인정한다.
    assert deleted == {"autoresearch/action_logs/removed.py"}


def test_parse_numstat_nul_round_trips_unicode_paths_and_rename_copy() -> None:
    # Given: 실제 git --numstat -z의 일반·rename/copy 레코드 바이트다.
    modified_path = "autoresearch/action_logs/수정.py"
    deleted_path = "autoresearch/action_logs/삭제.py"
    rename_old = "autoresearch/action_logs/이름전.py"
    rename_new = "autoresearch/action_logs/이름후.py"
    copy_source = "autoresearch/action_logs/복사원본.py"
    copy_target = "autoresearch/action_logs/복사본.py"
    raw = (
        f"1\t1\t{modified_path}\0"
        f"0\t2\t{deleted_path}\0"
        f"0\t0\t\0{rename_old}\0{rename_new}\0"
        f"0\t0\t\0{copy_source}\0{copy_target}\0"
    ).encode()

    # When: NUL-delimited numstat를 파싱한다.
    changed = delta_module.parse_numstat(raw)

    # Then: UTF-8 POSIX 경로와 rename/copy 양쪽 경로가 그대로 보존된다.
    assert changed == {
        modified_path: 1,
        deleted_path: 0,
        rename_old: 0,
        rename_new: 0,
        copy_source: 0,
        copy_target: 0,
    }


def test_parse_name_status_nul_counts_only_exact_unicode_delete() -> None:
    # Given: 실제 git --name-status -z의 수정·삭제·rename·copy 바이트다.
    modified_path = "autoresearch/action_logs/수정.py"
    deleted_path = "autoresearch/action_logs/삭제.py"
    raw = (
        f"M\0{modified_path}\0"
        f"D\0{deleted_path}\0"
        "R100\0autoresearch/action_logs/이름전.py\0"
        "autoresearch/action_logs/이름후.py\0"
        "C100\0autoresearch/action_logs/복사원본.py\0"
        "autoresearch/action_logs/복사본.py\0"
    ).encode()

    # When: NUL-delimited name-status를 삭제 사실로 파싱한다.
    deleted = delta_module.parse_name_status(raw)

    # Then: literal Unicode 경로의 exact D만 삭제이고 rename/copy는 제외된다.
    assert deleted == {deleted_path}


def test_public_and_version_changes_mark_missing_sidecar_stale() -> None:
    # Given: public signature와 version constant가 바뀌었고 head sidecar가 없다.
    d = _delta()

    # When: delta의 stale 사실을 읽는다.
    stale = d["sidecar_stale"]

    # Then: 기존 빈 배열이 아니라 해당 POSIX 모듈 경로가 stale이다.
    assert stale == ["autoresearch/action_logs/schema.py"]
    assert all(set(item) == {"module", "name"} for item in d["breaking_signatures"])
    assert all(isinstance(item["module"], str) and isinstance(item["name"], str)
               for item in d["breaking_signatures"])


def test_valid_to_missing_sidecar_is_stale() -> None:
    # Given: base에는 valid sidecar가 있고 head에서는 sidecar가 사라졌다.
    base = copy.deepcopy(BASE)
    _set_sidecar(base["modules"][0], "같은 역할", ["하나"], ["둘"])
    head = _head()

    # When: public/version 변경을 delta로 계산한다.
    d = build_delta(base, head, CHANGED, pr=120, issue=None)

    # Then: head sidecar 부재는 stale이다.
    assert d["sidecar_stale"] == ["autoresearch/action_logs/schema.py"]


def test_meaningful_sidecar_change_is_fresh() -> None:
    # Given: public/version 변경과 함께 sidecar 의미값이 달라졌다.
    base = copy.deepcopy(BASE)
    _set_sidecar(base["modules"][0], "이전 역할", ["하나"], ["둘"])
    head = _head()
    _set_sidecar(head["modules"][0], "새 역할", ["셋"], ["넷"])

    # When: delta를 계산한다.
    d = build_delta(base, head, CHANGED, pr=120, issue=None)

    # Then: 의미 있는 갱신은 stale를 해소한다.
    assert d["sidecar_stale"] == []


def test_reorder_and_comment_only_sidecar_change_is_stale(tmp_path) -> None:
    # Given: public signature가 바뀌지만 sidecar는 주석·포맷·목록 순서만 바뀐다.
    base_root, head_root = tmp_path / "base_repo", tmp_path / "head_repo"
    _write_module(base_root, "autoresearch/action_logs/daily.py",
                  '__arch__ = {\n'
                  '    "stage": "action_logs", "role": "역할",\n'
                  '    "owns": ["하나", "둘"], "not_owns": ["셋"],\n'
                  '}\n'
                  'def run(request):\n'
                  '    return request\n')
    _write_module(head_root, "autoresearch/action_logs/daily.py",
                  '# 설명 주석만 바뀜\n'
                  '__arch__={"stage": "action_logs",  # 인라인 주석\n'
                  '          "role": "역할", "owns": ["둘", "하나"], '
                  '"not_owns": ["셋"]}\n'
                  'def run(request, optional=None):\n'
                  '    return request\n')
    base = build_architecture(base_root, "Autoresearch", "base_sha", "")
    head = build_architecture(head_root, "Autoresearch", "head_sha", "")

    # When: 실제 manifest 사실로 delta를 계산한다.
    d = build_delta(base, head, {"autoresearch/action_logs/daily.py": 2}, pr=187, issue=None)

    # Then: sidecar 의미값 동일이므로 stale이다.
    assert d["sidecar_stale"] == ["autoresearch/action_logs/daily.py"]


def test_private_only_body_change_is_fresh(tmp_path) -> None:
    # Given: private 함수 본문만 바뀌고 public/version surface는 동일하다.
    base_root, head_root = tmp_path / "base_repo", tmp_path / "head_repo"
    _write_module(base_root, "autoresearch/action_logs/daily.py",
                  '__arch__ = {"stage": "action_logs", "role": "역할", '
                  '"owns": ["하나"], "not_owns": []}\n'
                  'def _private():\n'
                  '    return 1\n')
    _write_module(head_root, "autoresearch/action_logs/daily.py",
                  '__arch__ = {"stage": "action_logs", "role": "역할", '
                  '"owns": ["하나"], "not_owns": []}\n'
                  'def _private():\n'
                  '    return 2\n')
    base = build_architecture(base_root, "Autoresearch", "base_sha", "")
    head = build_architecture(head_root, "Autoresearch", "head_sha", "")

    # When: private-only change를 delta로 계산한다.
    d = build_delta(base, head, {"autoresearch/action_logs/daily.py": 1}, pr=187, issue=None)

    # Then: public/version trigger가 없으므로 fresh이다.
    assert d["sidecar_stale"] == []


@pytest.mark.parametrize(
    ("public_symbols", "version_consts", "role", "expected_stale"),
    [
        ([{"name": "new_public", "kind": "function", "sig": "()", "line": 1}],
         {}, None, True),
        ([{"name": "new_public", "kind": "function", "sig": "()", "line": 1}],
         {}, "신규 역할", False),
        ([], {"NEW_VERSION": {"value": "v1", "line": 1}}, None, True),
        ([], {}, None, False),
    ],
)
def test_new_module_public_missing_valid_and_private_only(
    public_symbols: list[_PublicSymbolFixture],
    version_consts: dict[str, _VersionConstFixture],
    role: str | None,
    expected_stale: bool,
) -> None:
    # Given: public surface가 있는 신규 모듈 또는 private-only 신규 모듈이다.
    path = "autoresearch/action_logs/new.py"
    head = _head()
    module = {
        "id": "action_logs.new", "stage": "action_logs", "path": path,
        "role": None, "owns": [], "not_owns": [],
        "public_symbols": public_symbols,
        "version_consts": version_consts,
        "schema_fields": {}, "imports": [],
    }
    _set_sidecar(module, role)
    head["modules"].append(module)
    changed = dict(CHANGED)
    changed[path] = 1

    # When: 신규 모듈 delta를 계산한다.
    d = build_delta(BASE, head, changed, pr=187, issue=None)

    # Then: public 신규 missing만 stale이고 valid/private-only는 fresh이다.
    assert (path in d["sidecar_stale"]) is expected_stale


def test_actual_delete_excludes_module_from_stale() -> None:
    # Given: manifest에서는 public 모듈이 사라졌지만 name-status가 실제 D를 증명한다.
    head = copy.deepcopy(_head())
    head["modules"] = []
    path = "autoresearch/action_logs/schema.py"
    deleted = delta_module.parse_name_status(f"D\t{path}\n")

    # When: 실제 삭제 집합을 함께 전달한다.
    d = build_delta(BASE, head, {path: 0}, pr=187, issue=None, deleted_paths=deleted)

    # Then: 삭제된 파일은 sidecar stale 계산에서 제외한다.
    assert d["sidecar_stale"] == []


def test_retained_public_to_private_transition_is_stale() -> None:
    # Given: 파일은 manifest에 남아 있으나 마지막 public/version surface가 사라졌다.
    head = copy.deepcopy(BASE)
    _set_sidecar(head["modules"][0], "역할", ["하나"], [])
    head["modules"][0]["public_symbols"] = []
    head["modules"][0]["version_consts"] = {}
    base = copy.deepcopy(BASE)
    _set_sidecar(base["modules"][0], "역할", ["하나"], [])

    # When: 파일 삭제 증거 없이 delta를 계산한다.
    d = build_delta(base, head, {"autoresearch/action_logs/schema.py": 1}, pr=187, issue=None)

    # Then: retained public-to-private transition은 stale이다.
    assert d["sidecar_stale"] == ["autoresearch/action_logs/schema.py"]


def test_rename_and_copy_are_not_actual_deletions() -> None:
    # Given: name-status가 rename/copy만 보고한다.
    text = (
        "R100\tautoresearch/action_logs/old.py\tautoresearch/action_logs/new.py\n"
        "C100\tautoresearch/action_logs/source.py\tautoresearch/action_logs/copy.py\n"
    )

    # When: 삭제 집합으로 파싱한다.
    deleted = delta_module.parse_name_status(text)

    # Then: 기존 rename/copy 판정을 삭제로 오인하지 않는다.
    assert deleted == set()


def test_stale_paths_are_sorted_and_unique() -> None:
    # Given: 두 모듈이 public surface를 바꾸고 입력 changed 순서는 역순이다.
    base = copy.deepcopy(BASE)
    extra_path = "autoresearch/action_logs/aaa.py"
    extra = copy.deepcopy(BASE["modules"][0])
    extra["id"] = "action_logs.aaa"
    extra["path"] = extra_path
    extra["public_symbols"] = [{"name": "extra", "kind": "function",
                                 "sig": "(value)", "line": 1}]
    extra["version_consts"] = {}
    extra["schema_fields"] = {}
    extra["imports"] = []
    base["modules"].append(extra)
    head = copy.deepcopy(base)
    head["modules"][0]["public_symbols"][0]["sig"] = "(request, generator, required)"
    head["modules"][1]["public_symbols"][0]["sig"] = "(value, required)"
    changed = {extra_path: 2, "autoresearch/action_logs/schema.py": 2}

    # When: stale 목록을 계산한다.
    d = build_delta(base, head, changed, pr=187, issue=None)

    # Then: POSIX 사전순·중복 제거 계약을 지킨다.
    assert d["sidecar_stale"] == [extra_path, "autoresearch/action_logs/schema.py"]
    assert d["sidecar_stale"] == sorted(set(d["sidecar_stale"]))
