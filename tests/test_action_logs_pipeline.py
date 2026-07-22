import json
import logging
import random
from datetime import UTC, datetime, timedelta
from threading import Event

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from pydantic import ValidationError

import autoresearch.action_logs.pipeline as pipeline_module
from autoresearch.action_logs.candidate import build_candidates
from autoresearch.action_logs.llm_generator import RuleBasedActionLogGenerator
from autoresearch.action_logs.pipeline import (
    ACTION_LOG_DRAFT_PARQUET_SCHEMA,
    ActionLogGenerationError,
    ExposureMetadata,
    _build_user_drafts,
    attach_exposure_tags,
    expand_action_log_drafts,
    generate_action_log_batch,
    generate_action_log_drafts,
    read_action_log_draft_parquet,
    select_clicks_per_slate,
    write_action_log_draft_parquet,
)
from autoresearch.action_logs.schema import (
    EventGenerationRequest,
    EventLog,
    ImpressionDraft,
)
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
    # per-slate 커트라인(기본 click_threshold=0.55): 유저당 클릭은 최대 1건이며,
    # 슬레이트 최고 click_propensity가 커트라인 이상일 때만 클릭이 발생한다.
    clicks_by_user: dict[str, int] = {}
    for c in clicks:
        clicks_by_user[c.user_id] = clicks_by_user.get(c.user_id, 0) + 1
    assert all(count == 1 for count in clicks_by_user.values())
    assert len(clicks) <= len(users)
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


def test_impression_headroom_covers_max_session_span():
    # window 불변식(모든 이벤트 <= history_end)이 _MAX_DURATION과 결합돼 있음을 명시적으로 잠근다.
    # _MAX_DURATION을 올리면 _MAX_SESSION_SPAN_SEC가 커지고 _MIN_IMPRESSION_HOURS도 따라
    # 커져야 하며, 그렇지 않으면 클릭 세션 후속 이벤트가 history_end를 넘을 수 있다.
    from autoresearch.action_logs.pipeline import (
        _MAX_SESSION_SPAN_SEC,
        _MIN_IMPRESSION_HOURS,
    )

    assert _MIN_IMPRESSION_HOURS >= 1
    assert _MIN_IMPRESSION_HOURS * 3600 >= _MAX_SESSION_SPAN_SEC


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
        "policy", "ctr_score", "is_exploration", "policy_version",
        "exposure_source",
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


@pytest.mark.parametrize(
    ("first_response", "expected_error_type"),
    [
        ("{not valid json", "invalid_json"),
        (json.dumps({"j": [[0, 0.1, 0.2]]}), "schema_fail"),
    ],
)
def test_openrouter_style_generator_repairs_response_validation_once(
    tmp_path,
    first_response,
    expected_error_type,
):
    class _RepairingGenerator:
        model_name = "repairing-generator"

        def __init__(self):
            self.generate_calls = 0
            self.retry_calls = []

        def generate(self, virtual_user, videos):
            self.generate_calls += 1
            return first_response

        def generate_schema_retry(self, virtual_user, videos, *, error_type):
            self.retry_calls.append(error_type)
            return RuleBasedActionLogGenerator().generate(virtual_user, videos)

    generator = _RepairingGenerator()
    result = generate_action_log_batch(
        _request(tmp_path, candidates_per_user=4),
        _fixture_users(1),
        build_fixture_video_records(4),
        generator,
    )

    assert generator.generate_calls == 1
    assert generator.retry_calls == [expected_error_type]
    assert result.summary["quarantined_users"] == 0
    assert result.summary["impressions"] == 4


