import copy

from tools.archmap.delta import build_delta, parse_numstat

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
                   "cli_args": ["--mode"], "consumed_by": ["Autoresearch-airflow"]}],
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


def test_version_change_nonbreaking_and_unchanged_contract():
    d = _delta()
    (v,) = d["version_changes"]
    assert v["const"] == "PROMPT_VERSION" and v["from"].endswith("v3") \
        and v["to"].endswith("v4") and v["breaking"] is False
    (u,) = d["unchanged_contracts"]
    assert u["const"] == "ACTION_LOG_SCHEMA_VERSION" and u["value"] == "action_log_schema_v1"


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


def test_cross_repo_arg_added_and_removed():
    d = _delta()
    (x,) = d["cross_repo"]
    assert x["contract"] == "batch-contract-v1" and x["impact"] == "optional-arg-added" \
        and x["breaking"] is False
    head = _head()
    head["contracts"][0]["cli_args"] = []
    removed = [x for x in _delta(head)["cross_repo"] if x["impact"] == "arg-removed"]
    assert removed and removed[0]["breaking"] is True


def test_tests_section():
    d = _delta()
    assert d["tests"] == {"files": ["tests/test_action_logs_daily.py"], "lines_added": 52}
    assert d["sidecar_stale"] == []


# --- Critical 1: git 기본 rename 축약 표기 (실제 `git diff --numstat` 출력으로 확인) ---
# 아래 문자열은 임시 저장소에서 실제로 `git mv` + `git diff --cached --numstat`을
# 실행해 얻은 값이다 (-M 플래그 없이도 git 2.34는 기본으로 rename을 압축한다).

def test_parse_numstat_rename_cross_dir_no_common_affix():
    # 공통 접두/접미가 전혀 없으면 "old => new" 형태 (중괄호 없음)
    text = "3\t0\tautoresearch/action_logs/schema.py => other_dir/schema_new.py\n"
    assert parse_numstat(text) == {"other_dir/schema_new.py": 3}


def test_parse_numstat_rename_same_dir_braces():
    # 같은 디렉터리 내 rename: "dir/{old => new}" (접미 없음)
    text = "3\t0\tautoresearch/action_logs/{schema.py => schema_new.py}\n"
    assert parse_numstat(text) == {"autoresearch/action_logs/schema_new.py": 3}


def test_parse_numstat_rename_prefix_and_suffix_common():
    # 접두("autoresearch/")와 접미("/schema.py")가 모두 있는 디렉터리 rename
    text = "3\t0\tautoresearch/{action_logs => jobs}/schema.py\n"
    assert parse_numstat(text) == {"autoresearch/jobs/schema.py": 3}


def test_parse_numstat_rename_empty_old_inner():
    # 중괄호 안 old 쪽이 빈 문자열: 디렉터리 계층이 새로 생기는 경우
    text = "3\t0\tautoresearch/{ => sub}/schema.py\n"
    assert parse_numstat(text) == {"autoresearch/sub/schema.py": 3}


def test_parse_numstat_rename_empty_new_inner():
    # 중괄호 안 new 쪽이 빈 문자열: 디렉터리 계층이 사라지는 경우 (이중 슬래시 방지 확인)
    text = "3\t0\tautoresearch/{sub => }/schema.py\n"
    assert parse_numstat(text) == {"autoresearch/schema.py": 3}


def test_parse_numstat_rename_top_level_no_brace_degenerate():
    # 공통 접두/접미가 없는 최상위 rename은 git이 중괄호 없이 "old => new"로 낸다
    text = "0\t0\tsub/schema.py => schema.py\n"
    assert parse_numstat(text) == {"schema.py": 0}


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
