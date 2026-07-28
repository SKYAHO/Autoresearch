"""build_pool_feature_frame_feast (서빙 pool의 offline PIT 조립) 단위 테스트 (#359 A1).

실제 feast/BigQuery 없이 glue만 검증한다: spine 구성(유저 상수·영상당 1행·
event_timestamp tz-aware UTC), 서빙 후처리(cold-start만, gap 드롭 금지), 결과 컬럼.
staged 조회 자체는 tests/test_feast_retrieval_integration_feast.py가 실물(로컬 File
store)로 검증한다.
"""

import pandas as pd
import pytest

from src.features import feast_retrieval
from src.features.model_contract import (
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
