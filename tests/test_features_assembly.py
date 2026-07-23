"""src/features/assembly.py 공용 피처 조립 함수 단위 테스트."""

import tempfile

import numpy as np
import pandas as pd

import src.features.assembly as assembly_module
from src.features.assembly import (
    compute_interaction_columns,
    compute_point_in_time_user_features,
    compute_user_offline_features,
    compute_user_topic_features,
    compute_video_features,
    connect_duckdb,
    parse_primary_categories,
)


def test_connect_duckdb_applies_memory_limit_and_temp_directory(monkeypatch):
    """대형 정렬/조인이 디스크로 spill하도록 memory_limit/temp_directory가
    실제 연결에 반영되는지 검증(#271/#291 OOM 회귀 방지)."""
    monkeypatch.setattr(assembly_module, "DUCKDB_MEMORY_LIMIT", "500MB")
    monkeypatch.setattr(assembly_module, "DUCKDB_TEMP_DIR", tempfile.gettempdir())
    con = connect_duckdb()
    try:
        mem = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
        tmp = con.execute("SELECT current_setting('temp_directory')").fetchone()[0]
        # DuckDB는 '500MB'를 '476.8 MiB' 등으로 정규화하므로 값이 시스템
        # RAM의 ~80% 기본값이 아니라 우리가 건 상한임을 바이트로 확인한다.
        assert mem not in ("", None)
        assert "MiB" in mem or "MB" in mem  # GiB 규모 기본값이 아님
        assert tmp != ""  # spill 대상 디렉터리가 설정됨
    finally:
        con.close()


def _videos_raw() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "video_id": ["v1", "v2"],
            "categoryId": ["Gaming", "Music"],
            "duration": [120, None],
            "viewCount": [1000, 0],
            "likeCount": [100, 5],
            "commentCount": [10, 1],
            "publishedAt": ["2026-07-01", "2026-07-10"],
        }
    )


def test_compute_video_features_columns_and_values():
    out = compute_video_features(_videos_raw(), "2026-07-11")
    assert list(out.columns) == [
        "video_id", "category_id", "duration_sec", "view_count",
        "like_ratio", "comment_ratio", "days_since_upload",
        "channel_subscriber_count", "channel_view_count", "channel_video_count",
    ]
    v1 = out[out["video_id"] == "v1"].iloc[0]
    assert v1["duration_sec"] == 120
    assert v1["like_ratio"] == 0.1
    assert v1["days_since_upload"] == 10
    # channelSubscriberCount 등 원본 컬럼이 없으면 0으로 default 처리된다.
    assert v1["channel_subscriber_count"] == 0
    assert v1["channel_view_count"] == 0
    assert v1["channel_video_count"] == 0
    v2 = out[out["video_id"] == "v2"].iloc[0]
    assert v2["duration_sec"] == 300  # COALESCE 기본값
    assert pd.isna(v2["like_ratio"])  # viewCount=0 → NULLIF → NULL


def test_compute_video_features_channel_stats_when_present():
    videos = _videos_raw()
    videos["channelSubscriberCount"] = [50_000, None]
    videos["channelViewCount"] = [1_000_000, 2_000_000]
    videos["channelVideoCount"] = [42, None]
    out = compute_video_features(videos, "2026-07-11")
    v1 = out[out["video_id"] == "v1"].iloc[0]
    assert v1["channel_subscriber_count"] == 50_000
    assert v1["channel_view_count"] == 1_000_000
    assert v1["channel_video_count"] == 42
    v2 = out[out["video_id"] == "v2"].iloc[0]
    assert v2["channel_subscriber_count"] == 0  # null → 0 default
    assert v2["channel_video_count"] == 0


