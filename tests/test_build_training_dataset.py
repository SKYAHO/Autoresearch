import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.pipeline import build_training_dataset  # noqa: E402


def test_load_personas_reads_csv(tmp_path):
    csv_path = tmp_path / "personas.csv"
    pd.DataFrame({"uuid": ["u1"], "age": [25], "occupation": ["Student"]}).to_csv(
        csv_path, index=False
    )

    result = build_training_dataset.load_personas(str(csv_path))

    assert list(result["uuid"]) == ["u1"]


def test_load_personas_reads_parquet(tmp_path):
    # parquet은 실제 virtual_users 파이프라인 원본 스키마(user_id 등)라, CSV mock과
    # 달리 to_personas_frame()을 거쳐 uuid 계약 컬럼으로 정규화되어야 한다(#229).
    parquet_path = tmp_path / "personas.parquet"
    pd.DataFrame(
        {
            "user_id": ["u1"],
            "age": [25],
            "occupation": ["Student"],
            "hobby_keywords": [["gaming"]],
            "interest_keywords": [["esports"]],
            "lifestyle_keywords": [None],
        }
    ).to_parquet(parquet_path)

    result = build_training_dataset.load_personas(str(parquet_path))

    assert list(result["uuid"]) == ["u1"]


def test_main_rejects_invalid_videos_source():
    with pytest.raises(ValueError, match="videos_source"):
        build_training_dataset.main(videos_source="not-a-real-source")


def test_load_videos_from_bigquery_queries_configured_table(monkeypatch):
    fake_df = pd.DataFrame({"video_id": ["v1"]})
    fake_query_job = MagicMock()
    fake_query_job.to_dataframe.return_value = fake_df
    fake_client = MagicMock()
    fake_client.query.return_value = fake_query_job

    fake_bigquery_module = MagicMock()
    fake_bigquery_module.Client.return_value = fake_client
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake_bigquery_module)
    monkeypatch.setitem(sys.modules, "google.cloud", MagicMock(bigquery=fake_bigquery_module))

    result = build_training_dataset.load_videos_from_bigquery()

    assert result is fake_df
    fake_client.query.assert_called_once()
    query_text = fake_client.query.call_args[0][0]
    assert build_training_dataset.BIGQUERY_VIDEOS_TABLE in query_text
    assert "video_category AS categoryId" in query_text
    assert "channel_subscriber_count AS channelSubscriberCount" in query_text
    assert "channel_view_count AS channelViewCount" in query_text
    assert "channel_video_count AS channelVideoCount" in query_text


def test_main_rejects_invalid_events_source():
    with pytest.raises(ValueError, match="events_source"):
        build_training_dataset.main(events_source="not-a-real-source")


def test_main_rejects_bigquery_events_source_without_date_range():
    with pytest.raises(ValueError, match="events_start_date"):
        build_training_dataset.main(events_source="bigquery")


def test_load_events_from_bigquery_queries_configured_table(monkeypatch):
    fake_df = pd.DataFrame({"event_id": ["e1"]})
    fake_query_job = MagicMock()
    fake_query_job.to_dataframe.return_value = fake_df
    fake_client = MagicMock()
    fake_client.query.return_value = fake_query_job

    fake_bigquery_module = MagicMock()
    fake_bigquery_module.Client.return_value = fake_client
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake_bigquery_module)
    monkeypatch.setitem(sys.modules, "google.cloud", MagicMock(bigquery=fake_bigquery_module))

    result = build_training_dataset.load_events_from_bigquery("2026-06-24", "2026-07-01")

    assert result is fake_df
    fake_client.query.assert_called_once()
    query_text = fake_client.query.call_args[0][0]
    assert build_training_dataset.BIGQUERY_ACTION_LOG_TABLE in query_text
    assert "dt BETWEEN '2026-06-24' AND '2026-07-01'" in query_text


def _long_event(event_id, ts, user_id, event_type, video_id, watch_time_sec=None):
    return {
        "event_id": event_id,
        "event_timestamp": pd.Timestamp(ts),
        "user_id": user_id,
        "event_type": event_type,
        "video_id": video_id,
        "watch_time_sec": watch_time_sec,
    }


