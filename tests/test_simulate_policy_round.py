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
        click_threshold=0.0,
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
        click_threshold=0.0,
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
        click_threshold=0.0,
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
    assert wide["clicked"].sum() >= 1  # click_threshold=0.0으로 유저별 최고 1개는 항상 클릭


def test_round_clicks_are_at_most_one_per_user(tmp_path, stub_reranker) -> None:
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
        click_threshold=0.0,
        seed=42,
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )
    table = pq.read_table(tmp_path / "event_log.parquet").to_pandas()
    clicks = table[table["event_type"] == "click"]
    per_user = clicks.groupby(["policy", "user_id"]).size()
    assert (per_user <= 1).all()


def test_render_report_html_contains_policies_and_values():
    from src.pipeline.report_html import render_report_html

    report = {
        "policy_version": "run-x", "k": 10, "exploration_ratio": 0.1,
        "click_threshold": 0.55, "seed": 42, "users": 100,
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
        click_threshold=0.0,
        seed=42,
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )
    html_path = tmp_path / "policy_round_report.html"
    assert html_path.is_file()
    assert "stub-run" in html_path.read_text(encoding="utf-8")


def test_round_dumps_drafts_and_meta(tmp_path, stub_reranker):
    """LLM 판정이 draft parquet + 사이드카 메타로 남아야 한다(캘리브레이션 입력)."""
    import json

    from autoresearch.action_logs.pipeline import read_action_log_draft_parquet
    from autoresearch.action_logs.schema import (
        ACTION_LOG_SCHEMA_VERSION,
        PROMPT_VERSION,
    )

    main(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        click_threshold=0.0,
        seed=42,
        as_of="2026-07-20 00:00:00",
        policy_version="stub-run",
        output_dir=str(tmp_path),
        input_paths={"personas": "demo/personas.csv"},
    )

    drafts = read_action_log_draft_parquet(tmp_path / "action_log_drafts.parquet")
    assert drafts
    assert all(0.0 <= d.click_propensity <= 1.0 for d in drafts)

    meta = json.loads((tmp_path / "action_log_drafts_meta.json").read_text(encoding="utf-8"))
    assert meta["llm_model"] == "fixture-rule-action-log"
    assert meta["prompt_version"] == PROMPT_VERSION
    assert meta["schema_version"] == ACTION_LOG_SCHEMA_VERSION
    assert meta["exposure_args"] == {
        "seed": 42,
        "k": 6,
        "exploration_ratio": 0.0,
        "as_of": "2026-07-20 00:00:00",
    }
    assert meta["policy_version"] == "stub-run"
    assert meta["virtual_users"] == 4
    assert meta["users"] == 4
    assert meta["drafts"] == len(drafts)
    assert meta["inputs"] == {"personas": "demo/personas.csv"}
    # click_threshold는 리플레이에서 바꾸는 값이므로 노출 인자에 없어야 한다.
    assert "click_threshold" not in meta["exposure_args"]


def test_round_meta_virtual_users_and_users_diverge_when_persona_missing(tmp_path, stub_reranker):
    """persona가 없는 유저는 skipped_users로 격리되어 virtual_users > users로 갈라진다.

    메타의 virtual_users는 입력 virtual user 수를, users는 노출 결정에 성공한
    유저 수를 각각 그대로 반영해야 한다(두 값이 뒤바뀌면 안 됨).
    """
    import json

    main(
        personas=_personas(2),  # u0, u1만 persona 보유
        virtual_users=_virtual_users(4),  # u0..u3
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        click_threshold=0.0,
        seed=42,
        as_of="2026-07-20 00:00:00",
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )

    report = json.loads((tmp_path / "policy_round_report.json").read_text(encoding="utf-8"))
    assert report["users"] == 2
    assert set(report["skipped_users"]) == {"u2", "u3"}

    meta = json.loads((tmp_path / "action_log_drafts_meta.json").read_text(encoding="utf-8"))
    assert meta["virtual_users"] == 4
    assert meta["users"] == 2
    assert meta["virtual_users"] != meta["users"]


def _run_round(tmp_path, stub_reranker, **overrides):
    """덤프까지 수행하는 표준 라운드 실행 헬퍼."""
    kwargs = dict(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        click_threshold=0.0,
        seed=42,
        as_of="2026-07-20 00:00:00",
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )
    kwargs.update(overrides)
    return main(**kwargs)


def _load_replay(round_dir):
    """덤프된 판정과 계보를 DraftReplay로 되살린다."""
    import json

    from autoresearch.action_logs.pipeline import read_action_log_draft_parquet
    from src.pipeline.simulate_policy_round import (
        DRAFTS_FILENAME,
        DRAFTS_META_FILENAME,
        DraftReplay,
    )

    meta = json.loads((round_dir / DRAFTS_META_FILENAME).read_text(encoding="utf-8"))
    return DraftReplay(
        drafts=read_action_log_draft_parquet(round_dir / DRAFTS_FILENAME),
        llm_model=str(meta["llm_model"]),
        exposure_args=meta["exposure_args"],
    )