def test_compute_user_offline_features_age_group_buckets():
    personas = pd.DataFrame(
        {"uuid": ["u1", "u2", "u3"], "age": [19, 34, 60], "occupation": ["s", "o", "r"]}
    )
    out = compute_user_offline_features(personas)
    assert list(out.columns) == ["user_id", "age_group", "occupation", "watch_time_band"]
    assert out["age_group"].tolist() == ["10s", "30s", "50s+"]
    # watch_time_band 컬럼이 없으면 전부 "unknown" default 처리된다.
    assert out["watch_time_band"].tolist() == ["unknown", "unknown", "unknown"]


def test_compute_user_offline_features_watch_time_band_normalization():
    personas = pd.DataFrame(
        {
            "uuid": ["u1", "u2", "u3", "u4", "u5"],
            "age": [25, 25, 25, 25, 25],
            "occupation": ["s", "s", "s", "s", "s"],
            "watch_time_band": ["오전", "PM", "night", "mixed", None],
        }
    )
    out = compute_user_offline_features(personas)
    assert out["watch_time_band"].tolist() == [
        "morning", "evening", "night", "unknown", "unknown",
    ]


def test_compute_point_in_time_user_features_respects_as_of():
    # u1: as_of 이전 클릭 1건(Gaming) → affinity=Gaming, count=1. as_of 이후 이벤트는 무시.
    events = pd.DataFrame(
        {
            "event_id": ["e1", "e2"],
            "user_id": ["u1", "u1"],
            "video_id": ["v1", "v2"],
            "timestamp": ["2026-07-10 10:00:00", "2026-07-12 10:00:00"],
            "clicked": [1, 1],
            "liked": [0, 1],
            "watch_time_sec": [60, 30],
        }
    )
    query_points = pd.DataFrame(
        {"user_id": ["u1"], "as_of": ["2026-07-11 00:00:00"], "tag": ["q1"]}
    )
    out = compute_point_in_time_user_features(events, _videos_raw(), query_points)
    row = out.iloc[0]
    assert row["tag"] == "q1"  # carry 컬럼 보존
    assert row["historical_category_affinity"] == "Gaming"
    assert row["recent_click_count_7d"] == 1
    assert row["recent_watch_time_7d"] == 60
    assert row["recent_like_count_7d"] == 0
    # as_of(07-11) 이전 7일 윈도우 안에는 e1(07-10, watch_time_sec=60>0)만 포함 → view 1건.
    assert row["recent_view_count_7d"] == 1
    # e1 근사치: impression(1) + clicked(1) + view(1) + liked(0) = 3.
    assert row["total_event_count_7d"] == 3


def test_compute_point_in_time_user_features_excludes_snapshot_day_itself():
    # 스펙(data-warehouse.md): 일 단위 snapshot이라 스냅샷 당일(as_of의 날) 행동은
    # [d-7, d) window 밖이라 반영되지 않는다("07-05 00:00~10:00 사이 행동은 미반영").
    events = pd.DataFrame(
        {
            "event_id": ["e1"],
            "user_id": ["u1"],
            "video_id": ["v1"],
            "timestamp": ["2026-07-11 08:00:00"],  # as_of와 같은 날(스냅샷 당일)
            "clicked": [1],
            "liked": [0],
            "watch_time_sec": [60],
        }
    )
    query_points = pd.DataFrame({"user_id": ["u1"], "as_of": ["2026-07-11 10:00:00"]})
    out = compute_point_in_time_user_features(events, _videos_raw(), query_points)
    row = out.iloc[0]
    assert row["recent_click_count_7d"] == 0
    assert row["recent_view_count_7d"] == 0
    assert row["total_event_count_7d"] == 0
    # affinity 30일 window도 당일을 제외하므로 이력 없음 → unknown.
    assert row["historical_category_affinity"] == "unknown"