def test_derive_wide_events_attributes_click_view_like_correctly():
    rows = [
        # Case A: impression only, no click
        _long_event("e1", "2026-07-01 00:00:00", "uA", "impression", "vA"),
        # Case B: impression + click, no view
        _long_event("e2", "2026-07-01 00:00:00", "uB", "impression", "vB"),
        _long_event("e3", "2026-07-01 00:00:10", "uB", "click", "vB"),
        # Case C: impression + click + view + like (full chain)
        _long_event("e4", "2026-07-01 00:00:00", "uC", "impression", "vC"),
        _long_event("e5", "2026-07-01 00:00:10", "uC", "click", "vC"),
        _long_event("e6", "2026-07-01 00:00:13", "uC", "view", "vC", watch_time_sec=120),
        _long_event("e7", "2026-07-01 00:00:15", "uC", "like", "vC"),
    ]
    long_events = pd.DataFrame(rows)

    wide = build_training_dataset.derive_wide_events(long_events)
    by_user = wide.set_index("user_id")

    assert by_user.loc["uA", "clicked"] == 0
    assert by_user.loc["uA", "liked"] == 0
    assert by_user.loc["uA", "watch_time_sec"] == 0

    assert by_user.loc["uB", "clicked"] == 1
    assert by_user.loc["uB", "liked"] == 0
    assert by_user.loc["uB", "watch_time_sec"] == 0

    assert by_user.loc["uC", "clicked"] == 1
    assert by_user.loc["uC", "liked"] == 1
    assert by_user.loc["uC", "watch_time_sec"] == 120


def test_derive_wide_events_click_attributes_to_nearest_preceding_impression():
    # 같은 (user, video)에 impression 2개. click은 두 번째 impression 직후에만
    # 발생 — 첫 번째 impression이 아니라 두 번째 impression에 귀속되어야 한다.
    rows = [
        _long_event("i1", "2026-07-01 00:00:00", "uD", "impression", "vD"),
        _long_event("i2", "2026-07-01 00:10:00", "uD", "impression", "vD"),
        _long_event("c1", "2026-07-01 00:10:05", "uD", "click", "vD"),
    ]
    long_events = pd.DataFrame(rows)

    wide = build_training_dataset.derive_wide_events(long_events)
    by_event = wide.set_index("event_id")

    assert by_event.loc["i1", "clicked"] == 0
    assert by_event.loc["i2", "clicked"] == 1


def test_derive_wide_events_uses_earliest_click_as_anchor_when_multiple_match():
    # impression 1개에 click 후보가 2개(둘 다 label_window_sec 이내) — 더 이른
    # click(c2)이 anchor가 되고, view는 그 anchor(c2) 기준으로만 체이닝된다.
    rows = [
        _long_event("i1", "2026-07-01 00:00:00", "uE", "impression", "vE"),
        _long_event("c2", "2026-07-01 00:00:10", "uE", "click", "vE"),
        _long_event("c1", "2026-07-01 00:28:00", "uE", "click", "vE"),
        _long_event("v1", "2026-07-01 00:00:13", "uE", "view", "vE", watch_time_sec=42),
    ]
    long_events = pd.DataFrame(rows)

    wide = build_training_dataset.derive_wide_events(long_events)
    row = wide.set_index("user_id").loc["uE"]

    assert row["clicked"] == 1
    assert row["watch_time_sec"] == 42


def test_main_trims_padding_range_from_output(tmp_path, monkeypatch):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    pd.DataFrame(
        {
            "video_id": ["v1"],
            "categoryId": ["Music"],
            "duration": ["PT5M"],
            "viewCount": [1000],
            "likeCount": [50],
            "commentCount": [10],
            "publishedAt": ["2026-01-01"],
            "title": ["t"],
            "description": ["d"],
        }
    ).to_csv(raw_dir / "youtube_videos.csv", index=False)
    pd.DataFrame(
        {
            "uuid": ["u1"],
            "age": [25],
            "occupation": ["Student"],
            "hobbies_and_interests": ["gaming"],
            "hobbies_and_interests_list": ["[]"],
        }
    ).to_csv(raw_dir / "personas.csv", index=False)

    # 왼쪽 padding 구간(7일 이전), 학습 구간 내부, 오른쪽 padding 구간(end_date
    # 이후) 각각 impression 1건씩 — load_events_from_bigquery를 mock해서
    # 실제 padding 계산과 무관하게 이 세 구간이 다 반환됐다고 가정한다.
    long_events = pd.DataFrame(
        [
            _long_event("before", "2026-07-05 00:00:00", "u1", "impression", "v1"),
            _long_event("in_window", "2026-07-08 12:00:00", "u1", "impression", "v1"),
            _long_event("after", "2026-07-09 00:30:00", "u1", "impression", "v1"),
        ]
    )
    monkeypatch.setattr(
        build_training_dataset,
        "load_events_from_bigquery",
        lambda start, end: long_events,
    )

    output_path = tmp_path / "training_dataset.csv"
    build_training_dataset.main(
        raw_dir=str(raw_dir),
        output_path=str(output_path),
        events_source="bigquery",
        events_start_date="2026-07-08",
        events_end_date="2026-07-09",
    )

    result = pd.read_csv(output_path)
    assert len(result) == 1