def test_replay_reproduces_identical_round(tmp_path, stub_reranker):
    """같은 커트라인으로 리플레이하면 LLM 없이 동일한 결과가 나와야 한다."""
    first_dir = tmp_path / "a"
    original = _run_round(
        first_dir, stub_reranker, generator=RuleBasedActionLogGenerator()
    )

    replayed = _run_round(
        tmp_path / "b",
        stub_reranker,
        generator=None,
        replay=_load_replay(first_dir),
        output_dir=str(tmp_path / "b"),
    )

    assert replayed["policies"] == original["policies"]
    assert replayed["dropped_exposures_without_judgment"] == 0


def test_replay_with_higher_threshold_reduces_clicks(tmp_path, stub_reranker):
    """판정을 재사용한 채 커트라인만 올리면 클릭이 줄어야 한다(캘리브레이션 전제)."""
    first_dir = tmp_path / "a"
    original = _run_round(
        first_dir, stub_reranker, generator=RuleBasedActionLogGenerator()
    )

    strict = _run_round(
        tmp_path / "b",
        stub_reranker,
        generator=None,
        replay=_load_replay(first_dir),
        click_threshold=1.0,  # 어떤 propensity도 넘을 수 없는 커트라인
        output_dir=str(tmp_path / "b"),
    )

    assert original["policies"]["model"]["clicks"] >= 1
    assert strict["policies"]["model"]["clicks"] == 0
    assert strict["policies"]["baseline"]["clicks"] == 0


def test_replay_fails_when_drafts_do_not_cover_exposures(tmp_path, stub_reranker):
    """판정이 노출을 다 덮지 못하면 조용히 넘기지 않고 실패해야 한다."""
    first_dir = tmp_path / "a"
    _run_round(first_dir, stub_reranker, generator=RuleBasedActionLogGenerator())

    replay = _load_replay(first_dir)
    from src.pipeline.simulate_policy_round import DraftReplay

    truncated = DraftReplay(
        drafts=replay.drafts[:-1],
        llm_model=replay.llm_model,
        exposure_args=replay.exposure_args,
    )

    with pytest.raises(ValueError, match="cover"):
        _run_round(
            tmp_path / "b",
            stub_reranker,
            generator=None,
            replay=truncated,
            output_dir=str(tmp_path / "b"),
        )


def test_replay_event_log_keeps_original_llm_model(tmp_path, stub_reranker):
    """리플레이 event log의 계보는 원본 판정 모델이어야 한다."""
    import pyarrow.parquet as pq

    first_dir = tmp_path / "a"
    _run_round(
        first_dir,
        stub_reranker,
        generator=RuleBasedActionLogGenerator(model_name="judge-v9"),
    )

    second_dir = tmp_path / "b"
    _run_round(
        second_dir,
        stub_reranker,
        generator=None,
        replay=_load_replay(first_dir),
        output_dir=str(second_dir),
    )

    table = pq.read_table(second_dir / "event_log.parquet").to_pandas()
    assert set(table["llm_model"].unique()) == {"judge-v9"}


def test_main_requires_exactly_one_of_generator_or_replay(tmp_path, stub_reranker):
    with pytest.raises(ValueError, match="정확히 하나"):
        _run_round(tmp_path / "a", stub_reranker, generator=None)

    first_dir = tmp_path / "b"
    _run_round(first_dir, stub_reranker, generator=RuleBasedActionLogGenerator())
    with pytest.raises(ValueError, match="정확히 하나"):
        _run_round(
            tmp_path / "c",
            stub_reranker,
            generator=RuleBasedActionLogGenerator(),
            replay=_load_replay(first_dir),
            output_dir=str(tmp_path / "c"),
        )


def test_resolve_exposure_args_uses_defaults_without_meta():
    from src.pipeline.simulate_policy_round import resolve_exposure_args

    resolved = resolve_exposure_args(
        explicit={"seed": None, "k": 6, "exploration_ratio": None, "as_of": None},
        defaults={"seed": 42, "k": 10, "exploration_ratio": 0.1, "as_of": "now"},
        meta_exposure_args=None,
    )
    assert resolved == {"seed": 42, "k": 6, "exploration_ratio": 0.1, "as_of": "now"}


def test_resolve_exposure_args_inherits_meta_when_unspecified():
    from src.pipeline.simulate_policy_round import resolve_exposure_args

    meta = {"seed": 7, "k": 6, "exploration_ratio": 0.0, "as_of": "2026-07-20 00:00:00"}
    resolved = resolve_exposure_args(
        explicit={"seed": None, "k": None, "exploration_ratio": None, "as_of": None},
        defaults={"seed": 42, "k": 10, "exploration_ratio": 0.1, "as_of": "now"},
        meta_exposure_args=meta,
    )
    assert resolved == meta


