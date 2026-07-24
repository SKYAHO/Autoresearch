import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.pipeline import build_training_dataset  # noqa: E402
from src.features.model_contract import MODEL_FEATURE_COLUMNS  # noqa: E402
import src.features.assembly as assembly_module  # noqa: E402


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
    # title/description은 다운스트림 어디에서도 쓰지 않는다 — 실 데이터
    # 규모(12만+ 행)에서 텍스트 컬럼을 그냥 들고만 있는 낭비를 막기 위해
    # 아예 조회하지 않는다(#231/#249).
    assert "video_title" not in query_text
    assert "video_description" not in query_text
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
    # KST 다음 날) 각각 impression 1건씩 — load_events_from_bigquery를 mock해서
    # 실제 padding 계산과 무관하게 이 세 구간이 다 반환됐다고 가정한다.
    # #286: 인자는 "이벤트 발생 KST 날짜 폐구간"이므로 after는 KST 07-10
    # (= 07-09 15:00Z 이후)이어야 트림 대상이다.
    long_events = pd.DataFrame(
        [
            _long_event("before", "2026-07-05 00:00:00", "u1", "impression", "v1"),
            _long_event("in_window", "2026-07-08 12:00:00", "u1", "impression", "v1"),
            _long_event("after", "2026-07-09 16:00:00", "u1", "impression", "v1"),
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


def test_padded_dt_range_covers_lookback_and_session_days():
    # #286: dt 프루닝은 왼쪽 7일 룩백 + 오른쪽 세션 완성 padding(일 단위 올림).
    # 기존 구현은 자정 + seconds(3000) 후 날짜 재포맷이라 오른쪽 pad가 항상
    # no-op이었다 — end 다음 날 파티션이 포함되어야 한다.
    start, end = build_training_dataset.padded_dt_range("2026-07-08", "2026-07-09")
    assert start == "2026-07-01"
    assert end == "2026-07-10"


def test_events_kst_window_returns_utc_boundaries():
    # #286: 인자 의미는 "이벤트 발생 KST 날짜 폐구간 [start, end]".
    # UTC 저장 timestamp 기준 경계는 [start 00:00 KST, end+1 00:00 KST).
    lo, hi = build_training_dataset.events_kst_window("2026-07-08", "2026-07-09")
    assert lo == "2026-07-07 15:00:00"
    assert hi == "2026-07-09 15:00:00"


def _write_minimal_raw(raw_dir):
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


def test_main_trim_uses_kst_day_boundaries(tmp_path, monkeypatch):
    # #286 경계 계약: KST 폐구간 [07-08, 07-09] =
    # UTC [07-07 15:00:00, 07-09 15:00:00). 실 BQ 경로처럼 tz-aware UTC
    # 프레임을 주입해 정규화까지 함께 검증한다.
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    _write_minimal_raw(raw_dir)

    long_events = pd.DataFrame(
        [
            _long_event("kst_prev_day", "2026-07-07 14:59:59", "u1", "impression", "v1"),
            _long_event("kst_start_midnight", "2026-07-07 15:00:00", "u1", "impression", "v1"),
            _long_event("kst_end_last_sec", "2026-07-09 14:59:59", "u1", "impression", "v1"),
            _long_event("kst_next_day", "2026-07-09 15:00:00", "u1", "impression", "v1"),
        ]
    )
    long_events["event_timestamp"] = pd.to_datetime(
        long_events["event_timestamp"], utc=True
    )
    captured = {}

    def _fake_load(start, end):
        captured["dt_range"] = (start, end)
        return long_events

    monkeypatch.setattr(build_training_dataset, "load_events_from_bigquery", _fake_load)

    output_path = tmp_path / "training_dataset.csv"
    build_training_dataset.main(
        raw_dir=str(raw_dir),
        output_path=str(output_path),
        events_source="bigquery",
        events_start_date="2026-07-08",
        events_end_date="2026-07-09",
    )

    result = pd.read_csv(output_path)
    assert len(result) == 2
    # dt 프루닝 범위가 padded_dt_range와 일치해야 한다 (오른쪽 pad no-op 수정).
    assert captured["dt_range"] == ("2026-07-01", "2026-07-10")


def test_main_attributes_session_crossing_kst_midnight(tmp_path, monkeypatch):
    # #286: end일 KST 23:59 impression의 클릭이 KST 자정을 넘어 dt=end+1
    # 파티션에 실려도(오른쪽 dt pad 덕에 로드됨) attribution되어야 한다.
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    _write_minimal_raw(raw_dir)

    long_events = pd.DataFrame(
        [
            _long_event("imp_last_min", "2026-07-09 14:59:00", "u1", "impression", "v1"),
            _long_event("click_next_kst_day", "2026-07-09 15:00:30", "u1", "click", "v1"),
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
    assert int(result["clicked"].iloc[0]) == 1


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
            "primary_categories": ['["Music"]'],
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
    assert list(result.columns) == [*MODEL_FEATURE_COLUMNS, "clicked"]
    assert result.loc[0, "watch_time_band"] == "morning"
    assert result.loc[0, "channel_subscriber_count"] == 12345
    # video의 category_id="Music"이 personas.csv의 primary_categories=["Music"]에
    # 포함되므로, 실제 값을 배선했다면 1이어야 한다 (#205).
    assert result.loc[0, "preferred_category_match"] == 1
    assert result.loc[0, "channel_view_count"] == 999999
    assert result.loc[0, "channel_video_count"] == 321
    assert result.loc[0, "recent_view_count_7d"] == 0
    assert result.loc[0, "total_event_count_7d"] == 0


def test_main_does_not_require_video_title_or_description(tmp_path, monkeypatch):
    # 최종 21컬럼 어디에도 title/description은 없다 — 예전에는 그런데도 Step 1의
    # joined 중간 프레임을 만들 때 videos_raw를 JOIN해서 title/description까지
    # 끌어왔다. 실 데이터 규모(약 160만 행)에서 이 두 텍스트 컬럼이 joined 최대
    # 크기의 메모리 사용량을 크게 늘려 최종 join 단계에서 OOM으로 이어졌다
    # (issue #231). videos 원본에 title/description이 아예 없어도 파이프라인이
    # 정상 동작해야, 그 JOIN과 두 컬럼이 더 이상 필요 없다는 것이 보장된다.
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
            # title/description 의도적으로 생략.
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
    assert "title" not in result.columns
    assert "description" not in result.columns
    assert len(result) == 1


def test_main_preferred_category_match_varies_by_event_category_for_same_user(tmp_path, monkeypatch):
    # persona의 primary_categories는 유저당 1개뿐이라 (user_id, category_id) 단위로
    # 선계산해 조인한다(#240) — 같은 유저가 서로 다른 category_id의 영상 2개에
    # 노출되면, 조인이 카테고리별로 올바르게 구분해서 preferred_category_match를
    # 매겨야 한다(유저 단위로 뭉개져 항상 같은 값이 나오면 회귀).
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    pd.DataFrame(
        {
            "video_id": ["v1", "v2"],
            "categoryId": ["Music", "Gaming"],
            "duration": ["PT5M", "PT5M"],
            "viewCount": [1000, 1000],
            "likeCount": [50, 50],
            "commentCount": [10, 10],
            "publishedAt": ["2026-01-01", "2026-01-01"],
        }
    ).to_csv(raw_dir / "youtube_videos.csv", index=False)
    pd.DataFrame(
        {
            "uuid": ["u1"],
            "age": [25],
            "occupation": ["Student"],
            "hobbies_and_interests": ["music"],
            "hobbies_and_interests_list": ["[]"],
            "primary_categories": ['["Music"]'],
        }
    ).to_csv(raw_dir / "personas.csv", index=False)

    events_path = tmp_path / "events.csv"
    pd.DataFrame(
        {
            "event_id": ["e1", "e2"],
            "user_id": ["u1", "u1"],
            "video_id": ["v1", "v2"],
            "timestamp": ["2026-07-08 12:00:00", "2026-07-08 12:05:00"],
            "clicked": [0, 0],
            "liked": [0, 0],
            "watch_time_sec": [0, 0],
        }
    ).to_csv(events_path, index=False)

    output_path = tmp_path / "training_dataset.csv"
    build_training_dataset.main(
        raw_dir=str(raw_dir),
        events_path=str(events_path),
        output_path=str(output_path),
    )

    result = pd.read_csv(output_path).sort_values("category_id").reset_index(drop=True)
    assert len(result) == 2
    gaming_row = result[result["category_id"] == "Gaming"].iloc[0]
    music_row = result[result["category_id"] == "Music"].iloc[0]
    assert gaming_row["preferred_category_match"] == 0
    assert music_row["preferred_category_match"] == 1


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


def _trending_snapshots_videos_df() -> pd.DataFrame:
    """같은 영상이 트렌딩 날짜별로 3번 등장하는 실제 원본 모양의 fixture.

    data_lake_youtube_trending_kr은 영상이 트렌딩에 오른 날마다 한 행이 쌓이는
    스냅샷 테이블이라 video_id가 유일하지 않다(실측 영상당 평균 2.66행, 최대 32행).
    viewCount를 스냅샷마다 다르게 둬서 "어느 스냅샷이 선택됐는지"를 값으로 구분한다.
    """
    return pd.DataFrame(
        {
            "video_id": ["v1", "v1", "v1"],
            "video_trending_date": pd.to_datetime(
                ["2026-07-06", "2026-07-08", "2026-07-10"]
            ),
            "categoryId": ["Music", "Music", "Music"],
            "duration": ["PT5M", "PT5M", "PT5M"],
            "viewCount": [100, 200, 300],
            "likeCount": [10, 20, 30],
            "commentCount": [1, 2, 3],
            "publishedAt": ["2026-01-01", "2026-01-01", "2026-01-01"],
        }
    )


def _single_persona_csv(personas_path) -> None:
    pd.DataFrame(
        {
            "uuid": ["u1"],
            "age": [25],
            "occupation": ["Student"],
            "hobbies_and_interests": ["music"],
            "hobbies_and_interests_list": ["[]"],
            "primary_categories": ['["Music"]'],
        }
    ).to_csv(personas_path, index=False)


def test_main_duplicate_video_snapshots_yield_one_row_per_event(tmp_path, monkeypatch):
    # 트렌딩 스냅샷 중복(#297): 평면 조인이면 이벤트 1건이 스냅샷 수(여기선 3)만큼
    # 복제되어 같은 impression이 학습셋에 3번 들어간다. point-in-time 조인은
    # 이벤트당 정확히 1행만 내야 한다.
    monkeypatch.setattr(
        build_training_dataset, "load_videos_from_bigquery", _trending_snapshots_videos_df
    )
    long_events = pd.DataFrame(
        [_long_event("i1", "2026-07-09 12:00:00", "u1", "impression", "v1")]
    )
    monkeypatch.setattr(
        build_training_dataset, "load_events_from_bigquery", lambda start, end: long_events
    )
    personas_path = tmp_path / "personas.csv"
    _single_persona_csv(personas_path)
    output_path = tmp_path / "training_dataset.csv"

    build_training_dataset.main(
        videos_source="bigquery",
        events_source="bigquery",
        events_start_date="2026-07-09",
        events_end_date="2026-07-10",
        personas_path=str(personas_path),
        output_path=str(output_path),
    )

    result = pd.read_csv(output_path)
    assert len(result) == 1, (
        f"이벤트 1건에 스냅샷 3건이 곱해져 {len(result)}행이 됐습니다 — "
        "학습셋에 같은 impression이 중복 수록됩니다"
    )


def test_main_video_features_come_from_snapshot_at_or_before_event(tmp_path, monkeypatch):
    # 미래 스냅샷 차단(#297): 이벤트(07-09) 시점에서는 07-10 스냅샷(viewCount=300)을
    # 쓰면 안 되고, 07-08 스냅샷(viewCount=200)이 선택돼야 한다.
    monkeypatch.setattr(
        build_training_dataset, "load_videos_from_bigquery", _trending_snapshots_videos_df
    )
    long_events = pd.DataFrame(
        [_long_event("i1", "2026-07-09 12:00:00", "u1", "impression", "v1")]
    )
    monkeypatch.setattr(
        build_training_dataset, "load_events_from_bigquery", lambda start, end: long_events
    )
    personas_path = tmp_path / "personas.csv"
    _single_persona_csv(personas_path)
    output_path = tmp_path / "training_dataset.csv"

    build_training_dataset.main(
        videos_source="bigquery",
        events_source="bigquery",
        events_start_date="2026-07-09",
        events_end_date="2026-07-10",
        personas_path=str(personas_path),
        output_path=str(output_path),
    )

    result = pd.read_csv(output_path)
    assert result.iloc[0]["view_count"] == 200, (
        "이벤트 시각 이하 중 최신 스냅샷(07-08, viewCount=200)이 아니라 "
        f"{result.iloc[0]['view_count']}가 선택됐습니다"
    )


def _csv_fixture_for_topic_similarity_source_tests(raw_dir, events_path):
    pd.DataFrame(
        {
            "video_id": ["v1", "v2"],
            "categoryId": ["Music", "Gaming"],
            "duration": ["PT5M", "PT5M"],
            "viewCount": [1000, 1000],
            "likeCount": [50, 50],
            "commentCount": [10, 10],
            "publishedAt": ["2026-01-01", "2026-01-01"],
        }
    ).to_csv(raw_dir / "youtube_videos.csv", index=False)
    pd.DataFrame(
        {
            "uuid": ["u1"],
            "age": [25],
            "occupation": ["Student"],
            "hobbies_and_interests": ["music"],
            "hobbies_and_interests_list": ["[]"],
            "primary_categories": ['["Music"]'],
        }
    ).to_csv(raw_dir / "personas.csv", index=False)
    pd.DataFrame(
        {
            "event_id": ["e1", "e2"],
            "user_id": ["u1", "u1"],
            "video_id": ["v1", "v2"],
            # e1(Music)의 entity timestamp: 2026-07-08 12:00:00
            "timestamp": ["2026-07-08 12:00:00", "2026-07-08 12:05:00"],
            "clicked": [0, 0],
            "liked": [0, 0],
            "watch_time_sec": [0, 0],
        }
    ).to_csv(events_path, index=False)


def _empty_user_category_similarity() -> pd.DataFrame:
    # 실제 BigQuery 응답은 쿼리 스키마 기준으로 타입이 고정되지만, 테스트용 빈
    # DataFrame은 dtype을 명시하지 않으면 event_timestamp가 DOUBLE로 추론되어
    # DuckDB의 TIMESTAMP 비교(`ucs.event_timestamp <= o.timestamp`)가 깨진다.
    return pd.DataFrame(
        {
            "user_id": pd.array([], dtype="string"),
            "category_id": pd.array([], dtype="string"),
            "event_timestamp": pd.array([], dtype="datetime64[ns]"),
            "topic_similarity": pd.array([], dtype="float64"),
        }
    )


def test_main_topic_similarity_source_bigquery_selects_most_recent_past_row(tmp_path, monkeypatch):
    # as-of join 자체를 검증한다 — 지금 실 BigQuery 데이터가 (user_id, category_id)당
    # 1행뿐이라 이 로직이 "골라내기"를 할 필요가 없는 상태에서는 SQL 필터/PARTITION BY
    # 버그(부호 실수, 키 누락 등)가 있어도 안 걸린다(#224류로 나중에 발견되는 잠재
    # 버그). 그래서 같은 (user_id, category_id)에 event_timestamp가 다른 행 3개를
    # 주입해 "미래 행 무시 + 시점 이하 중 최신 선택"을 직접 확인한다.
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    events_path = tmp_path / "events.csv"
    _csv_fixture_for_topic_similarity_source_tests(raw_dir, events_path)

    similarity_fixture = pd.DataFrame(
        {
            "user_id": ["u1", "u1", "u1"],
            "category_id": ["Music", "Music", "Music"],
            "event_timestamp": [
                pd.Timestamp("2026-07-01 00:00:00"),  # entity ts 이전이지만 더 오래됨
                pd.Timestamp("2026-07-08 00:00:00"),  # entity ts(07-08 12:00) 이전 중 최신 → 선택돼야 함
                pd.Timestamp("2026-07-09 00:00:00"),  # entity ts 이후 → 무시돼야 함
            ],
            "topic_similarity": [0.11, 0.55, 0.99],
        }
    )
    monkeypatch.setattr(
        build_training_dataset,
        "load_user_category_similarity_from_bigquery",
        lambda: similarity_fixture,
    )

    output_path = tmp_path / "training_dataset.csv"
    build_training_dataset.main(
        raw_dir=str(raw_dir),
        events_path=str(events_path),
        output_path=str(output_path),
        topic_similarity_source="bigquery",
    )

    result = pd.read_csv(output_path).set_index("category_id")
    assert result.loc["Music", "topic_similarity"] == pytest.approx(0.55)
    # Gaming에는 매칭되는 user_category_similarity 행이 없다 → 0.0 default.
    assert result.loc["Gaming", "topic_similarity"] == pytest.approx(0.0)


def test_main_both_asof_joins_coexist_with_bigquery_topic_similarity(tmp_path, monkeypatch):
    # 두 ASOF 공존 경로(#297 리뷰 지적). topic_similarity_source="bigquery"이고
    # videos에 video_trending_date가 있으면 COPY 쿼리에 ASOF가 두 개 걸린다:
    #   ① video_feature INNER ASOF (이번에 추가)
    #   ② user_category_similarity LEFT ASOF (#294, ①의 출력 vf.category_id를 참조)
    # 실제 GKE 재학습이 도는 구성이 바로 이 경로인데 나머지 신규 테스트는 모두
    # 기본 source라 여길 지나지 않는다. 파싱과 의미론을 함께 고정한다.
    monkeypatch.setattr(
        build_training_dataset, "load_videos_from_bigquery", _trending_snapshots_videos_df
    )
    long_events = pd.DataFrame(
        [_long_event("i1", "2026-07-09 12:00:00", "u1", "impression", "v1")]
    )
    monkeypatch.setattr(
        build_training_dataset, "load_events_from_bigquery", lambda start, end: long_events
    )
    similarity_fixture = pd.DataFrame(
        {
            "user_id": ["u1", "u1", "u1"],
            "category_id": ["Music", "Music", "Music"],
            "event_timestamp": [
                pd.Timestamp("2026-07-01 00:00:00"),
                pd.Timestamp("2026-07-08 00:00:00"),  # 이벤트(07-09) 이전 중 최신 → 선택
                pd.Timestamp("2026-07-10 00:00:00"),  # 이벤트 이후 → 무시
            ],
            "topic_similarity": [0.11, 0.55, 0.99],
        }
    )
    monkeypatch.setattr(
        build_training_dataset,
        "load_user_category_similarity_from_bigquery",
        lambda: similarity_fixture,
    )
    personas_path = tmp_path / "personas.csv"
    _single_persona_csv(personas_path)
    output_path = tmp_path / "training_dataset.csv"

    build_training_dataset.main(
        videos_source="bigquery",
        events_source="bigquery",
        topic_similarity_source="bigquery",
        events_start_date="2026-07-09",
        events_end_date="2026-07-10",
        personas_path=str(personas_path),
        output_path=str(output_path),
    )

    result = pd.read_csv(output_path)
    # ① video ASOF: 스냅샷 3건이어도 이벤트당 1행, 07-08 스냅샷(viewCount=200) 선택
    assert len(result) == 1
    assert result.iloc[0]["view_count"] == 200
    # ② similarity ASOF: ①이 고른 category_id(Music)를 기준으로 07-08 값(0.55) 선택
    assert result.iloc[0]["topic_similarity"] == pytest.approx(0.55)


def test_main_topic_similarity_source_bigquery_never_calls_vertex_ai(tmp_path, monkeypatch):
    def fail_if_called(texts, task_type):
        raise AssertionError("topic_similarity_source='bigquery'인데 embed_texts가 호출됨")

    monkeypatch.setattr(assembly_module, "embed_texts", fail_if_called)

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    events_path = tmp_path / "events.csv"
    _csv_fixture_for_topic_similarity_source_tests(raw_dir, events_path)
    monkeypatch.setattr(
        build_training_dataset,
        "load_user_category_similarity_from_bigquery",
        _empty_user_category_similarity,
    )

    output_path = tmp_path / "training_dataset.csv"
    build_training_dataset.main(
        raw_dir=str(raw_dir),
        events_path=str(events_path),
        output_path=str(output_path),
        topic_similarity_source="bigquery",
    )

    assert output_path.exists()


def test_main_topic_similarity_source_bigquery_output_schema_matches_inmemory(tmp_path, monkeypatch):
    # 두 모드의 training_dataset 물리 컬럼 집합이 미묘하게 갈라지면 안 된다
    # (예: topic_similarity_top_topic이 한쪽에만 추가되는 등) — #214는 topic_similarity
    # "값의 출처"만 바꾸고 스키마는 그대로 유지한다.
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    events_path = tmp_path / "events.csv"
    _csv_fixture_for_topic_similarity_source_tests(raw_dir, events_path)
    monkeypatch.setattr(
        build_training_dataset,
        "load_user_category_similarity_from_bigquery",
        _empty_user_category_similarity,
    )

    output_path = tmp_path / "training_dataset.csv"
    build_training_dataset.main(
        raw_dir=str(raw_dir),
        events_path=str(events_path),
        output_path=str(output_path),
        topic_similarity_source="bigquery",
    )

    result = pd.read_csv(output_path)
    assert list(result.columns) == [*MODEL_FEATURE_COLUMNS, "clicked"]


def test_main_rejects_invalid_topic_similarity_source():
    with pytest.raises(ValueError, match="topic_similarity_source"):
        build_training_dataset.main(topic_similarity_source="not-a-real-source")


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


def test_load_user_category_similarity_from_bigquery_reads_feature_dataset(monkeypatch):
    # user_category_similarity(#242가 적재)는 feature 계층(feast_offline_store)에
    # 있다 — raw 계층이 아니다.
    monkeypatch.setattr(build_training_dataset, "BIGQUERY_PROJECT", "proj")
    monkeypatch.setattr(build_training_dataset, "BIGQUERY_RAW_DATASET", "data_lake_raw")
    monkeypatch.setattr(build_training_dataset, "BIGQUERY_DATASET", "feast_offline_store")
    fake_df = pd.DataFrame({"user_id": ["u1"]})
    fake_client = _fake_bigquery(monkeypatch, fake_df)

    result = build_training_dataset.load_user_category_similarity_from_bigquery()

    assert result is fake_df
    query_text = fake_client.query.call_args[0][0]
    assert "`proj.feast_offline_store.user_category_similarity`" in query_text
    assert "data_lake_raw" not in query_text
    assert "event_timestamp" in query_text
    assert "topic_similarity" in query_text
