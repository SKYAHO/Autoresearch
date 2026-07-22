"""일일 추천 배치 헬퍼 단위 테스트 — 실 BQ 미접속(fake client)."""

import json
import logging
from datetime import UTC, date, datetime

import numpy as np
import pandas as pd
import pytest

import src.pipeline.daily_recommendations as daily
from src.features.model_contract import FeatureContractError, MODEL_FEATURE_COLUMNS
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


class _DataFrameJob:
    def __init__(self, frame):
        self._frame = frame

    def to_dataframe(self):
        return self._frame.copy()


class _IngressClient(_FakeClient):
    def __init__(self, candidates, virtual_users):
        super().__init__()
        self.candidates = candidates
        self.virtual_users = virtual_users
        self.queries = []

    def query(self, sql):
        self.queries.append(sql)
        if "SELECT video_id" in sql:
            columns = [
                "video_id", "categoryId", "duration", "viewCount", "likeCount",
                "commentCount", "publishedAt",
            ]
            for column in (
                "channelSubscriberCount", "channelViewCount", "channelVideoCount",
            ):
                if column in sql:
                    columns.append(column)
            return _DataFrameJob(self.candidates.loc[:, columns])
        columns = [
            "user_id", "age", "occupation", "hobby_keywords", "interest_keywords",
            "lifestyle_keywords",
        ]
        if "watch_time_band" in sql:
            columns.append("watch_time_band")
        return _DataFrameJob(self.virtual_users.loc[:, columns])


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

    def __init__(self):
        self.received_columns: list[tuple[str, ...]] = []

    def predict_proba(self, features):
        self.received_columns.append(tuple(features.columns))
        return np.column_stack([np.full(len(features), 0.5), np.full(len(features), 0.5)])


class _FeatureCaptureModel:
    def __init__(self):
        self.received_features = []

    def predict_proba(self, features):
        self.received_features.append(features.copy())
        return np.column_stack([np.full(len(features), 0.5), np.full(len(features), 0.5)])


