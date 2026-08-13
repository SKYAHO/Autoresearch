"""build_pool_feature_frame_feast (서빙 pool의 offline PIT 조립) 단위 테스트 (#359 A1).

실제 feast/BigQuery 없이 glue만 검증한다: spine 구성(유저 상수·영상당 1행·
event_timestamp tz-aware UTC), 서빙 후처리(cold-start만, gap 드롭 금지), 결과 컬럼.
staged 조회 자체는 tests/test_feast_retrieval_integration_feast.py가 실물(로컬 File
store)로 검증한다.
"""

import pandas as pd
import pytest

from autoresearch.feature_engineering import feast_retrieval
from autoresearch.feature_engineering.model_contract import (
    CATEGORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
)


def _fake_retrieve(captured: dict):
    """retrieve_training_features 대역. spine을 기록하고 영상당 1행 조회 결과를 모사한다.

    v2는 UserDynamic 전 피처 null(콜드 유저 gap) + category_id null(영상 미발견)로 두어
    cold-start 대상이 되게 한다.
    """

    def _inner(store, spine, *, service=feast_retrieval.DEFAULT_SERVICE):
        captured["spine"] = spine.copy()
        captured["service"] = service
        rows = []
        for video_id in spine["video_id"]:
            row = {c: 0 for c in MODEL_FEATURE_COLUMNS}
            for c in CATEGORICAL_FEATURE_COLUMNS:
                row[c] = "Gaming"
            row["video_id"] = video_id
            if video_id == "v2":
                for c in feast_retrieval._USER_DYNAMIC_COLUMNS:
                    row[c] = None
                row["category_id"] = None  # 영상 미발견
            rows.append(row)
        # 일부러 순서를 뒤집어 반환한다 — get_historical_features가 ORDER BY 없이
        # 뒤섞어 줄 수 있는 상황을 흉내내, reindex가 후보 순서로 되돌리는지 검증한다.
        return pd.DataFrame(rows[::-1])

    return _inner


def test_builds_spine_one_row_per_candidate_utc(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        feast_retrieval, "retrieve_training_features", _fake_retrieve(captured)
    )

    out = feast_retrieval.build_pool_feature_frame_feast(
        store=object(),
        user_id="u1",
        candidate_video_ids=["v1", "v2", "v3"],
        as_of="2026-07-20 00:00:00",
    )

    spine = captured["spine"]
    # 유저는 상수, 영상은 pool 순서대로 1행씩.
    assert spine["user_id"].unique().tolist() == ["u1"]
    assert spine["video_id"].tolist() == ["v1", "v2", "v3"]
    # event_timestamp: tz-aware UTC, as_of와 동일 시각(학습 spine과 정렬).
    ts = spine["event_timestamp"]
    assert ts.dt.tz is not None
    assert (ts == pd.Timestamp("2026-07-20 00:00:00", tz="UTC")).all()
    # 결과: video_id + 21피처, 영상당 1행. fake가 뒤집어 반환해도 reindex가 후보
    # 순서로 되돌리므로 항상 [v1, v2, v3](결정론적 순서).
    assert out.columns.tolist() == ["video_id", *MODEL_FEATURE_COLUMNS]
    assert out["video_id"].tolist() == ["v1", "v2", "v3"]


def test_serving_postprocess_cold_start_only_no_drop(monkeypatch) -> None:
    # §설계결정 3 회귀 가드: UserDynamic 전 null(콜드 유저)이어도 드롭하지 않고
    # cold-start로 채워 전 후보를 남긴다 — 학습의 drop_user_dynamic_gap_rows와 반대.
    captured: dict = {}
    monkeypatch.setattr(
        feast_retrieval, "retrieve_training_features", _fake_retrieve(captured)
    )

    out = feast_retrieval.build_pool_feature_frame_feast(
        store=object(),
        user_id="u1",
        candidate_video_ids=["v1", "v2"],
        as_of="2026-07-20 00:00:00",
    )

    # v2(콜드 유저 gap)가 드롭되지 않고 남아 있어야 한다.
    assert out["video_id"].tolist() == ["v1", "v2"]
    v2 = out[out["video_id"] == "v2"].iloc[0]
    # cold-start 규칙(카테고리→'unknown', 수치→0)으로 채워짐.
    assert v2["category_id"] == "unknown"
    assert v2["historical_category_affinity"] == "unknown"
    assert v2["recent_click_count_7d"] == 0
    assert v2["total_event_count_7d"] == 0


def test_diagnostics_reports_cold_start_signals(monkeypatch) -> None:
    # cold-start로 채우기 **전** 결손 신호를 diagnostics로 노출한다(서빙 관측용, #381 리뷰).
    def _all_cold(store, spine, *, service=feast_retrieval.DEFAULT_SERVICE):
        rows = []
        for video_id in spine["video_id"]:
            row = {c: 0 for c in MODEL_FEATURE_COLUMNS}
            for c in CATEGORICAL_FEATURE_COLUMNS:
                row[c] = "Gaming"
            row["video_id"] = video_id
            for c in feast_retrieval._USER_DYNAMIC_COLUMNS:  # 유저 동적 전량 결손
                row[c] = None
            if video_id == "v2":
                row["category_id"] = None  # 영상 미발견 1건
            rows.append(row)
        return pd.DataFrame(rows)

    monkeypatch.setattr(feast_retrieval, "retrieve_training_features", _all_cold)
    diag: dict = {}
    feast_retrieval.build_pool_feature_frame_feast(
        store=object(),
        user_id="u1",
        candidate_video_ids=["v1", "v2"],
        as_of="2026-07-20 00:00:00",
        diagnostics=diag,
    )
    assert diag["user_dynamic_cold"] is True  # UserDynamic 전 피처 null
    assert diag["video_missing"] == 1  # v2만 category_id null
    assert diag["pool_size"] == 2