def test_compute_point_in_time_user_features_snapshot_day_no_events_but_prior_activity():
    # 스냅샷 당일엔 이벤트 0건이지만 직전 6일 내 활동이 있는 케이스(시뮬레이션 경로
    # 스타일: query_points가 이벤트가 아님). daily 위에서만 window를 돌리면 이
    # (user, day) 행이 없어 조용히 0으로 떨어지는데, spine 0-패딩으로 정상 계산되어야
    # 한다(#284 — window-over-daily였다면 못 잡는 silent bug).
    events = pd.DataFrame(
        {
            "event_id": ["e1"],
            "user_id": ["u1"],
            "video_id": ["v1"],
            "timestamp": ["2026-07-08 10:00:00"],  # 스냅샷(07-11)보다 3일 전
            "clicked": [1],
            "liked": [0],
            "watch_time_sec": [60],
        }
    )
    query_points = pd.DataFrame({"user_id": ["u1"], "as_of": ["2026-07-11 00:00:00"]})
    out = compute_point_in_time_user_features(events, _videos_raw(), query_points)
    row = out.iloc[0]
    # 07-08은 [07-04, 07-10] window 안 → 반영돼야 한다. 0이면 spine 패딩 누락 회귀.
    assert row["recent_click_count_7d"] == 1
    assert row["recent_view_count_7d"] == 1
    assert row["recent_watch_time_7d"] == 60
    assert row["total_event_count_7d"] == 3
    assert row["historical_category_affinity"] == "Gaming"


def test_compute_point_in_time_user_features_affinity_respects_30_day_bound():
    # historical_category_affinity는 스펙상 30일 윈도우다. 30일 밖 클릭은 제외된다
    # (기존 코드는 하한이 없어 전체 히스토리를 봤던 스펙 위반 — 이번에 수정).
    events = pd.DataFrame(
        {
            "event_id": ["e_old", "e_new"],
            "user_id": ["u1", "u1"],
            "video_id": ["v1", "v2"],  # v1=Gaming, v2=Music
            "timestamp": ["2026-06-15 10:00:00", "2026-07-20 10:00:00"],  # 46일 전(밖), 11일 전(안)
            "clicked": [1, 1],
            "liked": [0, 0],
            "watch_time_sec": [0, 0],
        }
    )
    query_points = pd.DataFrame({"user_id": ["u1"], "as_of": ["2026-07-31 00:00:00"]})
    out = compute_point_in_time_user_features(events, _videos_raw(), query_points)
    # 30일 window [07-01, 07-31) 안엔 Music(07-20)만 → Gaming(06-15)은 제외.
    assert out.iloc[0]["historical_category_affinity"] == "Music"


def test_compute_interaction_columns_matches():
    joined = pd.DataFrame(
        {
            "hobbies_and_interests_list": ['["gaming"]'],
            "historical_category_affinity": ["Gaming"],
            "category_id": ["Gaming"],
        }
    )
    out = compute_interaction_columns(joined)
    assert out["historical_category_match"].iloc[0] == 1
    assert out["preferred_category_match"].iloc[0] in (0, 1)
    assert 0.0 <= abs(out["topic_similarity"].iloc[0]) <= 1.0


def test_compute_interaction_columns_embeds_each_unique_keyword_only_once(monkeypatch):
    # 같은 유저가 여러 impression 행(row)에 걸쳐 등장해도, Vertex AI에는 유니크
    # 키워드 집합만 한 번씩 요청해야 한다(#206) — 행 개수만큼 반복 요청하면 안 됨.
    calls = []

    def fake_embed_texts(texts, task_type):
        calls.append(list(texts))
        return [np.zeros(768) for _ in texts]

    monkeypatch.setattr(assembly_module, "embed_texts", fake_embed_texts)

    joined = pd.DataFrame(
        {
            # 같은 유저(같은 키워드 세트)가 3개 행에 걸쳐 등장.
            "hobbies_and_interests_list": ['["gaming", "music"]'] * 3,
            "historical_category_affinity": ["Gaming", "Music", "Gaming"],
            "category_id": ["Gaming", "Music", "Gaming"],
        }
    )
    compute_interaction_columns(joined)

    assert len(calls) == 1  # embed_texts는 딱 한 번만 호출
    assert sorted(calls[0]) == ["gaming", "music"]  # 유니크 키워드만, dedup됨