def test_schema_retry_stays_in_worker_and_does_not_block_next_work_submission(
    tmp_path,
):
    retry_started = Event()
    third_work_started = Event()

    class _CoordinatedGenerator:
        model_name = "coordinated-generator"

        def generate(self, virtual_user, videos):
            user_id = virtual_user["user_id"]
            if user_id == "vu_0000":
                return "{first invalid"
            if user_id == "vu_0001":
                assert retry_started.wait(timeout=2.0)
            if user_id == "vu_0002":
                third_work_started.set()
            return RuleBasedActionLogGenerator().generate(virtual_user, videos)

        def generate_schema_retry(self, virtual_user, videos, *, error_type):
            assert virtual_user["user_id"] == "vu_0000"
            assert error_type == "invalid_json"
            retry_started.set()
            assert third_work_started.wait(timeout=2.0)
            return RuleBasedActionLogGenerator().generate(virtual_user, videos)

    result = generate_action_log_drafts(
        _request(
            tmp_path,
            candidates_per_user=1,
            chunk_size=0,
            max_concurrency=2,
        ),
        _fixture_users(3),
        build_fixture_video_records(3),
        _CoordinatedGenerator(),
    )

    assert third_work_started.is_set()
    assert len(result.drafts) == 3
    assert result.quarantine == []


def test_schema_retry_timings_separate_request_and_parse(monkeypatch):
    class _RepairingGenerator:
        model_name = "timed-repairing-generator"

        def generate(self, virtual_user, videos):
            return "{first invalid"

        def generate_schema_retry(self, virtual_user, videos, *, error_type):
            return RuleBasedActionLogGenerator().generate(virtual_user, videos)

    clock = iter(
        [
            0.000,  # worker start
            0.000,  # initial request start
            0.010,  # initial request end
            0.010,  # initial parse start
            0.012,  # initial parse end
            0.012,  # retry request start
            0.032,  # retry request end
            0.032,  # retry parse start
            0.037,  # retry parse end
        ]
    )
    monkeypatch.setattr(pipeline_module, "monotonic", lambda: next(clock))
    virtual_user = _fixture_users(1)[0]
    item = pipeline_module._ActionLogWorkItem(
        work_id="work_00000000",
        user_id=virtual_user["user_id"],
        virtual_user=virtual_user,
        candidates=build_fixture_video_records(1),
    )

    result = pipeline_module._generate_action_log_work(
        _RepairingGenerator(),
        item,
        work_sequence=0,
        submitted_at=0.0,
        shard_index=None,
        detailed_telemetry=True,
    )

    assert result.drafts is not None
    assert result.error is None
    assert result.request_elapsed_ms == pytest.approx(30.0)
    assert result.parse_elapsed_ms == pytest.approx(7.0)


def test_schema_retry_api_error_preserves_error_and_initial_raw_response(tmp_path):
    class _RetryApiErrorGenerator:
        model_name = "retry-api-error-generator"

        def generate(self, virtual_user, videos):
            return "{first invalid"

        def generate_schema_retry(self, virtual_user, videos, *, error_type):
            raise RuntimeError("retry transport unavailable")

    result = generate_action_log_drafts(
        _request(
            tmp_path,
            candidates_per_user=1,
            max_quarantine_ratio=1.0,
        ),
        _fixture_users(1),
        build_fixture_video_records(1),
        _RetryApiErrorGenerator(),
    )

    assert result.summary["api_error"] == 1
    assert result.summary["invalid_json"] == 0
    assert result.quarantine[0].raw_llm_response == "{first invalid"
    assert result.quarantine[0].error_message == "retry transport unavailable"


def test_unexpected_worker_error_is_not_disguised_as_api_error(
    tmp_path,
    monkeypatch,
):
    def _raise_internal_error(virtual_user, candidates, raw_text):
        raise RuntimeError("unexpected parser bug")

    monkeypatch.setattr(
        pipeline_module,
        "_try_build_user_drafts",
        _raise_internal_error,
    )

    with pytest.raises(RuntimeError, match="unexpected parser bug"):
        generate_action_log_drafts(
            _request(tmp_path, candidates_per_user=1),
            _fixture_users(1),
            build_fixture_video_records(1),
            RuleBasedActionLogGenerator(),
        )



