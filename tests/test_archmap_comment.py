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


def test_breaking_signatures_only_delta_shows_warning_and_symbol():
    """unchanged_contracts/version_changes/schema_changes/cross_repo가 전부 비어 있고
    breaking_signatures만 있어도 판정 표에 ⚠️와 심볼 이름이 나타나야 한다.
    (수정 전에는 render_comment가 breaking_signatures를 전혀 읽지 않아 표 자체가
    생성되지 않고 심볼 이름도 코멘트에 나타나지 않았다.)"""
    import copy
    d = copy.deepcopy(DELTA)
    d["version_changes"] = []
    d["unchanged_contracts"] = []
    d["schema_changes"] = []
    d["cross_repo"] = []
    d["breaking_signatures"] = [
        {"module": "action_logs.daily", "name": "run_daily_action_log"},
    ]
    md = render_comment(d, None)
    assert "⚠️" in md
    assert "run_daily_action_log" in md


def test_breaking_signatures_mixed_with_non_breaking_still_warns():
    """비파괴 항목(rename 등)과 breaking_signatures가 섞여 있어도 ⚠️가 표에 나타나야 한다."""
    import copy
    d = copy.deepcopy(DELTA)
    d["breaking_signatures"] = [
        {"module": "action_logs.daily", "name": "run_daily_action_log"},
    ]
    md = render_comment(d, None)
    assert "⚠️" in md
    assert "run_daily_action_log" in md


def test_table_cells_are_escaped():
    """표 셀 값에 `|`나 개행이 있으면 표가 깨지므로 이스케이프해야 한다."""
    import copy
    d = copy.deepcopy(DELTA)
    d["version_changes"][0]["from"] = "a|b\nc"
    d["version_changes"][0]["to"] = "d|e\nf"
    md = render_comment(d, None)
    lines = md.splitlines()
    version_lines = [ln for ln in lines if "PROMPT_VERSION" in ln]
    assert len(version_lines) == 1
    # 원본 개행으로 인한 행 분리가 없어야 한다 — 줄 수가 늘어나지 않음을 의미.
    assert r"a\|b c" in md
    assert r"d\|e f" in md
    # 이스케이프되지 않은 원본 값(raw `|`가 포함된 원문 그대로)은 남아있지 않아야 한다.
    assert "a|b" not in md
    assert "d|e" not in md


def test_schema_changes_are_rendered():
    """schema_changes 렌더링 분기가 실제로 실행되는지 검증한다(기존 테스트는 항상 빈 리스트)."""
    import copy
    d = copy.deepcopy(DELTA)
    d["schema_changes"] = [
        {"model": "ActionLog", "module": "action_logs.schema", "field": "user_id",
         "change": "removed", "breaking": True},
    ]
    md = render_comment(d, None)
    assert "ActionLog" in md and "user_id" in md
    assert "⚠️" in md


def test_version_changes_breaking_true_is_rendered():
    """version_changes에서 breaking: True인 경우(상수 삭제 등)도 ⚠️로 표시되어야 한다
    (기존 breaking 토글 테스트는 cross_repo만 다뤘다)."""
    import copy
    d = copy.deepcopy(DELTA)
    d["version_changes"] = [
        {"const": "OLD_CONST", "module": "action_logs.schema",
         "from": "v1", "to": None, "line": None, "breaking": True},
    ]
    md = render_comment(d, None)
    assert "OLD_CONST" in md
    assert "⚠️" in md
