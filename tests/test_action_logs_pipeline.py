import json
import random
from datetime import UTC, datetime, timedelta

import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

from autoresearch.action_logs.candidate import build_candidates
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


def test_end_to_end_long_event_stream(tmp_path):
    users, videos = _fixture_users(6), build_fixture_video_records(40)
    result = generate_action_log_batch(_request(tmp_path), users, videos, RuleBasedActionLogGenerator())
    events = result.batch.events

    impressions = [e for e in events if e.event_type == "impression"]
    clicks = [e for e in events if e.event_type == "click"]
    views = [e for e in events if e.event_type == "view"]
    likes = [e for e in events if e.event_type == "like"]

    assert len(impressions) == 6 * 20  # 유저당 후보 20 (pool 40)
    assert result.summary["impressions"] == 6 * 20
    assert len(clicks) == round(0.05 * len(impressions))  # 전역 CTR 정규화(여기선 5%)
    assert result.summary["clicks"] == len(clicks)
    assert len(views) == len(clicks)  # 클릭 선정분마다 view 1행
    assert len(likes) <= len(clicks)  # like는 would_like일 때만
    # view만 watch_time_sec>0, 그 외 event_type은 None
    for e in events:
        if e.event_type == "view":
            assert e.watch_time_sec is not None and e.watch_time_sec > 0
        else:
            assert e.watch_time_sec is None
        assert e.rank is None and e.source == "historical"
    # 클릭 선정 video는 impression·click·view를 모두 가진다
    clicked_keys = {(e.user_id, e.video_id) for e in clicks}
    imp_keys = {(e.user_id, e.video_id) for e in impressions}
    view_keys = {(e.user_id, e.video_id) for e in views}
    assert clicked_keys <= imp_keys and clicked_keys == view_keys
    assert (tmp_path / "e.parquet").exists()
    assert result.summary["quarantined_users"] == 0


def test_click_session_timestamps_are_monotonic(tmp_path):
    users, videos = _fixture_users(6), build_fixture_video_records(40)
    result = generate_action_log_batch(_request(tmp_path), users, videos, RuleBasedActionLogGenerator())
    # (user, video)별로 event_type 순서대로 timestamp가 단조 증가하는지
    by_key: dict = {}
    for e in result.batch.events:
        by_key.setdefault((e.user_id, e.video_id), []).append(e)
    order = {"impression": 0, "click": 1, "view": 2, "like": 3}
    for group in by_key.values():
        group.sort(key=lambda e: order[e.event_type])
        ts = [e.event_timestamp for e in group]
        assert all(a < b for a, b in zip(ts, ts[1:])), f"non-strict session order: {ts}"


def test_clicked_indices_selects_highest_propensity():
    from autoresearch.action_logs.pipeline import _clicked_indices
    from autoresearch.action_logs.schema import ImpressionDraft

    drafts = [
        ImpressionDraft(
            user_id="u",
            video_id=f"v{i}",
            click_propensity=p,
            watch_fraction=0.5,
            would_like=False,
            duration_sec=100,
        )
        for i, p in enumerate([0.1, 0.9, 0.5, 0.8, 0.2])
    ]
    chosen = _clicked_indices(drafts, target_ctr=0.4)  # round(0.4*5)=2
    assert chosen == {1, 3}  # the 0.9 and 0.8 propensity drafts


def test_timestamps_within_history_window(tmp_path):
    users, videos = _fixture_users(4), build_fixture_video_records(40)
    result = generate_action_log_batch(_request(tmp_path), users, videos, RuleBasedActionLogGenerator())
    lo = _FIXED_END - timedelta(days=30)
    for event in result.batch.events:
        assert lo <= event.event_timestamp <= _FIXED_END


def test_per_user_daily_impression_cap_respected(tmp_path):
    users, videos = _fixture_users(1), build_fixture_video_records(40)
    result = generate_action_log_batch(
        _request(tmp_path, candidates_per_user=30, max_events_per_user_per_day=5, history_days=30),
        users, videos, RuleBasedActionLogGenerator(),
    )
    per_day: dict = {}
    for event in result.batch.events:
        if event.event_type != "impression":
            continue  # 상한은 impression 기준
        key = (event.user_id, event.event_timestamp.date())
        per_day[key] = per_day.get(key, 0) + 1
    assert max(per_day.values()) <= 5