def test_schema_retry_final_failure_is_quarantined(tmp_path):
    class _AlwaysInvalidGenerator:
        model_name = "always-invalid-generator"

        def __init__(self):
            self.retry_calls = 0

        def generate(self, virtual_user, videos):
            return "{first invalid"

        def generate_schema_retry(self, virtual_user, videos, *, error_type):
            self.retry_calls += 1
            return "{retry invalid"

    generator = _AlwaysInvalidGenerator()
    result = generate_action_log_batch(
        _request(
            tmp_path,
            candidates_per_user=4,
            max_quarantine_ratio=1.0,
        ),
        _fixture_users(1),
        build_fixture_video_records(4),
        generator,
    )

    assert generator.retry_calls == 1
    assert result.summary["invalid_json"] == 1
    quarantine = json.loads(
        (tmp_path / "q.jsonl").read_text(encoding="utf-8").splitlines()[0]
    )
    assert quarantine["raw_llm_response"] == "{retry invalid"


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


def test_event_generation_request_defaults_to_70_20_10_candidate_mix():
    req = EventGenerationRequest()

    assert req.personalized_ratio == 0.7
    assert req.popular_ratio == 0.2
    assert req.exploration_ratio == 0.1


def test_event_generation_request_accepts_candidate_ratio_sum_inside_tolerance():
    request = EventGenerationRequest(
        personalized_ratio=0.7000000005,
        popular_ratio=0.2,
        exploration_ratio=0.1,
    )

    assert request.personalized_ratio == 0.7000000005


@pytest.mark.parametrize(
    ("personalized", "popular", "exploration"),
    [
        (0.700000002, 0.2, 0.1),
        (0.6, 0.2, 0.1),
        (float("nan"), 0.2, 0.1),
        (float("inf"), 0.0, 0.0),
    ],
)
def test_event_generation_request_rejects_invalid_candidate_ratio_mix(
    personalized,
    popular,
    exploration,
):
    with pytest.raises(ValidationError):
        EventGenerationRequest(
            personalized_ratio=personalized,
            popular_ratio=popular,
            exploration_ratio=exploration,
        )


def test_build_candidates_includes_popular_slice_after_personalized_slice():
    user = {
        "user_id": "vu",
        "primary_categories": ["niche"],
        "interest_keywords": ["niche"],
    }
    videos = []
    for i in range(12):
        videos.append(
            {
                "video_id": f"personal_{i}",
                "title": f"niche match {i}",
                "description": "",
                "tags": [],
                "view_count": 100 - i,
            }
        )
    videos.extend(
        [
            {
                "video_id": "popular_a",
                "title": "broad hit",
                "description": "",
                "tags": [],
                "view_count": 10_000,
            },
            {
                "video_id": "popular_b",
                "title": "another broad hit",
                "description": "",
                "tags": [],
                "view_count": 9_000,
            },
            {
                "video_id": "tail",
                "title": "tail video",
                "description": "",
                "tags": [],
                "view_count": 1,
            },
        ]
    )

    got = build_candidates(
        user,
        videos,
        candidates_per_user=10,
        exploration_ratio=0.1,
        rng=random.Random(7),
        personalized_ratio=0.7,
        popular_ratio=0.2,
    )
    ids = {v["video_id"] for v in got}

    assert len(got) == 10
    assert "popular_a" in ids
    assert "popular_b" in ids
    assert len(ids) == 10


def test_build_candidates_fills_popular_slice_when_top_popular_overlap_personalized():
    user = {
        "user_id": "vu",
        "primary_categories": ["niche"],
        "interest_keywords": ["niche"],
    }
    videos = []
    for i in range(7):
        videos.append(
            {
                "video_id": f"popular_personalized_{i}",
                "title": f"niche popular match {i}",
                "description": "",
                "tags": [],
                "view_count": 10_000 - i,
            }
        )
    for i in range(5):
        videos.append(
            {
                "video_id": f"popular_broad_{i}",
                "title": f"broad popular {i}",
                "description": "",
                "tags": [],
                "view_count": 9_000 - i,
            }
        )

    got = build_candidates(
        user,
        videos,
        candidates_per_user=10,
        exploration_ratio=0.1,
        rng=random.Random(7),
        personalized_ratio=0.7,
        popular_ratio=0.2,
    )
    ids = {v["video_id"] for v in got}

    assert len(got) == 10
    assert {"popular_broad_0", "popular_broad_1"} <= ids