def test_batched_frames_split_reindex_and_aggregate(monkeypatch) -> None:
    # (유저 × 후보) spine 1회 조회 → 유저별 프레임 dict. 각 프레임은 후보 순서로 reindex되고,
    # diagnostics는 유저 전체 집계(cold_start_users/video_missing/pool_total)를 채운다(#359 C1).
    def _fake(store, spine, *, service=feast_retrieval.DEFAULT_SERVICE):
        rows = []
        for _, r in spine.iterrows():
            row = {c: 0 for c in MODEL_FEATURE_COLUMNS}
            for c in CATEGORICAL_FEATURE_COLUMNS:
                row[c] = "Gaming"
            row["user_id"] = r["user_id"]
            row["video_id"] = r["video_id"]
            if r["user_id"] == "u2":  # u2는 UserDynamic 전량 결손(cold)
                for c in feast_retrieval._USER_DYNAMIC_COLUMNS:
                    row[c] = None
            if r["video_id"] == "v3":  # v3는 영상 미발견
                row["category_id"] = None
            rows.append(row)
        return pd.DataFrame(rows[::-1])  # 뒤집어 반환 → reindex가 후보 순서로 되돌리는지 검증

    monkeypatch.setattr(feast_retrieval, "retrieve_training_features", _fake)
    diag: dict = {}
    frames = feast_retrieval.build_pool_feature_frames_feast(
        store=object(),
        user_ids=["u1", "u2"],
        candidate_video_ids=["v1", "v2", "v3"],
        as_of="2026-07-20 00:00:00",
        diagnostics=diag,
    )
    assert set(frames) == {"u1", "u2"}
    for user in ("u1", "u2"):
        assert frames[user].columns.tolist() == ["video_id", *MODEL_FEATURE_COLUMNS]
        assert frames[user]["video_id"].tolist() == ["v1", "v2", "v3"]  # 후보 순서 결정론
    # 집계: u2만 cold → 1명, v3 미발견 × 2유저 = 2, pool_total = 2×3.
    assert diag["cold_start_users"] == 1
    assert diag["video_missing"] == 2
    assert diag["pool_total"] == 6


def test_batched_user_absent_from_result_gets_cold_start_frame(monkeypatch) -> None:
    # by_user.get(user, empty) 폴백 분기(#384 리뷰 4): 조회 결과에서 한 유저의 행이 통째로
    # 빠져도(File store 드롭 등) 그 유저는 전 후보 cold-start 프레임으로 채점 대상에 남고,
    # 집계에서 cold(전 UserDynamic null)로 잡힌다.
    def _fake(store, spine, *, service=feast_retrieval.DEFAULT_SERVICE):
        rows = []
        for _, r in spine.iterrows():
            if r["user_id"] == "u_absent":
                continue  # 이 유저 행을 통째로 드롭
            row = {c: 0 for c in MODEL_FEATURE_COLUMNS}
            for c in CATEGORICAL_FEATURE_COLUMNS:
                row[c] = "Gaming"
            row["user_id"] = r["user_id"]
            row["video_id"] = r["video_id"]
            rows.append(row)
        return pd.DataFrame(rows)

    monkeypatch.setattr(feast_retrieval, "retrieve_training_features", _fake)
    diag: dict = {}
    frames = feast_retrieval.build_pool_feature_frames_feast(
        store=object(),
        user_ids=["u1", "u_absent"],
        candidate_video_ids=["v1", "v2"],
        as_of="2026-07-20 00:00:00",
        diagnostics=diag,
    )
    # 빠진 유저도 dict에 있고, 후보 전부를 cold-start로 채운 프레임(드롭 아님).
    assert set(frames) == {"u1", "u_absent"}
    absent = frames["u_absent"]
    assert absent["video_id"].tolist() == ["v1", "v2"]
    assert absent["category_id"].tolist() == ["unknown", "unknown"]  # 영상 피처 cold-start
    assert (absent["recent_click_count_7d"] == 0).all()  # UserDynamic cold-start
    # 집계: u_absent가 cold 1명, 그의 2행 모두 미발견 → video_missing에 2 기여.
    assert diag["cold_start_users"] == 1
    assert diag["video_missing"] == 2  # u_absent의 v1,v2 (u1은 발견)
    assert diag["pool_total"] == 4


def test_missing_feature_raises(monkeypatch) -> None:
    # 조회 결과에 모델 피처가 빠지면 조용히 넘기지 않고 즉시 실패.
    monkeypatch.setattr(
        feast_retrieval,
        "retrieve_training_features",
        lambda store, spine, *, service=feast_retrieval.DEFAULT_SERVICE: pd.DataFrame(
            {"video_id": ["v1"], "category_id": ["Gaming"]}
        ),
    )
    with pytest.raises(ValueError, match="누락된 모델 피처"):
        feast_retrieval.build_pool_feature_frame_feast(
            store=object(),
            user_id="u1",
            candidate_video_ids=["v1"],
            as_of="2026-07-20 00:00:00",
        )