def test_compute_interaction_columns_uses_primary_categories_when_present():
    # primary_categories 컬럼이 있으면 키워드 매핑 mock 대신 실제 값을 그대로 쓴다.
    joined = pd.DataFrame(
        {
            "hobbies_and_interests_list": ['["unmapped_keyword"]'],
            "primary_categories": ['["Sports", "Music"]'],
            "historical_category_affinity": ["Sports"],
            "category_id": ["Sports"],
        }
    )
    out = compute_interaction_columns(joined)
    assert out["preferred_category"].iloc[0] == ["Sports", "Music"]
    assert out["preferred_category_match"].iloc[0] == 1  # category_id="Sports" ∈ preferred_category


def test_compute_interaction_columns_falls_back_without_primary_categories():
    # primary_categories 컬럼이 없으면(구식 mock) 기존 키워드 매핑으로 fallback한다.
    joined = pd.DataFrame(
        {
            "hobbies_and_interests_list": ['["gaming"]'],
            "historical_category_affinity": ["Gaming"],
            "category_id": ["Gaming"],
        }
    )
    out = compute_interaction_columns(joined)
    assert out["preferred_category"].iloc[0] == ["Gaming"]


def test_compute_user_topic_features_shape_and_values():
    personas = pd.DataFrame(
        {
            "uuid": ["u1", "u2"],
            "hobbies_and_interests_list": ['["gaming"]', '["music"]'],
            "primary_categories": ['["Gaming"]', '["Music"]'],
        }
    )
    out = compute_user_topic_features(personas, ["Gaming", "Music"])

    assert len(out) == 4  # 2 personas x 2 categories
    assert set(out.columns) == {
        "user_id",
        "category_id",
        "topic_similarity",
        "preferred_category_match",
    }

    u1_gaming = out[(out["user_id"] == "u1") & (out["category_id"] == "Gaming")].iloc[0]
    assert u1_gaming["preferred_category_match"] == 1
    u1_music = out[(out["user_id"] == "u1") & (out["category_id"] == "Music")].iloc[0]
    assert u1_music["preferred_category_match"] == 0


def test_compute_user_topic_features_embeds_each_unique_keyword_only_once(monkeypatch):
    # persona 단위(3명)로 계산하므로, 이벤트 수(수백만)와 무관하게 유니크
    # 키워드 집합만 한 번씩 임베딩해야 한다(#206/#240).
    calls = []

    def fake_embed_texts(texts, task_type):
        calls.append(list(texts))
        return [np.zeros(768) for _ in texts]

    monkeypatch.setattr(assembly_module, "embed_texts", fake_embed_texts)

    personas = pd.DataFrame(
        {
            "uuid": ["u1", "u2", "u3"],
            "hobbies_and_interests_list": ['["gaming", "music"]'] * 3,
        }
    )
    compute_user_topic_features(personas, ["Gaming", "Music"])

    assert len(calls) == 1
    assert sorted(calls[0]) == ["gaming", "music"]


def test_compute_user_topic_features_falls_back_without_primary_categories():
    personas = pd.DataFrame(
        {
            "uuid": ["u1"],
            "hobbies_and_interests_list": ['["gaming"]'],
        }
    )
    out = compute_user_topic_features(personas, ["Gaming", "Music"])
    assert out[out["category_id"] == "Gaming"]["preferred_category_match"].iloc[0] == 1
    assert out[out["category_id"] == "Music"]["preferred_category_match"].iloc[0] == 0