def test_parquet_matches_events(tmp_path):
    users, videos = _fixture_users(3), build_fixture_video_records(40)
    result = generate_action_log_batch(_request(tmp_path), users, videos, RuleBasedActionLogGenerator())
    table = pq.read_table(tmp_path / "e.parquet")
    assert table.num_rows == result.summary["total_events"]
    assert set(table.column_names) >= {"event_id", "event_timestamp", "event_type", "watch_time_sec"}
    assert "clicked" not in table.column_names and "exposure_type" not in table.column_names
    warehouse = [json.loads(line) for line in (tmp_path / "e.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(warehouse) == result.summary["total_events"]
    assert set(warehouse[0]) == {
        "event_id", "event_timestamp", "user_id", "event_type",
        "video_id", "watch_time_sec", "rank", "source",
    }


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


def test_eventlog_watch_time_only_for_view():
    now = datetime(2026, 7, 1, tzinfo=UTC)
    # view는 watch_time_sec 필수(>=0)
    ev = EventLog(event_id="e", event_timestamp=now, user_id="u",
                  event_type="view", video_id="v", watch_time_sec=42)
    assert ev.watch_time_sec == 42 and ev.rank is None and ev.source == "historical"
    # impression/click/like는 watch_time_sec=None (기본값)
    for et in ("impression", "click", "like"):
        assert EventLog(event_id="e", event_timestamp=now, user_id="u",
                        event_type=et, video_id="v").watch_time_sec is None
    # view인데 watch_time_sec 누락 -> 거부
    with pytest.raises(ValidationError):
        EventLog(event_id="e", event_timestamp=now, user_id="u",
                 event_type="view", video_id="v")
    # 비-view인데 watch_time_sec 채움 -> 거부
    with pytest.raises(ValidationError):
        EventLog(event_id="e", event_timestamp=now, user_id="u",
                 event_type="impression", video_id="v", watch_time_sec=5)


def test_batch_summary_ctr_from_impression_and_click_rows():
    from autoresearch.action_logs.schema import EventLogBatch
    now = datetime(2026, 7, 1, tzinfo=UTC)

    def _ev(et, wt=None):
        return EventLog(event_id="e", event_timestamp=now, user_id="u",
                        event_type=et, video_id="v", watch_time_sec=wt)

    events = [_ev("impression"), _ev("impression"), _ev("click"), _ev("view", 10), _ev("like")]
    batch = EventLogBatch(
        schema_version="s", prompt_version="p",
        request=EventGenerationRequest(), events=events,
    )
    s = batch.summary
    assert s["impressions"] == 2 and s["clicks"] == 1
    assert s["total_events"] == 5
    assert s["ctr"] == round(1 / 2, 4)


def test_video_source_helpers():
    assert _parse_tags("LCK, 롤, None") == ["LCK", "롤"]
    assert _parse_tags("None") == []
    assert _parse_tags(None) == []
    assert _parse_tags(["a", " b ", ""]) == ["a", "b"]
    assert nominal_duration_sec("abc") == nominal_duration_sec("abc")  # 결정론적
    assert 60 <= nominal_duration_sec("abc") <= 900


def test_build_candidates_returns_video_dicts_no_exposure_label():
    users = _fixture_users(1)
    videos = build_fixture_video_records(40)
    got = build_candidates(users[0], videos, candidates_per_user=20,
                           exploration_ratio=0.2, rng=random.Random(1))
    assert len(got) == 20
    assert all(isinstance(v, dict) and "video_id" in v for v in got)  # tuple 아님
    assert len({v["video_id"] for v in got}) == 20  # dedup
    # pool보다 큰 요청은 pool 크기로 클램프
    assert len(build_candidates(users[0], videos[:5], 20, 0.2, random.Random(1))) == 5
    assert build_candidates(users[0], [], 20, 0.2, random.Random(1)) == []


def test_rulebased_judgments_have_no_search_keyword():
    users = _fixture_users(1)
    videos = build_fixture_video_records(6)
    raw = RuleBasedActionLogGenerator().generate(users[0], videos)
    data = json.loads(raw)
    assert len(data["judgments"]) == 6
    for j in data["judgments"]:
        assert set(j) == {"video_id", "click_propensity", "watch_fraction", "would_like"}
        assert 0.0 <= j["click_propensity"] <= 1.0
        assert isinstance(j["would_like"], bool)
