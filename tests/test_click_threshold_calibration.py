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