def test_rulebased_judgments_are_indexed_triples():
    users = _fixture_users(1)
    videos = build_fixture_video_records(6)
    raw = RuleBasedActionLogGenerator().generate(users[0], videos)
    data = json.loads(raw)
    # 인덱스 포맷: {"j": [[idx, cp, wf], ...]} — would_like·video_id 없음.
    assert set(data) == {"j"}
    assert len(data["j"]) == 6
    assert [entry[0] for entry in data["j"]] == list(range(6))  # 0..n-1
    for entry in data["j"]:
        assert len(entry) == 3
        idx, cp, wf = entry
        assert 0.0 <= cp <= 1.0
        assert 0.0 <= wf <= 1.0


def test_build_user_drafts_realigns_shuffled_indices():
    # LLM이 순서를 바꿔 반환해도 index로 재결합해 올바른 video_id에 매핑된다.
    vu = {"user_id": "vu_x"}
    videos = [{"video_id": f"vid_{i}"} for i in range(4)]
    shuffled = json.dumps({"j": [[2, 0.9, 0.9], [0, 0.1, 0.1], [3, 0.8, 0.7], [1, 0.2, 0.2]]})
    drafts = _build_user_drafts(vu, videos, shuffled)
    got = {d.video_id: (d.click_propensity, d.watch_fraction) for d in drafts}
    assert got["vid_0"] == (0.1, 0.1)
    assert got["vid_2"] == (0.9, 0.9)
    assert got["vid_3"] == (0.8, 0.7)


@pytest.mark.parametrize(
    "payload",
    [
        {"j": [[0, 0.1, 0.1], [1, 0.2, 0.2], [2, 0.3, 0.3]]},  # 개수 부족(n=4)
        {"j": [[0, 0.1, 0.1], [0, 0.2, 0.2], [2, 0.3, 0.3], [3, 0.4, 0.4]]},  # 중복 index
        {"j": [[0, 0.1, 0.1], [1, 0.2, 0.2], [2, 0.3, 0.3], [9, 0.4, 0.4]]},  # 범위 이탈
        {"j": [[0, 0.1], [1, 0.2, 0.2], [2, 0.3, 0.3], [3, 0.4, 0.4]]},  # 원소 길이 오류
    ],
)
def test_build_user_drafts_rejects_broken_index_sets(payload):
    vu = {"user_id": "vu_x"}
    videos = [{"video_id": f"vid_{i}"} for i in range(4)]
    with pytest.raises(ValueError):
        _build_user_drafts(vu, videos, json.dumps(payload))


def test_chunked_parallel_matches_single_call(tmp_path):
    # 청킹+병렬(chunk_size=8, workers=4)이 단일콜과 동일한 impression/click을 내고 결정론적.
    users, videos = _fixture_users(6), build_fixture_video_records(40)
    chunked = generate_action_log_batch(
        _request(tmp_path / "c", chunk_size=8, max_concurrency=4),
        users, videos, RuleBasedActionLogGenerator(),
    )
    single = generate_action_log_batch(
        _request(tmp_path / "s", chunk_size=0, max_concurrency=1),
        users, videos, RuleBasedActionLogGenerator(),
    )
    assert chunked.summary["impressions"] == single.summary["impressions"] == 6 * 20
    assert chunked.summary["clicks"] == single.summary["clicks"]
    imps = [e for e in chunked.batch.events if e.event_type == "impression"]
    assert imps[0].user_id == "vu_0000"  # 병렬이어도 원본 유저 순서 유지
    assert chunked.summary["quarantined_users"] == 0