def test_compute_user_topic_features_matches_compute_interaction_columns(monkeypatch):
    # 유저 단위 선계산 경로(#240)가 기존 이벤트 단위 compute_interaction_columns()와
    # 정확히 같은 topic_similarity/preferred_category_match 값을 내야 한다 — 조인
    # 순서만 바뀌었을 뿐 계산 결과는 동일해야 한다.
    def fake_embed_texts(texts, task_type):
        return [np.full(768, float(len(t))) for t in texts]

    monkeypatch.setattr(assembly_module, "embed_texts", fake_embed_texts)

    personas = pd.DataFrame(
        {
            "uuid": ["u1", "u2"],
            "hobbies_and_interests_list": ['["gaming", "music"]', '["travel"]'],
            "primary_categories": ['["Gaming"]', '["Travel & Events"]'],
        }
    )
    events_joined = pd.DataFrame(
        {
            "user_id": ["u1", "u1", "u2"],
            "category_id": ["Gaming", "Music", "Travel & Events"],
            "hobbies_and_interests_list": [
                '["gaming", "music"]', '["gaming", "music"]', '["travel"]'
            ],
            "primary_categories": ['["Gaming"]', '["Gaming"]', '["Travel & Events"]'],
            "historical_category_affinity": ["unknown", "unknown", "unknown"],
        }
    )

    expected = compute_interaction_columns(events_joined)

    user_topic = compute_user_topic_features(personas, events_joined["category_id"].unique())
    merged = events_joined.merge(user_topic, on=["user_id", "category_id"], how="left")

    assert list(merged["topic_similarity"]) == list(expected["topic_similarity"])
    assert list(merged["preferred_category_match"]) == list(expected["preferred_category_match"])


def test_compute_user_topic_features_skip_embedding_never_calls_embed_texts(monkeypatch):
    # skip_embedding=True(#214, topic_similarity_source="bigquery" 호출부용)는
    # topic_similarity를 BigQuery에서 별도로 가져오므로 embed_texts(Vertex AI)를
    # 절대 호출하면 안 된다 — 호출되면 즉시 실패하도록 예외를 던지는 스텁으로 교체.
    def fail_if_called(texts, task_type):
        raise AssertionError("skip_embedding=True인데 embed_texts가 호출됨")

    monkeypatch.setattr(assembly_module, "embed_texts", fail_if_called)

    personas = pd.DataFrame(
        {
            "uuid": ["u1", "u2"],
            "hobbies_and_interests_list": ['["gaming"]', '["music"]'],
            "primary_categories": ['["Gaming"]', '["Music"]'],
        }
    )
    out = compute_user_topic_features(personas, ["Gaming", "Music"], skip_embedding=True)

    assert out["topic_similarity"].isna().all()


def test_compute_user_topic_features_skip_embedding_preserves_preferred_category_match():
    # preferred_category_match 산출 로직은 skip_embedding 값과 무관하게 완전히
    # 동일해야 한다(#245/#246과 같은 로직 drift를 피하려고 별도 함수로 복제하지
    # 않고 같은 함수를 공유하기로 한 결정 — #214).
    personas = pd.DataFrame(
        {
            "uuid": ["u1", "u2"],
            "hobbies_and_interests_list": ['["gaming"]', '["music"]'],
            "primary_categories": ['["Gaming"]', '["Music"]'],
        }
    )
    with_embedding = compute_user_topic_features(personas, ["Gaming", "Music"])
    without_embedding = compute_user_topic_features(personas, ["Gaming", "Music"], skip_embedding=True)

    assert list(without_embedding["preferred_category_match"]) == list(
        with_embedding["preferred_category_match"]
    )


def test_parse_primary_categories_from_json_string():
    assert parse_primary_categories('["Gaming", "Music"]') == ["Gaming", "Music"]


def test_parse_primary_categories_from_list():
    assert parse_primary_categories(["Gaming", "Music"]) == ["Gaming", "Music"]


def test_parse_primary_categories_filters_out_of_vocabulary_values():
    # LLM이 vocabulary 밖 값을 산출하는 vocab drift 상황을 방어한다.
    assert parse_primary_categories('["Gaming", "NotACategory"]') == ["Gaming"]


def test_parse_primary_categories_handles_null_and_empty():
    assert parse_primary_categories(None) == []
    assert parse_primary_categories(float("nan")) == []
    assert parse_primary_categories("") == []
    assert parse_primary_categories("[]") == []
