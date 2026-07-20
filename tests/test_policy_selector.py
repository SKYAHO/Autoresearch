"""select_exposures Top-K + exploration 선택기 단위 테스트."""

import random

import pytest

from src.pipeline.policy_selector import Exposure, select_exposures
from src.serving.schemas import RerankedVideo


def _ranked(n: int) -> list[RerankedVideo]:
    # 점수 내림차순 n개 (v0이 최고점)
    return [RerankedVideo(video_id=f"v{i}", ctr_score=1.0 - i * 0.01) for i in range(n)]


def test_exploitation_takes_top_scores_in_order():
    out = select_exposures(_ranked(20), k=10, exploration_ratio=0.0, rng=random.Random(1))
    assert [e.video_id for e in out] == [f"v{i}" for i in range(10)]
    assert [e.rank for e in out] == list(range(1, 11))
    assert all(e.is_exploration is False for e in out)


def test_exploration_slots_come_from_non_topk():
    out = select_exposures(_ranked(100), k=10, exploration_ratio=0.2, rng=random.Random(1))
    assert len(out) == 10
    explore = [e for e in out if e.is_exploration]
    exploit = [e for e in out if not e.is_exploration]
    assert len(explore) == 2  # round(10 * 0.2)
    assert len(exploit) == 8
    exploit_ids = {e.video_id for e in exploit}
    assert exploit_ids == {f"v{i}" for i in range(8)}
    # exploration은 exploitation 이후 순위·비-Top-K 출신
    assert all(int(e.video_id[1:]) >= 8 for e in explore)
    assert [e.rank for e in out] == list(range(1, 11))


def test_deterministic_given_same_seed():
    a = select_exposures(_ranked(50), k=10, exploration_ratio=0.3, rng=random.Random(7))
    b = select_exposures(_ranked(50), k=10, exploration_ratio=0.3, rng=random.Random(7))
    assert a == b


def test_k_at_least_pool_exposes_everything_without_exploration():
    out = select_exposures(_ranked(5), k=10, exploration_ratio=0.5, rng=random.Random(1))
    assert len(out) == 5
    assert all(e.is_exploration is False for e in out)


def test_invalid_arguments_raise():
    with pytest.raises(ValueError):
        select_exposures(_ranked(5), k=0, exploration_ratio=0.1, rng=random.Random(1))
    with pytest.raises(ValueError):
        select_exposures(_ranked(5), k=3, exploration_ratio=1.5, rng=random.Random(1))