def test_draft_progress_callback_reports_completed_chunks(tmp_path):
    users, videos = _fixture_users(2), build_fixture_video_records(8)
    snapshots = []

    result = generate_action_log_drafts(
        _request(tmp_path, candidates_per_user=4, chunk_size=2, max_concurrency=2),
        users,
        videos,
        RuleBasedActionLogGenerator(),
        progress_callback=snapshots.append,
    )

    assert result.total_work == 4
    completed = [snapshot.completed_chunks for snapshot in snapshots]
    assert completed[0] == 0
    assert completed[-1] == 4
    assert completed == sorted(set(completed))
    assert {snapshot.total_chunks for snapshot in snapshots} == {4}
    assert snapshots[-1].success_chunks == 4
    assert snapshots[-1].failed_chunks == 0
    assert snapshots[-1].quarantined_chunks == 0


def test_progress_snapshot_is_emitted_after_completed_batch_is_drained(
    tmp_path,
    monkeypatch,
    caplog,
):
    real_wait = pipeline_module.wait

    def _wait_for_current_batch(futures, *, return_when):
        return real_wait(futures)

    monkeypatch.setattr(pipeline_module, "wait", _wait_for_current_batch)
    snapshots = []

    with caplog.at_level(logging.INFO, logger="autoresearch.action_logs.pipeline"):
        result = generate_action_log_drafts(
            _request(
                tmp_path,
                candidates_per_user=4,
                chunk_size=2,
                max_concurrency=2,
            ),
            _fixture_users(2),
            build_fixture_video_records(8),
            RuleBasedActionLogGenerator(),
            progress_callback=snapshots.append,
        )

    assert result.total_work == 4
    assert [snapshot.completed_chunks for snapshot in snapshots] == [0, 2, 4]
    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.message.startswith("{")
    ]
    micro = [
        event
        for event in events
        if event["event"] == "action_log_micro_work_complete"
    ]
    assert len(micro) == 4
    assert [event["completed_work"] for event in micro] == [2, 2, 4, 4]
    assert all(
        event["completed_work"]
        + event["active_workers"]
        + event["pending_work"]
        == event["total_work"]
        for event in micro
    )


def test_draft_progress_callback_counts_quarantined_chunks(tmp_path):
    class _OneBadUserGen(RuleBasedActionLogGenerator):
        def generate(self, virtual_user, videos):
            if virtual_user["user_id"] == "vu_0000":
                return "{not valid json"
            return super().generate(virtual_user, videos)

    users, videos = _fixture_users(2), build_fixture_video_records(8)
    snapshots = []

    result = generate_action_log_drafts(
        _request(tmp_path, candidates_per_user=4, chunk_size=2, max_concurrency=2),
        users,
        videos,
        _OneBadUserGen(),
        progress_callback=snapshots.append,
    )

    assert result.total_work == 4
    assert len(result.quarantine) == 2
    assert snapshots[-1].completed_chunks == 4
    assert snapshots[-1].success_chunks == 2
    assert snapshots[-1].failed_chunks == 2
    assert snapshots[-1].quarantined_chunks == 2


def test_micro_work_structured_log_separates_pipeline_timings(tmp_path, caplog):
    checkpoint_rows = []

    def _checkpoint(work_id, work_order, drafts):
        checkpoint_rows.append((work_id, work_order, len(drafts)))

    def _progress(snapshot):
        return 3.25

    with caplog.at_level(logging.INFO, logger="autoresearch.action_logs.pipeline"):
        result = generate_action_log_drafts(
            _request(
                tmp_path,
                candidates_per_user=4,
                chunk_size=0,
                max_concurrency=1,
            ),
            _fixture_users(1),
            build_fixture_video_records(8),
            RuleBasedActionLogGenerator(),
            progress_callback=_progress,
            checkpoint_callback=_checkpoint,
            shard_index=3,
        )

    events = [
        json.loads(record.message)
        for record in caplog.records
        if record.message.startswith("{")
    ]
    micro = [
        event
        for event in events
        if event["event"] == "action_log_micro_work_complete"
    ]

    assert result.total_work == 1
    assert checkpoint_rows[0][2] == 4
    assert len(micro) == 1
    payload = micro[0]
    assert payload["shard_index"] == 3
    assert payload["work_sequence"] == 0
    assert payload["checkpoint_rows"] == 4
    assert payload["progress_write_elapsed_ms"] == 3.25
    assert payload["completed_work"] == payload["total_work"] == 1
    assert payload["failed_work"] == payload["active_workers"] == 0
    assert payload["pending_work"] == 0
    for field in (
        "queue_wait_ms",
        "request_elapsed_ms",
        "parse_elapsed_ms",
        "checkpoint_write_elapsed_ms",
        "submit_elapsed_ms",
        "total_elapsed_ms",
        "throughput_per_min",
        "latency_p50_ms",
        "latency_p95_ms",
        "eta_seconds",
    ):
        assert payload[field] >= 0
    serialized = json.dumps(events, ensure_ascii=False)
    assert "user_id" not in serialized
    assert "vu_0000" not in serialized


