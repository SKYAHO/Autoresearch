from tools.archmap.comment import MARKER, render_comment

DELTA = {
    "schema_version": "archmap-v0", "repo": "Autoresearch", "pr": 120,
    "base_sha": "aaaaaaa", "head_sha": "3be5fae",
    "issue": {"number": 118, "title": "후보 목록을 프롬프트에 명시", "body_excerpt": "b"},
    "changed_modules": [
        {"id": "action_logs.daily", "path": "autoresearch/action_logs/daily.py",
         "stage": "action_logs",
         "symbols_changed": [{"name": "run_daily_action_log", "change": "signature", "line": 722}],
         "public_surface_changed": True}],
    "version_changes": [
        {"const": "PROMPT_VERSION", "module": "action_logs.schema",
         "from": "action_log_ctr_v3", "to": "action_log_ctr_v4", "line": 17, "breaking": False}],
    "unchanged_contracts": [
        {"const": "ACTION_LOG_SCHEMA_VERSION", "module": "action_logs.schema",
         "value": "action_log_schema_v1", "line": 16}],
    "schema_changes": [],
    "cross_repo": [{"contract": "batch-contract-v1", "impact": "optional-arg-added",
                    "breaking": False, "details": "--max-users 인자 추가"}],
    "tests": {"files": ["tests/test_action_logs_daily.py"], "lines_added": 52},
    "sidecar_stale": [], "breaking_signatures": [],
}


def test_comment_starts_with_marker():
    assert render_comment(DELTA, "http://srv/reports/Autoresearch/120").startswith(MARKER)


def test_comment_contains_facts():
    md = render_comment(DELTA, "http://srv/reports/Autoresearch/120")
    assert "action_logs" in md
    assert "PROMPT_VERSION" in md and "action_log_ctr_v4" in md
    assert "ACTION_LOG_SCHEMA_VERSION" in md and "✅" in md
    assert "batch-contract-v1" in md
    assert "+52줄" in md
    assert "http://srv/reports/Autoresearch/120" in md


def test_comment_without_server_url_notes_it():
    md = render_comment(DELTA, None)
    assert "서버 미연결" in md and "http" not in md


def test_breaking_rows_are_flagged():
    import copy
    d = copy.deepcopy(DELTA)
    d["cross_repo"][0]["breaking"] = True
    assert "⚠️" in render_comment(d, None)
