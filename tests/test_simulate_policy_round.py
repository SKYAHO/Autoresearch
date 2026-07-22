"""simulate_policy_round 배치 테스트 — stub Reranker + rule-based LLM."""

import numpy as np
import pandas as pd
import pytest

from autoresearch.action_logs.llm_generator import RuleBasedActionLogGenerator
from src.features.model_contract import FeatureContractError, MODEL_FEATURE_COLUMNS
from src.pipeline.simulate_policy_round import (
    _to_candidate_videos,
    build_pool_feature_frame,
    main,
)
from src.serving.service import Reranker


class _CategoryLovingModel:
    """category_id가 'Gaming'인 후보에 높은 확률을 주는 stub predict_proba."""

    def predict_proba(self, features):
        p1 = np.where(features["category_id"].astype(str) == "Gaming", 0.9, 0.1)
        return np.column_stack([1 - p1, p1])


def _videos_raw(n: int = 30) -> pd.DataFrame:
    rows = []
    for i in range(n):
        cat = "Gaming" if i % 3 == 0 else "Music"
        rows.append(
            {
                "video_id": f"v{i:03d}",
                "categoryId": cat,
                "duration": 100 + i,
                "viewCount": 1000 + i,
                "likeCount": 10,
                "commentCount": 1,
                "publishedAt": "2026-07-01",
                "title": f"{cat} video {i}",
                "description": f"{cat} 설명 {i}",
                "tags": "",
                "channelSubscriberCount": 100_000 + i,
                "channelViewCount": 10_000_000 + i,
                "channelVideoCount": 100 + i,
            }
        )
    return pd.DataFrame(rows)


def _personas(n: int = 4) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "uuid": [f"u{i}" for i in range(n)],
            "age": [25] * n,
            "occupation": ["student"] * n,
            "watch_time_band": ["night"] * n,
            "hobbies_and_interests_list": ['["gaming"]'] * n,
        }
    )


def _virtual_users(n: int = 4) -> list[dict]:
    return [
        {
            "user_id": f"u{i}",
            "age": 25,
            "occupation": "student",
            "interest_keywords": ["게임"],
            "hobby_keywords": [],
            "lifestyle_keywords": [],
            "primary_categories": ["Gaming"],
        }
        for i in range(n)
    ]


def _empty_events() -> pd.DataFrame:
    # 빈 프레임은 컬럼 dtype이 없으면 DuckDB가 INTEGER로 추론해 user_id 문자열
    # 비교가 깨진다. 실데이터(read_csv) 경로와 동일하게 문자열/정수형을 명시한다.
    return pd.DataFrame(
        columns=["event_id", "user_id", "video_id", "timestamp", "clicked", "liked", "watch_time_sec"]
    ).astype(
        {
            "event_id": "string",
            "user_id": "string",
            "video_id": "string",
            "timestamp": "string",
            "clicked": "Int64",
            "liked": "Int64",
            "watch_time_sec": "Int64",
        }
    )


def _events_with_history() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "event_id": ["e1", "e2"],
            "user_id": ["u0", "u0"],
            "video_id": ["v000", "v001"],
            "timestamp": ["2026-07-20 10:00:00", "2026-07-18 10:00:00"],
            "clicked": [1, 0],
            "liked": [1, 0],
            "watch_time_sec": [120, 0],
        }
    ).astype(
        {
            "event_id": "string",
            "user_id": "string",
            "video_id": "string",
            "timestamp": "string",
            "clicked": "Int64",
            "liked": "Int64",
            "watch_time_sec": "Int64",
        }
    )


@pytest.fixture()
def stub_reranker() -> Reranker:
    return Reranker(
        model=_CategoryLovingModel(),
        feature_columns=MODEL_FEATURE_COLUMNS,
        categorical_categories={"category_id": ("Gaming", "Music")},
    )


def test_build_pool_feature_frame_covers_model_columns(stub_reranker):
    frame = build_pool_feature_frame(
        personas=_personas(1),
        events=_empty_events(),
        videos_raw=_videos_raw(6),
        user_id="u0",
        as_of="2026-07-20 00:00:00",
    )
    assert len(frame) == 6
    for column in stub_reranker.feature_columns:
        assert column in frame.columns, column


def test_build_pool_feature_frame_includes_all_missing_contract_features():
    frame = build_pool_feature_frame(
        personas=_personas(1),
        events=_events_with_history(),
        videos_raw=_videos_raw(2),
        user_id="u0",
        as_of="2026-07-22 00:00:00",
        snapshot_date="2026-07-22",
    )

    assert set(MODEL_FEATURE_COLUMNS).issubset(frame.columns)
    assert frame.loc[0, "watch_time_band"] == "night"
    assert frame.loc[0, "recent_view_count_7d"] == 1
    assert frame.loc[0, "total_event_count_7d"] == 5
    assert frame.loc[0, "channel_subscriber_count"] == 100_000
    assert frame.loc[0, "channel_view_count"] == 10_000_000
    assert frame.loc[0, "channel_video_count"] == 100

    candidates = _to_candidate_videos(frame, MODEL_FEATURE_COLUMNS)
    assert tuple(candidates[0].features) == MODEL_FEATURE_COLUMNS


