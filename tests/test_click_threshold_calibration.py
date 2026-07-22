import pytest

from autoresearch.action_logs.calibration import recommend_click_threshold


def test_recommends_threshold_hitting_target_ctr() -> None:
    # 10 유저, 100 노출, target 3% → n_click=3 → 3번째 큰 값(0.7)이 커트라인.
    per_user_max = [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.05]
    rec = recommend_click_threshold(per_user_max, impressions=100, target_ctr=0.03)
    assert rec.recommended_threshold == 0.7
    assert rec.achieved_ctr == 0.03
    assert rec.users == 10
    assert rec.impressions == 100


def test_sweep_ctr_is_monotone_non_increasing() -> None:
    per_user_max = [0.9, 0.7, 0.5, 0.3, 0.1]
    rec = recommend_click_threshold(per_user_max, impressions=100, target_ctr=0.02)
    ctrs = [ctr for _, ctr in rec.sweep]
    assert ctrs == sorted(ctrs, reverse=True)


def test_errors_when_target_exceeds_ceiling() -> None:
    # ceiling = users/impressions = 10/100 = 0.1
    with pytest.raises(ValueError):
        recommend_click_threshold([0.5] * 10, impressions=100, target_ctr=0.2)


def test_errors_on_zero_target() -> None:
    with pytest.raises(ValueError):
        recommend_click_threshold([0.5] * 10, impressions=100, target_ctr=0.0)


def test_errors_on_empty_input() -> None:
    with pytest.raises(ValueError):
        recommend_click_threshold([], impressions=0, target_ctr=0.02)


def test_ties_at_boundary_overshoot_target_ctr() -> None:
    # n_click=round(0.02*100)=2 → threshold=values[1]=0.7, but 3 users tie at 0.7
    # so clickers=4 (>=0.7) and achieved_ctr=0.04 overshoots target 0.02. Documented.
    rec = recommend_click_threshold(
        [0.9, 0.7, 0.7, 0.7, 0.5], impressions=100, target_ctr=0.02
    )
    assert rec.recommended_threshold == 0.7
    assert rec.achieved_ctr == 0.04


def test_recommendation_reports_per_user_max_quantiles() -> None:
    rec = recommend_click_threshold(
        [0.9, 0.8, 0.7, 0.6, 0.5], impressions=100, target_ctr=0.02
    )
    q = rec.per_user_max_quantiles
    assert set(q) == {"p50", "p75", "p90", "p95", "p99"}
    assert q["p50"] == 0.7  # median of [0.5,0.6,0.7,0.8,0.9]
    assert q["p99"] == 0.9