def test_load_video_records_accepts_youtube_collection_schema(tmp_path):
    path = tmp_path / "youtube.parquet"
    table = pa.Table.from_pylist(
        [
            {
                "video_id": "yt1",
                "video_title": "정규화 영상",
                "video_description": "설명",
                "video_tags": ["태그1", "태그2"],
                "video_view_count": 1234,
                "video_like_count": 55,
                "video_comment_count": 6,
                "channel_title": "채널명",
                "video_published_at": datetime(2026, 7, 1, tzinfo=UTC),
            }
        ]
    )
    pq.write_table(table, path)

    from autoresearch.action_logs.video_source import load_video_records

    records = load_video_records(path)

    assert records == [
        {
            "video_id": "yt1",
            "title": "정규화 영상",
            "description": "설명",
            "tags": ["태그1", "태그2"],
            "view_count": 1234,
            "like_count": 55,
            "comment_count": 6,
            "channel_name": "채널명",
            "published_at": "2026-07-01 00:00:00+00:00",
        }
    ]


def test_candidate_provider_overrides_default_selection(tmp_path):
    """candidate_provider 주입 시 build_candidates 대신 주입된 후보만 판정한다."""
    users, videos = _fixture_users(2), build_fixture_video_records(10)
    fixed = [videos[0], videos[1]]  # 항상 같은 2개만 노출

    def provider(virtual_user, user_rng):
        return list(fixed)

    result = generate_action_log_drafts(
        _request(tmp_path), users, videos, RuleBasedActionLogGenerator(),
        candidate_provider=provider,
    )
    judged_pairs = {(d.user_id, d.video_id) for d in result.drafts}
    expected_video_ids = {str(v["video_id"]) for v in fixed}
    assert {pair[1] for pair in judged_pairs} <= expected_video_ids
    assert len(result.drafts) == 2 * len(users)


def test_expand_events_tags_exposure_metadata_and_prefix():
    from autoresearch.action_logs.pipeline import (
        ExposureMetadata,
        _expand_events,
        normalize_clicks,
    )
    from autoresearch.action_logs.schema import SOURCE_ONLINE_SIMULATED, ImpressionDraft

    drafts = [
        ImpressionDraft(
            user_id="u1", video_id="v1", click_propensity=0.9,
            watch_fraction=0.5, would_like=False, duration_sec=100,
        ),
        ImpressionDraft(
            user_id="u1", video_id="v2", click_propensity=0.1,
            watch_fraction=0.5, would_like=False, duration_sec=100,
        ),
    ]
    clicked = normalize_clicks(drafts, target_ctr=0.5)  # 상위 1건 = v1
    assert clicked == {0}

    metadata = {
        ("u1", "v1"): ExposureMetadata(
            policy="model", rank=1, ctr_score=0.9,
            is_exploration=False, policy_version="run-x",
        ),
        ("u1", "v2"): ExposureMetadata(
            policy="model", rank=2, ctr_score=0.1,
            is_exploration=True, policy_version="run-x",
        ),
    }
    request = EventGenerationRequest(seed=7)
    events = _expand_events(
        drafts, clicked, request,
        metadata=metadata, source=SOURCE_ONLINE_SIMULATED, event_id_prefix="evt_m",
    )
    impressions = [e for e in events if e.event_type == "impression"]
    assert len(impressions) == 2
    assert all(e.source == "online_simulated" for e in events)
    assert all(e.event_id.startswith("evt_m_") for e in events)
    v1_imp = next(e for e in impressions if e.video_id == "v1")
    assert (v1_imp.policy, v1_imp.rank, v1_imp.ctr_score) == ("model", 1, 0.9)
    v1_click = next(e for e in events if e.event_type == "click")
    assert v1_click.policy == "model"  # 세션 행에도 태깅
    v2_imp = next(e for e in impressions if e.video_id == "v2")
    assert v2_imp.is_exploration is True