def test_main_outputs_21_model_input_columns_plus_clicked(tmp_path, monkeypatch):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    pd.DataFrame(
        {
            "video_id": ["v1"],
            "categoryId": ["Music"],
            "duration": ["PT5M"],
            "viewCount": [1000],
            "likeCount": [50],
            "commentCount": [10],
            "publishedAt": ["2026-01-01"],
            "title": ["t"],
            "description": ["d"],
            "channelSubscriberCount": [12345],
            "channelViewCount": [999999],
            "channelVideoCount": [321],
        }
    ).to_csv(raw_dir / "youtube_videos.csv", index=False)
    pd.DataFrame(
        {
            "uuid": ["u1"],
            "age": [25],
            "occupation": ["Student"],
            "hobbies_and_interests": ["gaming"],
            "hobbies_and_interests_list": ["[]"],
            "watch_time_band": ["morning"],
        }
    ).to_csv(raw_dir / "personas.csv", index=False)

    events_path = tmp_path / "events.csv"
    pd.DataFrame(
        {
            "event_id": ["e1"],
            "user_id": ["u1"],
            "video_id": ["v1"],
            "timestamp": ["2026-07-08 12:00:00"],
            "clicked": [0],
            "liked": [0],
            "watch_time_sec": [0],
        }
    ).to_csv(events_path, index=False)

    output_path = tmp_path / "training_dataset.csv"
    build_training_dataset.main(
        raw_dir=str(raw_dir),
        events_path=str(events_path),
        output_path=str(output_path),
    )

    result = pd.read_csv(output_path)
    assert len(result.columns) == 22  # 21 Model Input + clicked label
    assert result.loc[0, "watch_time_band"] == "morning"
    assert result.loc[0, "channel_subscriber_count"] == 12345
    assert result.loc[0, "channel_view_count"] == 999999
    assert result.loc[0, "channel_video_count"] == 321
    assert result.loc[0, "recent_view_count_7d"] == 0
    assert result.loc[0, "total_event_count_7d"] == 0


def test_main_bigquery_mode_never_resolves_local_data_dir(tmp_path, monkeypatch):
    # Dockerfile.train(GCS 코드 부트스트랩 이미지)은 로컬 data/ 디렉토리를 이미지에
    # 전혀 포함하지 않는다. videos_source/events_source가 모두 bigquery이고
    # personas_path/output_path가 명시되면 get_data_dir()을 절대 호출하지
    # 않아야 한다 — 안 그러면 컨테이너에서 항상 실패한다(issue #212).
    def _fail_if_called():
        raise AssertionError("get_data_dir() must not be called when all paths are explicit")

    monkeypatch.setattr(build_training_dataset, "get_data_dir", _fail_if_called)

    videos_df = pd.DataFrame(
        {
            "video_id": ["v1"],
            "categoryId": ["Music"],
            "duration": ["PT5M"],
            "viewCount": [1000],
            "likeCount": [50],
            "commentCount": [10],
            "publishedAt": ["2026-01-01"],
            "title": ["t"],
            "description": ["d"],
        }
    )
    monkeypatch.setattr(build_training_dataset, "load_videos_from_bigquery", lambda: videos_df)

    long_events = pd.DataFrame(
        [_long_event("i1", "2026-07-08 12:00:00", "u1", "impression", "v1")]
    )
    monkeypatch.setattr(
        build_training_dataset, "load_events_from_bigquery", lambda start, end: long_events
    )

    personas_path = tmp_path / "personas.csv"
    pd.DataFrame(
        {
            "uuid": ["u1"],
            "age": [25],
            "occupation": ["Student"],
            "hobbies_and_interests": ["gaming"],
            "hobbies_and_interests_list": ["[]"],
        }
    ).to_csv(personas_path, index=False)

    output_path = tmp_path / "training_dataset.csv"

    build_training_dataset.main(
        videos_source="bigquery",
        events_source="bigquery",
        events_start_date="2026-07-08",
        events_end_date="2026-07-09",
        personas_path=str(personas_path),
        output_path=str(output_path),
    )

    assert output_path.exists()


