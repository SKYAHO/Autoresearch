"""모델 노출 조립 provider 단위 테스트 — 실 BQ 미접속(fake client)."""

import random
from datetime import date

import pandas as pd
import pytest

from src.pipeline.model_exposure_provider import (
    RankedVideo,
    RankingsPartition,
    build_model_exposures,
    load_user_rankings,
)


def _videos(n: int = 40) -> list[dict]:
    return [
        {
            "video_id": f"v{i:03d}",
            "title": f"title {i}",
            "description": f"desc {i}",
            "tags": [],
            "view_count": 1000 - i,  # v000이 최고 인기
        }
        for i in range(n)
    ]


def _ranking(n: int = 30) -> list[RankedVideo]:
    # 모델 순위: v039부터 역순(인기와 어긋나게) — 슬롯 출처 구분 가능
    return [
        RankedVideo(video_id=f"v{39 - i:03d}", rank=i + 1, ctr_score=0.9 - i * 0.01)
        for i in range(n)
    ]


def _sources(meta: dict) -> dict[str, int]:
    counts: dict[str, int] = {"model": 0, "trending": 0, "random": 0}
    for m in meta.values():
        counts[m.exposure_source] += 1
    return counts


def test_default_slots_are_17_model_5_trending_2_random():
    candidates, meta = build_model_exposures(
        "u1", _ranking(), _videos(), random.Random(42), model_run_id="run-a"
    )
    assert len(candidates) == 24
    assert _sources(meta) == {"model": 17, "trending": 5, "random": 2}


def test_model_slots_follow_rank_and_carry_score_and_lineage():
    _, meta = build_model_exposures(
        "u1", _ranking(), _videos(), random.Random(42), model_run_id="run-a"
    )
    model_rows = {m.rank: m for m in meta.values() if m.exposure_source == "model"}
    assert sorted(model_rows) == list(range(1, 18))  # rank 1..17
    assert model_rows[1].ctr_score == pytest.approx(0.9)
    assert all(m.policy == "model" for m in meta.values())
    assert all(m.policy_version == "run-a" for m in meta.values())
    trending = [m for m in meta.values() if m.exposure_source == "trending"]
    assert all(m.ctr_score is None for m in trending)
    randoms = [m for m in meta.values() if m.exposure_source == "random"]
    assert all(m.is_exploration for m in randoms)


def test_trending_overlap_with_model_falls_to_next_popular():
    # 모델 상위가 인기 상위(v000~)와 겹치도록 모델 순위를 인기순과 동일하게 구성
    ranking = [
        RankedVideo(video_id=f"v{i:03d}", rank=i + 1, ctr_score=0.5) for i in range(17)
    ]
    candidates, meta = build_model_exposures(
        "u1", ranking, _videos(), random.Random(42), model_run_id="run-a"
    )
    video_ids = [str(v["video_id"]) for v in candidates]
    assert len(video_ids) == len(set(video_ids))  # 중복 없음
    trending_ids = {
        vid for (uid, vid), m in meta.items() if m.exposure_source == "trending"
    }
    assert trending_ids == {"v017", "v018", "v019", "v020", "v021"}  # 다음 인기 5개


def test_shortfall_fills_from_trending_then_random_with_true_tags():
    candidates, meta = build_model_exposures(
        "u1", _ranking(5), _videos(), random.Random(42), model_run_id="run-a"
    )
    assert len(candidates) == 24
    counts = _sources(meta)
    assert counts["model"] == 5  # 모델로 위장하지 않음
    assert counts["model"] + counts["trending"] + counts["random"] == 24


def test_user_without_recommendations_gets_trending_and_random_only():
    candidates, meta = build_model_exposures(
        "u1", [], _videos(), random.Random(42), model_run_id="run-a"
    )
    assert len(candidates) == 24
    assert _sources(meta)["model"] == 0


def test_missing_video_join_skips_to_next_rank():
    ranking = [RankedVideo(video_id="missing", rank=1, ctr_score=0.9)] + _ranking(20)
    _, meta = build_model_exposures(
        "u1", ranking, _videos(), random.Random(42), model_run_id="run-a"
    )
    assert ("u1", "missing") not in meta
    assert _sources(meta)["model"] == 17


def test_deterministic_for_same_rng_seed():
    first, _ = build_model_exposures(
        "u1", _ranking(), _videos(), random.Random("s:u1"), model_run_id="run-a"
    )
    second, _ = build_model_exposures(
        "u1", _ranking(), _videos(), random.Random("s:u1"), model_run_id="run-a"
    )
    assert [v["video_id"] for v in first] == [v["video_id"] for v in second]


class _FakeQueryJob:
    def __init__(self, frame: pd.DataFrame):
        self._frame = frame

    def to_dataframe(self) -> pd.DataFrame:
        return self._frame


class _FakeClient:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.queries: list[str] = []

    def query(self, query: str) -> _FakeQueryJob:
        self.queries.append(query)
        return _FakeQueryJob(self.frame)


def _rankings_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "user_id": ["u1", "u1", "u2"],
            "video_id": ["v001", "v002", "v001"],
            "rank": [1, 2, 1],
            "ctr_score": [0.9, 0.8, 0.7],
            "model_run_id": ["run-a"] * 3,
        }
    )


def test_load_user_rankings_groups_by_user_in_rank_order():
    client = _FakeClient(_rankings_frame())
    partition = load_user_rankings(client, "p.d.user_recommendations", date(2026, 7, 22))
    assert [rv.video_id for rv in partition.by_user["u1"]] == ["v001", "v002"]
    assert partition.by_user["u2"][0].rank == 1
    assert partition.model_run_id == "run-a"
    assert "dt = '2026-07-22'" in client.queries[0]


def test_load_user_rankings_fails_fast_on_empty_partition():
    client = _FakeClient(_rankings_frame().iloc[0:0])
    with pytest.raises(RuntimeError, match="2026-07-22"):
        load_user_rankings(client, "p.d.user_recommendations", date(2026, 7, 22))