def test_resolve_exposure_args_rejects_mismatch():
    from src.pipeline.simulate_policy_round import resolve_exposure_args

    meta = {"seed": 7, "k": 6, "exploration_ratio": 0.0, "as_of": "2026-07-20 00:00:00"}
    with pytest.raises(ValueError, match="seed"):
        resolve_exposure_args(
            explicit={"seed": 42, "k": None, "exploration_ratio": None, "as_of": None},
            defaults={"seed": 42, "k": 10, "exploration_ratio": 0.1, "as_of": "now"},
            meta_exposure_args=meta,
        )


def test_read_drafts_meta_requires_sidecar(tmp_path):
    from src.pipeline.simulate_policy_round import _read_drafts_meta

    with pytest.raises(FileNotFoundError, match="llm_model"):
        _read_drafts_meta(tmp_path / "action_log_drafts_meta.json")


def test_cli_replay_runs_without_generator(tmp_path, stub_reranker, monkeypatch):
    """CLI 리플레이는 --generator 없이 메타에서 인자를 상속해 동작해야 한다."""
    import json
    import sys

    import pyarrow as pa
    import pyarrow.parquet as pq

    from src.pipeline import simulate_policy_round as module

    # 입력 파일 준비
    personas_path = tmp_path / "personas.csv"
    _personas().to_csv(personas_path, index=False)
    videos_path = tmp_path / "videos.csv"
    _videos_raw().to_csv(videos_path, index=False)
    events_path = tmp_path / "events.csv"
    # 빈 프레임을 CSV로 왕복시키면 dtype이 전부 object로 추론돼 DuckDB의
    # user_id 비교가 깨진다. 실데이터와 같은 형태로 이력이 있는 프레임을 쓴다.
    _events_with_history().to_csv(events_path, index=False)
    users_path = tmp_path / "virtual_users.parquet"
    pq.write_table(pa.Table.from_pylist(_virtual_users()), users_path)

    monkeypatch.setattr(module, "load_reranker", lambda settings: stub_reranker)
    monkeypatch.setattr(module, "load_model_settings_from_environment", lambda: None)

    round_a = tmp_path / "round_a"
    argv = [
        "prog",
        "--personas", str(personas_path),
        "--virtual-users", str(users_path),
        "--videos", str(videos_path),
        "--events", str(events_path),
        "--generator", "rule-based",
        "--click-threshold", "0.0",
        "--k", "6",
        "--exploration-ratio", "0.0",
        "--as-of", "2026-07-20 00:00:00",
        "--output-dir", str(round_a),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    module._cli()

    meta = json.loads(
        (round_a / "action_log_drafts_meta.json").read_text(encoding="utf-8")
    )
    assert meta["exposure_args"]["k"] == 6

    # 리플레이 — k/seed/as-of/generator 모두 생략하고 메타에서 상속한다
    round_b = tmp_path / "round_b"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--personas", str(personas_path),
            "--virtual-users", str(users_path),
            "--videos", str(videos_path),
            "--events", str(events_path),
            "--replay-drafts", str(round_a / "action_log_drafts.parquet"),
            "--click-threshold", "0.0",
            "--output-dir", str(round_b),
        ],
    )
    module._cli()

    original = json.loads((round_a / "policy_round_report.json").read_text(encoding="utf-8"))
    replayed = json.loads((round_b / "policy_round_report.json").read_text(encoding="utf-8"))
    assert replayed["policies"] == original["policies"]


def test_cli_replay_rejects_generator_flag(tmp_path, monkeypatch):
    import sys

    from src.pipeline import simulate_policy_round as module

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--personas", "p.csv", "--virtual-users", "u.parquet",
            "--videos", "v.csv", "--events", "e.csv",
            "--replay-drafts", str(tmp_path / "action_log_drafts.parquet"),
            "--generator", "rule-based",
            "--click-threshold", "0.5",
        ],
    )
    with pytest.raises(SystemExit):
        module._cli()


def test_report_records_replay_provenance(tmp_path, stub_reranker):
    """산출물만 보고 원본 판정 라운드와 리플레이를 구분할 수 있어야 한다."""
    first_dir = tmp_path / "a"
    original = _run_round(
        first_dir,
        stub_reranker,
        generator=RuleBasedActionLogGenerator(model_name="judge-v9"),
    )
    assert original["replay"] is False
    assert original["llm_model"] == "judge-v9"

    second_dir = tmp_path / "b"
    replayed = _run_round(
        second_dir,
        stub_reranker,
        generator=None,
        replay=_load_replay(first_dir),
        output_dir=str(second_dir),
    )
    assert replayed["replay"] is True
    assert replayed["llm_model"] == "judge-v9"

    html = (second_dir / "policy_round_report.html").read_text(encoding="utf-8")
    assert "judge-v9" in html
    assert "replay" in html