def test_expand_events_without_metadata_is_unchanged():
    from autoresearch.action_logs.pipeline import _expand_events, normalize_clicks
    from autoresearch.action_logs.schema import ImpressionDraft

    drafts = [
        ImpressionDraft(
            user_id="u1", video_id="v1", click_propensity=0.9,
            watch_fraction=0.5, would_like=False, duration_sec=100,
        ),
    ]
    events = _expand_events(drafts, normalize_clicks(drafts, 0.0), EventGenerationRequest(seed=7))
    assert events[0].event_id == "evt_00000000"
    assert events[0].source == "historical"
    assert events[0].policy is None


def _tagged_draft(**overrides) -> ImpressionDraft:
    base = dict(
        user_id="u1", video_id="v1", click_propensity=0.9,
        watch_fraction=0.4, would_like=False, duration_sec=100,
        exposure_source="model", exposure_rank=3, exposure_ctr_score=0.7,
        policy_version="run-a",
    )
    base.update(overrides)
    return ImpressionDraft(**base)


def test_draft_exposure_tags_roundtrip_parquet(tmp_path):
    drafts = [
        _tagged_draft(),
        _tagged_draft(video_id="v2", exposure_source="random",
                      exposure_rank=9, exposure_ctr_score=None),
    ]
    path = tmp_path / "drafts.parquet"
    write_action_log_draft_parquet(drafts, path)
    restored = read_action_log_draft_parquet(path)
    assert [d.exposure_source for d in restored] == ["model", "random"]
    assert restored[0].exposure_rank == 3 and restored[0].policy_version == "run-a"


def test_legacy_draft_parquet_without_tag_columns_reads_untagged(tmp_path):
    legacy_fields = [
        f for f in ACTION_LOG_DRAFT_PARQUET_SCHEMA
        if f.name not in ("exposure_source", "exposure_rank",
                          "exposure_ctr_score", "policy_version")
    ]
    row = {"user_id": "u1", "video_id": "v1", "click_propensity": 0.9,
           "watch_fraction": 0.4, "would_like": False, "duration_sec": 100}
    path = tmp_path / "legacy.parquet"
    pq.write_table(pa.Table.from_pylist([row], schema=pa.schema(legacy_fields)), path)
    restored = read_action_log_draft_parquet(path)
    assert restored[0].exposure_source is None


def test_attach_exposure_tags_leaves_unmapped_drafts_untagged():
    metadata = {
        ("u1", "v1"): ExposureMetadata(
            policy="model", rank=3, ctr_score=0.7, is_exploration=False,
            policy_version="run-a", exposure_source="model",
        )
    }
    plain = _tagged_draft(exposure_source=None, exposure_rank=None,
                          exposure_ctr_score=None, policy_version=None)
    other = _tagged_draft(video_id="vX", exposure_source=None, exposure_rank=None,
                          exposure_ctr_score=None, policy_version=None)
    tagged = attach_exposure_tags([plain, other], metadata)
    assert tagged[0].exposure_source == "model" and tagged[0].exposure_rank == 3
    assert tagged[1].exposure_source is None