@pytest.mark.parametrize(
    "feature_columns",
    [MODEL_FEATURE_COLUMNS[:-1], MODEL_FEATURE_COLUMNS[1:] + MODEL_FEATURE_COLUMNS[:1]],
)
def test_to_candidate_videos_rejects_noncanonical_feature_contract(feature_columns):
    with pytest.raises(FeatureContractError):
        _to_candidate_videos(pd.DataFrame(), feature_columns)


def test_build_pool_feature_frame_snapshot_date_decoupled_from_as_of():
    # 영상 나이(days_since_upload)는 snapshot_date 기준, 유저 이력은 as_of 기준으로 분리한다.
    common = dict(
        personas=_personas(1),
        events=_empty_events(),
        videos_raw=_videos_raw(1),  # publishedAt 2026-07-01
        user_id="u0",
        as_of="2026-07-20 00:00:00",
    )
    explicit = build_pool_feature_frame(**common, snapshot_date="2026-07-21")
    assert explicit["days_since_upload"].iloc[0] == 20

    fallback = build_pool_feature_frame(**common)  # 기본값은 기존 동작(as_of 날짜) 유지
    assert fallback["days_since_upload"].iloc[0] == 19


def test_round_report_prefers_model_policy_when_model_is_right(tmp_path, stub_reranker):
    """모델이 유저 취향(Gaming)을 맞히면 합동 정규화 후 model CTR ≥ baseline CTR."""
    report = main(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        target_ctr=0.2,
        seed=42,
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )
    assert set(report["policies"]) == {"baseline", "model"}
    model = report["policies"]["model"]
    baseline = report["policies"]["baseline"]
    assert model["impressions"] == 6 * 4
    assert 0.0 <= report["overlap_jaccard_mean"] <= 1.0
    # rule-based generator는 관련도 기반 propensity를 주므로 Gaming만 노출한
    # model 정책의 평균 propensity가 baseline(혼합 노출) 이상이어야 한다.
    assert model["mean_click_propensity"] >= baseline["mean_click_propensity"]
    assert (tmp_path / "policy_round_report.json").is_file()
    assert (tmp_path / "event_log.parquet").is_file()


def test_round_events_are_tagged_per_policy(tmp_path, stub_reranker):
    import pyarrow.parquet as pq

    main(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        target_ctr=0.2,
        seed=42,
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )
    table = pq.read_table(tmp_path / "event_log.parquet").to_pandas()
    assert set(table["policy"].dropna().unique()) == {"baseline", "model"}
    assert (table["source"] == "online_simulated").all()
    model_imps = table[(table["policy"] == "model") & (table["event_type"] == "impression")]
    assert model_imps["ctr_score"].notna().all()
    assert (model_imps["policy_version"] == "stub-run").all()


def test_round_output_feeds_retraining_path(tmp_path, stub_reranker):
    """policy=model 필터 후 derive_wide_events가 라벨을 복원할 수 있어야 한다."""
    import pyarrow.parquet as pq

    from src.pipeline.build_training_dataset import derive_wide_events

    main(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        target_ctr=0.2,
        seed=42,
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )
    table = pq.read_table(tmp_path / "event_log.parquet").to_pandas()
    model_long = table[table["policy"] == "model"][
        ["event_id", "event_timestamp", "user_id", "event_type", "video_id", "watch_time_sec"]
    ]
    wide = derive_wide_events(model_long)
    impressions = len(model_long[model_long["event_type"] == "impression"])
    assert len(wide) == impressions
    assert wide["clicked"].sum() >= 1  # target_ctr=0.2로 클릭이 존재


def test_render_report_html_contains_policies_and_values():
    from src.pipeline.report_html import render_report_html

    report = {
        "policy_version": "run-x", "k": 10, "exploration_ratio": 0.1,
        "target_ctr": 0.02, "seed": 42, "users": 100,
        "skipped_users": [], "dropped_exposures_without_judgment": 0,
        "overlap_jaccard_mean": 0.25, "unseen_category_counts": {},
        "quarantined_chunks": 0,
        "policies": {
            "baseline": {"impressions": 1000, "clicks": 15, "ctr": 0.015,
                          "mean_click_propensity": 0.31,
                          "exploration_impressions": 0, "exploration_clicks": 0},
            "model": {"impressions": 1000, "clicks": 25, "ctr": 0.025,
                       "mean_click_propensity": 0.44,
                       "exploration_impressions": 100, "exploration_clicks": 2},
        },
    }
    html = render_report_html(report)
    assert "<!doctype html>" in html.lower()
    assert "baseline" in html and "model" in html
    assert "2.50%" in html and "1.50%" in html  # 정책별 CTR
    assert "run-x" in html
    assert "<table" in html  # 접근성용 데이터 테이블
    assert "http" not in html.split("</head>")[0]  # head에 외부 리소스 없음


def test_round_writes_html_report(tmp_path, stub_reranker):
    main(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        target_ctr=0.2,
        seed=42,
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )
    html_path = tmp_path / "policy_round_report.html"
    assert html_path.is_file()
    assert "stub-run" in html_path.read_text(encoding="utf-8")
