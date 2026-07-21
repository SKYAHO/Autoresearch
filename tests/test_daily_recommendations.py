"""일일 추천 배치 헬퍼 단위 테스트 — 실 BQ 미접속(fake client)."""

import json
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

import src.pipeline.daily_recommendations as daily
from src.pipeline.daily_recommendations import (
    RECOMMENDATIONS_SCHEMA,
    ensure_output_table,
    parse_iso8601_duration,
    run_batch,
    to_recommendation_rows,
    write_partition,
)
from src.serving.model_loader import ResolvedModel
from src.serving.schemas import RerankedVideo
from src.serving.service import Reranker

_GENERATED_AT = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


def _rows(ranked):
    return to_recommendation_rows(
        "vu_0001",
        ranked,
        dt=date(2026, 7, 21),
        events_dt=date(2026, 7, 21),
        model_run_id="run-abc",
        model_version="3",
        generated_at=_GENERATED_AT,
    )


def test_parse_iso8601_duration():
    assert parse_iso8601_duration("PT4M29S") == 269
    assert parse_iso8601_duration("PT1H2M3S") == 3723
    assert parse_iso8601_duration(None) == 0
    assert parse_iso8601_duration("not-a-duration") == 0
    assert parse_iso8601_duration("PT1Mgarbage") == 0


def test_rows_rank_descending_with_video_id_tiebreak():
    ranked = [
        RerankedVideo(video_id="vB", ctr_score=0.9),
        RerankedVideo(video_id="vC", ctr_score=0.5),
        RerankedVideo(video_id="vA", ctr_score=0.9),  # vB와 동점 → video_id 오름차순으로 앞
    ]
    rows = _rows(ranked)
    assert [(r["rank"], r["video_id"]) for r in rows] == [(1, "vA"), (2, "vB"), (3, "vC")]
    first = rows[0]
    assert first["user_id"] == "vu_0001"
    assert first["dt"] == date(2026, 7, 21)
    assert first["model_run_id"] == "run-abc"
    assert first["model_version"] == "3"
    assert first["generated_at"] == _GENERATED_AT


class _FakeLoadJob:
    def result(self):
        return None


class _FakeClient:
    def __init__(self):
        self.created = []
        self.loads = []
        self.partitions = {}

    def create_table(self, table, exists_ok=False):
        self.created.append((table, exists_ok))

    def load_table_from_dataframe(self, frame, destination, job_config=None):
        self.loads.append((frame.copy(), destination, job_config))
        if job_config.write_disposition == "WRITE_TRUNCATE":
            self.partitions[destination] = frame.copy()
        return _FakeLoadJob()


def test_ensure_output_table_creates_partitioned_table_exists_ok():
    client = _FakeClient()
    ensure_output_table(client, "proj.ds.user_recommendations")
    (table, exists_ok), = client.created
    assert exists_ok is True
    assert table.time_partitioning.field == "dt"
    assert [f.name for f in table.schema] == [f.name for f in RECOMMENDATIONS_SCHEMA]


def test_write_partition_is_idempotent_by_truncate_decorator():
    client = _FakeClient()
    frame = pd.DataFrame({"user_id": ["u1"], "video_id": ["v1"]})
    for _ in range(2):  # 같은 dt 2회 실행 = 같은 파티션 대상 + TRUNCATE → 중복 불가능
        write_partition(client, "proj.ds.user_recommendations", frame, date(2026, 7, 21))
    destinations = [dest for _, dest, _ in client.loads]
    dispositions = [cfg.write_disposition for _, _, cfg in client.loads]
    assert destinations == ["proj.ds.user_recommendations$20260721"] * 2
    assert dispositions == ["WRITE_TRUNCATE"] * 2
    assert len(client.partitions["proj.ds.user_recommendations$20260721"]) == 1


class _EverythingHalfModel:
    """모든 후보에 0.5를 주는 stub — 채점 경로만 검증한다."""

    def predict_proba(self, features):
        return np.column_stack([np.full(len(features), 0.5), np.full(len(features), 0.5)])


def _stub_resolved() -> ResolvedModel:
    feature_columns = (
        "age_group", "occupation", "historical_category_affinity",
        "recent_click_count_7d", "recent_watch_time_7d", "recent_like_count_7d",
        "category_id", "duration_sec", "view_count", "like_ratio",
        "comment_ratio", "days_since_upload", "historical_category_match",
        "preferred_category_match", "topic_similarity",
    )
    reranker = Reranker(
        model=_EverythingHalfModel(),
        feature_columns=feature_columns,
        categorical_categories={},
    )
    return ResolvedModel(reranker=reranker, run_id="run-e2e", model_version="9")


def _videos_raw(n: int = 5) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "video_id": [f"v{i}" for i in range(n)],
            "categoryId": ["Gaming"] * n,
            "duration": [100 + i for i in range(n)],
            "viewCount": [1000] * n,
            "likeCount": [10] * n,
            "commentCount": [1] * n,
            "publishedAt": ["2026-07-01"] * n,
        }
    )


