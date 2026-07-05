import json
from datetime import UTC, datetime, timedelta

import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from autoresearch.action_logs.llm_generator import RuleBasedActionLogGenerator
from autoresearch.action_logs.pipeline import (
    ActionLogGenerationError,
    generate_action_log_batch,
)
from autoresearch.action_logs.schema import EventGenerationRequest, EventLog
from autoresearch.action_logs.video_source import (
    _parse_tags,
    build_fixture_video_records,
    nominal_duration_sec,
)

_FIXED_END = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)


def _fixture_users(n=6):
    cats = [
        ["Gaming", "Music"],
        ["Music", "Entertainment"],
        ["Education", "Science & Technology"],
        ["Food", "Howto & Style"],
        ["Travel & Events", "Sports"],
        ["News & Politics", "People & Blogs"],
    ]
    users = []
    for i in range(n):
        c = cats[i % len(cats)]
        users.append(
            {
                "user_id": f"vu_{i:04d}",
                "age": 20 + i,
                "sex": "male" if i % 2 else "female",
                "persona_summary": "테스트 유저",
                "primary_categories": c,
                "category_affinity": {c[0]: 0.8, c[1]: 0.6},
                "interest_keywords": ["게임" if "Gaming" in c else "음악"],
                "hobby_keywords": [],
                "lifestyle_keywords": [],
                "watch_time_band": "night",
            }
        )
    return users


def _request(tmp_path, **kw):
    base = dict(
        candidates_per_user=20,
        target_ctr=0.05,
        seed=42,
        history_end=_FIXED_END,
        history_days=30,
        output_path=str(tmp_path / "e.parquet"),
        warehouse_output_path=str(tmp_path / "e.jsonl"),
        quarantine_output_path=str(tmp_path / "q.jsonl"),
    )
    base.update(kw)
    return EventGenerationRequest(**base)


def test_end_to_end_events_hit_target_ctr_and_constraints(tmp_path):
    users, videos = _fixture_users(6), build_fixture_video_records(40)
    result = generate_action_log_batch(_request(tmp_path), users, videos, RuleBasedActionLogGenerator())

    total = result.summary["total_events"]
    assert total == 6 * 20  # 유저당 후보 20 (pool 40)
    assert result.summary["clicks"] == round(0.05 * total)  # 전역 2%(여기선 5%) 정규화
    for event in result.batch.events:
        if event.clicked == 0:
            assert event.watch_time_sec == 0 and event.liked == 0
        assert event.source == "historical"
        assert event.rank is None
        assert event.exposure_type in {"top_ranked", "exploration"}
    assert (tmp_path / "e.parquet").exists()
    assert result.summary["quarantined_users"] == 0


def test_timestamps_within_history_window(tmp_path):
    users, videos = _fixture_users(4), build_fixture_video_records(40)
    result = generate_action_log_batch(_request(tmp_path), users, videos, RuleBasedActionLogGenerator())
    lo = _FIXED_END - timedelta(days=31)
    for event in result.batch.events:
        assert lo <= event.event_timestamp <= _FIXED_END


def test_per_user_daily_cap_respected(tmp_path):
    users, videos = _fixture_users(1), build_fixture_video_records(40)
    result = generate_action_log_batch(
        _request(tmp_path, candidates_per_user=30, max_events_per_user_per_day=5, history_days=30),
        users, videos, RuleBasedActionLogGenerator(),
    )
    per_day: dict = {}
    for event in result.batch.events:
        key = (event.user_id, event.event_timestamp.date())
        per_day[key] = per_day.get(key, 0) + 1
    assert max(per_day.values()) <= 5


def test_parquet_matches_events(tmp_path):
    users, videos = _fixture_users(3), build_fixture_video_records(40)
    result = generate_action_log_batch(_request(tmp_path), users, videos, RuleBasedActionLogGenerator())
    table = pq.read_table(tmp_path / "e.parquet")
    assert table.num_rows == result.summary["total_events"]
    assert set(table.column_names) >= {"event_id", "event_timestamp", "clicked", "exposure_type"}
    warehouse = [json.loads(line) for line in (tmp_path / "e.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(warehouse) == result.summary["total_events"]


def test_user_isolation_quarantines_bad_row(tmp_path):
    class _OneBadUserGen(RuleBasedActionLogGenerator):
        def generate(self, virtual_user, videos):
            if virtual_user["user_id"] == "vu_0001":
                return "{not valid json"
            return super().generate(virtual_user, videos)

    users, videos = _fixture_users(6), build_fixture_video_records(40)
    result = generate_action_log_batch(_request(tmp_path), users, videos, _OneBadUserGen())
    assert result.summary["quarantined_users"] == 1
    assert result.summary["invalid_json"] == 1
    q_lines = (tmp_path / "q.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(q_lines[0])["error_type"] == "invalid_json"


def test_total_failure_raises_and_writes_quarantine(tmp_path):
    class _AllBadGen(RuleBasedActionLogGenerator):
        def generate(self, virtual_user, videos):
            return "{not valid json"

    users, videos = _fixture_users(6), build_fixture_video_records(40)
    with pytest.raises(ActionLogGenerationError):
        generate_action_log_batch(_request(tmp_path), users, videos, _AllBadGen())
    assert len((tmp_path / "q.jsonl").read_text(encoding="utf-8").splitlines()) == 6
    assert not (tmp_path / "e.parquet").exists()


def test_eventlog_rejects_click0_with_watch_or_like():
    now = datetime(2026, 7, 1, tzinfo=UTC)
    EventLog(event_id="e", event_timestamp=now, user_id="u", video_id="v", clicked=0, watch_time_sec=0, liked=0)
    with pytest.raises(ValidationError):
        EventLog(event_id="e", event_timestamp=now, user_id="u", video_id="v", clicked=0, watch_time_sec=5, liked=0)
    with pytest.raises(ValidationError):
        EventLog(event_id="e", event_timestamp=now, user_id="u", video_id="v", clicked=0, watch_time_sec=0, liked=1)


def test_video_source_helpers():
    assert _parse_tags("LCK, 롤, None") == ["LCK", "롤"]
    assert _parse_tags("None") == []
    assert _parse_tags(None) == []
    assert _parse_tags(["a", " b ", ""]) == ["a", "b"]
    assert nominal_duration_sec("abc") == nominal_duration_sec("abc")  # 결정론적
    assert 60 <= nominal_duration_sec("abc") <= 900