def test_get_data_dir_creates_directory_when_none_found(tmp_path, monkeypatch):
    # 컨테이너 최초 실행 시 프로젝트 루트 어디에도 data/가 없다 — 예외를 던지는
    # 대신 만들어서 돌려줘야 한다(issue #212). 걸어 올라가는 실제 파일시스템
    # 탐색은 OS마다 sentinel("/")이 달라 테스트하기 위험하므로, dirname을 즉시
    # sentinel로 만들어 "어디서도 못 찾음" 경로를 결정적으로 재현한다.
    created = {}
    monkeypatch.setattr(build_training_dataset, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(build_training_dataset.os.path, "exists", lambda path: False)
    monkeypatch.setattr(build_training_dataset.os.path, "dirname", lambda path: "/")
    monkeypatch.setattr(
        build_training_dataset.os,
        "makedirs",
        lambda path, exist_ok=False: created.setdefault("path", path),
    )

    data_dir = build_training_dataset.get_data_dir()

    assert data_dir == os.path.join(str(tmp_path), "data")
    assert created["path"] == data_dir


def test_derive_wide_events_like_without_view_defaults_to_zero():
    # like는 view를 거쳐서만 체이닝된다 — view가 없으면 like가 존재해도 0.
    rows = [
        _long_event("i1", "2026-07-01 00:00:00", "uF", "impression", "vF"),
        _long_event("c1", "2026-07-01 00:00:10", "uF", "click", "vF"),
        _long_event("l1", "2026-07-01 00:00:13", "uF", "like", "vF"),
    ]
    long_events = pd.DataFrame(rows)

    wide = build_training_dataset.derive_wide_events(long_events)
    row = wide.set_index("user_id").loc["uF"]

    assert row["clicked"] == 1
    assert row["liked"] == 0
    assert row["watch_time_sec"] == 0


# --- dataset 계층 분리(raw vs feature) ------------------------------------


def test_raw_table_id_uses_raw_dataset(monkeypatch):
    monkeypatch.setattr(build_training_dataset, "BIGQUERY_PROJECT", "proj")
    monkeypatch.setattr(build_training_dataset, "BIGQUERY_RAW_DATASET", "data_lake_raw")

    assert (
        build_training_dataset.raw_table_id("data_lake_action_log")
        == "proj.data_lake_raw.data_lake_action_log"
    )


def test_feature_table_id_uses_feature_dataset(monkeypatch):
    monkeypatch.setattr(build_training_dataset, "BIGQUERY_PROJECT", "proj")
    monkeypatch.setattr(build_training_dataset, "BIGQUERY_DATASET", "feast_offline_store")

    assert (
        build_training_dataset.feature_table_id("user_recommendations")
        == "proj.feast_offline_store.user_recommendations"
    )


def test_raw_and_feature_datasets_default_to_separate_datasets():
    # 기본값이 같아지면 dataset 분리가 무의미해진다 — 회귀 방지용 계약.
    assert build_training_dataset.BIGQUERY_RAW_DATASET == "data_lake_raw"
    assert build_training_dataset.BIGQUERY_DATASET == "feast_offline_store"


def _fake_bigquery(monkeypatch, fake_df):
    fake_query_job = MagicMock()
    fake_query_job.to_dataframe.return_value = fake_df
    fake_client = MagicMock()
    fake_client.query.return_value = fake_query_job

    fake_bigquery_module = MagicMock()
    fake_bigquery_module.Client.return_value = fake_client
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake_bigquery_module)
    monkeypatch.setitem(sys.modules, "google.cloud", MagicMock(bigquery=fake_bigquery_module))
    return fake_client


def test_load_videos_from_bigquery_reads_raw_dataset(monkeypatch):
    monkeypatch.setattr(build_training_dataset, "BIGQUERY_PROJECT", "proj")
    monkeypatch.setattr(build_training_dataset, "BIGQUERY_RAW_DATASET", "data_lake_raw")
    monkeypatch.setattr(build_training_dataset, "BIGQUERY_DATASET", "feast_offline_store")
    fake_client = _fake_bigquery(monkeypatch, pd.DataFrame({"video_id": ["v1"]}))

    build_training_dataset.load_videos_from_bigquery()

    query_text = fake_client.query.call_args[0][0]
    assert "`proj.data_lake_raw.data_lake_youtube_trending_kr`" in query_text
    assert "feast_offline_store" not in query_text


def test_load_events_from_bigquery_reads_raw_dataset(monkeypatch):
    monkeypatch.setattr(build_training_dataset, "BIGQUERY_PROJECT", "proj")
    monkeypatch.setattr(build_training_dataset, "BIGQUERY_RAW_DATASET", "data_lake_raw")
    monkeypatch.setattr(build_training_dataset, "BIGQUERY_DATASET", "feast_offline_store")
    fake_client = _fake_bigquery(monkeypatch, pd.DataFrame({"event_id": ["e1"]}))

    build_training_dataset.load_events_from_bigquery("2026-06-24", "2026-07-01")

    query_text = fake_client.query.call_args[0][0]
    assert "`proj.data_lake_raw.data_lake_action_log`" in query_text
    assert "feast_offline_store" not in query_text