def _personas(users: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "uuid": users,
            "age": [25] * len(users),
            "occupation": ["student"] * len(users),
            "hobbies_and_interests_list": ['["게임"]'] * len(users),
            "hobbies_and_interests": ["게임"] * len(users),
        }
    )


def _empty_events() -> pd.DataFrame:
    frame = pd.DataFrame(
        columns=["event_id", "user_id", "video_id", "timestamp", "clicked", "liked", "watch_time_sec"]
    )
    return frame.astype(
        {"event_id": "string", "user_id": "string", "video_id": "string",
         "timestamp": "string", "clicked": "Int64", "liked": "Int64", "watch_time_sec": "Int64"}
    )


def test_run_batch_scores_all_users_and_writes_one_partition():
    client = _FakeClient()
    report = run_batch(
        candidate_dt=date(2026, 7, 21),
        events_dt=date(2026, 7, 21),
        bq_client=client,
        resolved=_stub_resolved(),
        videos_raw=_videos_raw(),
        personas=_personas(["u1", "u2"]),
        events=_empty_events(),
    )
    assert (report["users"], report["skipped_users"], report["rows"]) == (2, 0, 10)
    assert report["model_run_id"] == "run-e2e" and report["model_version"] == "9"
    (frame, destination, cfg), = client.loads
    assert destination.endswith("$20260721")
    assert cfg.write_disposition == "WRITE_TRUNCATE"
    assert len(frame) == 10  # 유저 2 × 후보 5
    assert set(frame["rank"]) == {1, 2, 3, 4, 5}


def test_run_batch_dry_run_writes_nothing():
    client = _FakeClient()
    report = run_batch(
        candidate_dt=date(2026, 7, 21),
        events_dt=date(2026, 7, 21),
        dry_run=True,
        bq_client=client,
        resolved=_stub_resolved(),
        videos_raw=_videos_raw(),
        personas=_personas(["u1"]),
        events=_empty_events(),
    )
    assert report["dry_run"] is True and report["rows"] == 5
    assert client.loads == []


def test_run_batch_rejects_empty_candidate_partition():
    client = _FakeClient()
    with pytest.raises(RuntimeError, match="No candidates"):
        run_batch(
            candidate_dt=date(2026, 7, 21),
            events_dt=date(2026, 7, 21),
            bq_client=client,
            resolved=_stub_resolved(),
            videos_raw=pd.DataFrame(),
            personas=_personas(["u1"]),
            events=_empty_events(),
        )
    assert client.loads == []


def test_run_batch_loads_exactly_one_events_partition(monkeypatch):
    calls = []

    def _load_events(start_date, end_date):
        calls.append((start_date, end_date))
        return pd.DataFrame()

    monkeypatch.setattr(daily, "load_events_from_bigquery", _load_events)
    monkeypatch.setattr(daily, "derive_wide_events", lambda frame: _empty_events())
    run_batch(
        candidate_dt=date(2026, 7, 21),
        events_dt=date(2026, 7, 20),
        dry_run=True,
        bq_client=_FakeClient(),
        resolved=_stub_resolved(),
        videos_raw=_videos_raw(),
        personas=_personas(["u1"]),
    )
    assert calls == [("2026-07-20", "2026-07-20")]


def test_run_batch_fails_without_write_when_skip_ratio_exceeded(monkeypatch):
    # u2의 조립 실패를 유저 단위로 격리하되 1/2 > 0.4이면 전체 적재를 중단한다.
    client = _FakeClient()
    original = daily.build_pool_feature_frame

    def _build_or_fail(*, user_id, **kwargs):
        if user_id == "u2":
            raise KeyError("broken persona")
        return original(user_id=user_id, **kwargs)

    monkeypatch.setattr(daily, "build_pool_feature_frame", _build_or_fail)

    with pytest.raises(RuntimeError):
        run_batch(
            candidate_dt=date(2026, 7, 21),
            events_dt=date(2026, 7, 21),
            max_skip_ratio=0.4,
            bq_client=client,
            resolved=_stub_resolved(),
            videos_raw=_videos_raw(),
            personas=_personas(["u1", "u2"]),
            events=_empty_events(),
        )
    assert client.loads == []


def test_main_emits_public_job_summary(monkeypatch, capsys):
    monkeypatch.setattr(
        daily,
        "run_batch",
        lambda **kwargs: {
            "event": "job_summary",
            "contract_version": "batch-contract-v1",
            "job": "daily_recommendations",
            "status": "succeeded",
            "dt": "2026-07-21",
            "events_dt": "2026-07-20",
            "users": 2,
            "skipped_users": 0,
            "rows": 10,
            "model_run_id": "run-e2e",
            "model_version": "9",
            "dry_run": True,
        },
    )
    assert daily.main(["--candidate-dt", "2026-07-21", "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert (payload["event"], payload["status"], payload["rows"]) == (
        "job_summary",
        "succeeded",
        10,
    )


def test_main_rejects_invalid_ratio_with_exit_2(capsys):
    assert daily.main(["--max-skip-ratio", "1.1"]) == 2
    assert json.loads(capsys.readouterr().out)["error_type"] == "invalid_arguments"
