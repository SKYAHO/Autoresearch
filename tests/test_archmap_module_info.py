import textwrap

from tools.archmap.module_info import extract_module_info

SAMPLE = textwrap.dedent('''
    """예시 모듈."""
    from pydantic import BaseModel
    from autoresearch.action_logs import candidate
    import autoresearch.action_logs.schema
    import json

    ACTION_LOG_SCHEMA_VERSION = "action_log_schema_v1"
    PROMPT_VERSION = "action_log_ctr_v4"
    TARGET_COUNTRY = "KR"
    MAX_RETRY = 3
    _PRIVATE = "x"

    CANDIDATE_COLUMNS = ["index", "title"]


    class EventLog(BaseModel):
        event_id: str
        clicked: bool
        model_config = {"frozen": True}
        _cache: dict = {}


    class Helper:
        pass


    def run_daily(request, generator, max_users=None, *, seed=42):
        pass


    def _hidden():
        pass
''')


def _info():
    return extract_module_info(SAMPLE, "action_logs.schema", "action_logs",
                               "autoresearch/action_logs/schema.py")


def test_identity_fields():
    info = _info()
    assert info["id"] == "action_logs.schema"
    assert info["stage"] == "action_logs"
    assert info["path"] == "autoresearch/action_logs/schema.py"
    assert info["role"] is None and info["owns"] == [] and info["not_owns"] == []


def test_public_symbols_exclude_private():
    names = {s["name"]: s for s in _info()["public_symbols"]}
    assert "run_daily" in names and "_hidden" not in names and "_PRIVATE" not in names
    assert names["EventLog"]["kind"] == "class"
    assert names["CANDIDATE_COLUMNS"]["kind"] == "const"
    assert names["run_daily"]["sig"] == "(request, generator, max_users=None, *, seed=42)"
    assert names["run_daily"]["line"] > 0


def test_version_consts_rule():
    consts = _info()["version_consts"]
    assert consts["ACTION_LOG_SCHEMA_VERSION"]["value"] == "action_log_schema_v1"
    assert consts["PROMPT_VERSION"]["value"] == "action_log_ctr_v4"
    assert consts["TARGET_COUNTRY"]["value"] == "KR"      # 허용목록
    assert "MAX_RETRY" not in consts                       # 문자열 아님 + _VERSION 아님
    assert consts["PROMPT_VERSION"]["line"] > 0


def test_schema_fields_only_from_basemodel():
    # FG-2: 필드는 "이름: 타입" 문자열로 나온다 — 타입 변경이 delta.py의 집합
    # 비교에서 침묵하지 않게 하려면 이름만으로는 부족하다(주석 참고).
    fields = _info()["schema_fields"]
    assert fields == {"EventLog": ["event_id: str", "clicked: bool"]}  # Helper 제외, model_config·_cache 제외


def test_imports_internal_only_prefix_stripped():
    assert _info()["imports"] == ["action_logs", "action_logs.schema"]