def test_expand_events_joins_tags_from_draft_fallback(tmp_path):
    request = _request(tmp_path)
    drafts = [_tagged_draft(), _tagged_draft(video_id="v2", exposure_source="random",
                                             exposure_rank=2, exposure_ctr_score=None)]
    result = expand_action_log_drafts(request, drafts, [])
    impressions = [e for e in result.batch.events if e.event_type == "impression"]
    by_video = {e.video_id: e for e in impressions}
    assert by_video["v1"].exposure_source == "model"
    assert by_video["v1"].policy == "model" and by_video["v1"].rank == 3
    assert by_video["v1"].ctr_score == 0.7 and by_video["v1"].policy_version == "run-a"
    assert by_video["v2"].is_exploration is True


def test_batch_attaches_provider_exposure_tags(tmp_path):
    users, videos = _fixture_users(2), build_fixture_video_records(10)
    metadata: dict[tuple[str, str], ExposureMetadata] = {}

    def provider(virtual_user: dict, user_rng) -> list[dict]:
        picked = videos[:3]
        for position, video in enumerate(picked, start=1):
            metadata[(virtual_user["user_id"], str(video["video_id"]))] = (
                ExposureMetadata(
                    policy="model", rank=position, ctr_score=0.5,
                    is_exploration=False, policy_version="run-a",
                    exposure_source="model",
                )
            )
        return picked

    result = generate_action_log_batch(
        _request(tmp_path), users, videos, RuleBasedActionLogGenerator(),
        candidate_provider=provider, exposure_metadata=metadata,
    )
    impressions = [e for e in result.batch.events if e.event_type == "impression"]
    assert impressions and all(e.exposure_source == "model" for e in impressions)
    assert all(e.policy_version == "run-a" for e in impressions)


def _draft(user_id: str, video_id: str, cp: float) -> ImpressionDraft:
    return ImpressionDraft(
        user_id=user_id,
        video_id=video_id,
        click_propensity=cp,
        watch_fraction=0.5,
        would_like=False,
        duration_sec=100,
    )


def test_select_clicks_one_top_per_user_above_threshold() -> None:
    drafts = [
        _draft("u1", "a", 0.30),
        _draft("u1", "b", 0.80),  # u1 최고 → 클릭
        _draft("u2", "c", 0.40),  # u2 최고지만 커트라인 미만 → 클릭 없음
        _draft("u2", "d", 0.20),
    ]
    assert select_clicks_per_slate(drafts, 0.55) == {1}


def test_select_clicks_none_when_all_below_threshold() -> None:
    drafts = [_draft("u1", "a", 0.10), _draft("u1", "b", 0.20)]
    assert select_clicks_per_slate(drafts, 0.55) == set()


def test_select_clicks_threshold_is_inclusive() -> None:
    drafts = [_draft("u1", "a", 0.55)]
    assert select_clicks_per_slate(drafts, 0.55) == {0}


def test_select_clicks_tiebreak_is_deterministic_by_video_id() -> None:
    drafts = [_draft("u1", "b", 0.80), _draft("u1", "a", 0.80)]
    # 동점이면 video_id 작은 "a"(index 1)가 선택된다.
    assert select_clicks_per_slate(drafts, 0.55) == {1}


def test_select_clicks_handles_empty() -> None:
    assert select_clicks_per_slate([], 0.55) == set()


def test_expand_uses_per_slate_click_threshold() -> None:
    request = EventGenerationRequest(click_threshold=0.55)
    drafts = [
        _draft("u1", "a", 0.80),  # 클릭
        _draft("u1", "b", 0.30),
        _draft("u2", "c", 0.40),  # 커트라인 미만 → 클릭 없음
    ]
    result = expand_action_log_drafts(request, drafts)
    clicks = [e for e in result.batch.events if e.event_type == "click"]
    assert {c.video_id for c in clicks} == {"a"}


def test_event_generation_request_defaults_click_threshold() -> None:
    assert EventGenerationRequest().click_threshold == 0.55