def _stub_resolved() -> ResolvedModel:
    model = _EverythingHalfModel()
    reranker = Reranker(
        model=model,
        feature_columns=MODEL_FEATURE_COLUMNS,
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


def test_run_batch_passes_canonical_feature_order_to_reranker():
    resolved = _stub_resolved()
    run_batch(
        candidate_dt=date(2026, 7, 21),
        events_dt=date(2026, 7, 21),
        dry_run=True,
        bq_client=_FakeClient(),
        resolved=resolved,
        videos_raw=_videos_raw(),
        personas=_personas(["u1"]),
        events=_empty_events(),
    )

    assert resolved.reranker.model.received_columns == [MODEL_FEATURE_COLUMNS]


def test_run_batch_preserves_ingress_values_in_canonical_vector():
    candidates = pd.DataFrame(
        {
            "video_id": ["v1"],
            "categoryId": ["Gaming"],
            "duration": ["PT1M"],
            "viewCount": [1000],
            "likeCount": [100],
            "commentCount": [10],
            "publishedAt": ["2026-07-01"],
            "channelSubscriberCount": [12345],
            "channelViewCount": [999999],
            "channelVideoCount": [321],
        }
    )
    virtual_users = pd.DataFrame(
        {
            "user_id": ["u1"],
            "age": [25],
            "occupation": ["student"],
            "hobby_keywords": [[]],
            "interest_keywords": [[]],
            "lifestyle_keywords": [[]],
            "watch_time_band": ["night"],
        }
    )
    client = _IngressClient(candidates, virtual_users)
    model = _FeatureCaptureModel()
    resolved = ResolvedModel(
        reranker=Reranker(
            model=model,
            feature_columns=MODEL_FEATURE_COLUMNS,
            categorical_categories={},
        ),
        run_id="run-ingress",
        model_version="1",
    )

    report = run_batch(
        candidate_dt=date(2026, 7, 21),
        events_dt=date(2026, 7, 21),
        bq_client=client,
        resolved=resolved,
        events=_empty_events(),
        dry_run=True,
    )

    assert report["skipped_users"] == 0
    assert tuple(model.received_features[0].columns) == MODEL_FEATURE_COLUMNS
    row = model.received_features[0].iloc[0]
    assert row["watch_time_band"] == "night"
    assert row["channel_subscriber_count"] == 12345
    assert row["channel_view_count"] == 999999
    assert row["channel_video_count"] == 321


def test_run_batch_rejects_invalid_feature_contract_before_user_loop(monkeypatch):
    resolved = ResolvedModel(
        reranker=Reranker(
            model=_EverythingHalfModel(),
            feature_columns=MODEL_FEATURE_COLUMNS[:-1],
            categorical_categories={},
        ),
        run_id="run-invalid",
        model_version="1",
    )
    loop_users: list[str] = []

    def _should_not_enter_loop(**kwargs):
        loop_users.append(kwargs["user_id"])
        return pd.DataFrame()

    monkeypatch.setattr(daily, "build_pool_feature_frame", _should_not_enter_loop)

    with pytest.raises(FeatureContractError):
        run_batch(
            candidate_dt=date(2026, 7, 21),
            events_dt=date(2026, 7, 21),
            dry_run=True,
            bq_client=_FakeClient(),
            resolved=resolved,
            videos_raw=_videos_raw(),
            personas=_personas(["u1"]),
            events=_empty_events(),
        )

    assert loop_users == []


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


def _bq_empty_long_events() -> pd.DataFrame:
    """빈 파티션 조회 시 BigQuery to_dataframe()가 반환하는 형상 — STRING 컬럼이 object dtype."""
    return pd.DataFrame(
        {
            "event_id": pd.Series([], dtype="object"),
            "event_timestamp": pd.Series([], dtype="datetime64[us, UTC]"),
            "user_id": pd.Series([], dtype="object"),
            "event_type": pd.Series([], dtype="object"),
            "video_id": pd.Series([], dtype="object"),
            "watch_time_sec": pd.Series([], dtype="Int64"),
        }
    )


def test_run_batch_cold_start_scores_with_empty_events_partition():
    # 빈 action-log 파티션(콜드 스타트)은 정상 경로다: dtype 붕괴 없이 0 집계로 채점해야 한다.
    client = _FakeClient()
    report = run_batch(
        candidate_dt=date(2026, 7, 21),
        events_dt=date(2026, 7, 21),
        bq_client=client,
        resolved=_stub_resolved(),
        videos_raw=_videos_raw(),
        personas=_personas(["u1", "u2"]),
        events=daily.derive_wide_events(_bq_empty_long_events()),
    )
    assert (report["users"], report["skipped_users"], report["rows"]) == (2, 0, 10)


def test_run_batch_never_truncates_partition_when_all_users_skipped(monkeypatch):
    # max_skip_ratio=1.0(허용 경계)에서도 전 유저 실패 시 빈 프레임으로 파티션을 덮으면 안 된다.
    client = _FakeClient()

    def _always_fail(**kwargs):
        raise KeyError("broken persona")

    monkeypatch.setattr(daily, "build_pool_feature_frame", _always_fail)
    with pytest.raises(RuntimeError):
        run_batch(
            candidate_dt=date(2026, 7, 21),
            events_dt=date(2026, 7, 21),
            max_skip_ratio=1.0,
            bq_client=client,
            resolved=_stub_resolved(),
            videos_raw=_videos_raw(),
            personas=_personas(["u1", "u2"]),
            events=_empty_events(),
        )
    assert client.loads == []


def test_run_batch_snapshots_video_features_at_candidate_dt(monkeypatch):
    # action log가 지연돼도 영상 스냅샷(days_since_upload 기준일)은 candidate_dt여야 한다.
    captured: dict = {}
    original = daily.build_pool_feature_frame

    def _capture(**kwargs):
        captured.update(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(daily, "build_pool_feature_frame", _capture)
    run_batch(
        candidate_dt=date(2026, 7, 21),
        events_dt=date(2026, 7, 18),
        dry_run=True,
        bq_client=_FakeClient(),
        resolved=_stub_resolved(),
        videos_raw=_videos_raw(),
        personas=_personas(["u1"]),
        events=_empty_events(),
    )
    assert captured["snapshot_date"] == "2026-07-21"
    assert captured["as_of"] == "2026-07-19 00:00:00"


def test_quarantine_warning_names_user_and_exception_type(monkeypatch, caplog):
    # spec 계약: 격리 시 stderr warning에 user_id와 예외 타입이 보여야 한다.
    original = daily.build_pool_feature_frame

    def _fail_u2(**kwargs):
        if kwargs["user_id"] == "u2":
            raise KeyError("broken persona")
        return original(**kwargs)

    monkeypatch.setattr(daily, "build_pool_feature_frame", _fail_u2)
    with caplog.at_level(logging.WARNING, logger="src.pipeline.daily_recommendations"):
        run_batch(
            candidate_dt=date(2026, 7, 21),
            events_dt=date(2026, 7, 21),
            max_skip_ratio=0.6,
            dry_run=True,
            bq_client=_FakeClient(),
            resolved=_stub_resolved(),
            videos_raw=_videos_raw(),
            personas=_personas(["u1", "u2"]),
            events=_empty_events(),
        )
    message = next(
        record.getMessage() for record in caplog.records if "quarantined" in record.getMessage()
    )
    assert "u2" in message
    assert "KeyError" in message


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


# --- dataset 계층 분리(raw vs feature) ------------------------------------


class _QueryRecordingClient(_FakeClient):
    """MAX(dt) 조회 SQL을 기록하는 fake — dataset 해석만 검증한다."""

    def __init__(self, max_dt: date):
        super().__init__()
        self.queries: list[str] = []
        self._max_dt = max_dt

    def query(self, sql: str):
        self.queries.append(sql)
        job = type("_Job", (), {})()
        job.result = lambda: iter([type("_Row", (), {"max_dt": self._max_dt})()])
        return job


def test_run_batch_resolves_raw_tables_in_raw_dataset(monkeypatch):
    import src.pipeline.build_training_dataset as btd

    monkeypatch.setattr(btd, "BIGQUERY_PROJECT", "proj")
    monkeypatch.setattr(btd, "BIGQUERY_RAW_DATASET", "data_lake_raw")
    monkeypatch.setattr(btd, "BIGQUERY_DATASET", "feast_offline_store")

    client = _QueryRecordingClient(date(2026, 7, 21))
    run_batch(
        bq_client=client,
        resolved=_stub_resolved(),
        videos_raw=_videos_raw(),
        personas=_personas(["u1"]),
        events=_empty_events(),
        dry_run=True,
    )

    joined = "\n".join(client.queries)
    assert "`proj.data_lake_raw.data_lake_youtube_trending_kr`" in joined
    assert "`proj.data_lake_raw.data_lake_action_log`" in joined
    assert "feast_offline_store" not in joined


def test_run_batch_writes_output_table_in_feature_dataset(monkeypatch):
    import src.pipeline.build_training_dataset as btd

    monkeypatch.setattr(btd, "BIGQUERY_PROJECT", "proj")
    monkeypatch.setattr(btd, "BIGQUERY_RAW_DATASET", "data_lake_raw")
    monkeypatch.setattr(btd, "BIGQUERY_DATASET", "feast_offline_store")

    client = _FakeClient()
    run_batch(
        candidate_dt=date(2026, 7, 21),
        events_dt=date(2026, 7, 21),
        bq_client=client,
        resolved=_stub_resolved(),
        videos_raw=_videos_raw(),
        personas=_personas(["u1"]),
        events=_empty_events(),
    )

    destinations = [dest for _, dest, _ in client.loads]
    assert destinations == ["proj.feast_offline_store.user_recommendations$20260721"]


def test_virtual_users_table_stays_in_feature_dataset(monkeypatch):
    """CAVEAT(#232): asset_virtual_user_vu_1000 은 삭제 예정이지만 이번 범위에서는
    소스를 바꾸지 않는다 — feature dataset 해석을 유지한다는 계약을 고정한다."""
    import src.pipeline.build_training_dataset as btd

    monkeypatch.setattr(btd, "BIGQUERY_PROJECT", "proj")
    monkeypatch.setattr(btd, "BIGQUERY_DATASET", "feast_offline_store")

    captured: list[str] = []

    def _capture(client, table_id):
        captured.append(table_id)
        return _personas(["u1"])

    monkeypatch.setattr(daily, "_load_virtual_users", _capture)
    monkeypatch.setattr(daily, "to_personas_frame", lambda frame: frame)

    run_batch(
        candidate_dt=date(2026, 7, 21),
        events_dt=date(2026, 7, 21),
        bq_client=_FakeClient(),
        resolved=_stub_resolved(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        dry_run=True,
    )

    assert captured == ["proj.feast_offline_store.asset_virtual_user_vu_1000"]
