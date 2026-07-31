"""다중 시드 반복 학습과 유의성 판정 검증 (#407).

#396은 시드 42 하나로 1회씩만 돌려 +0.0019(임계 +0.002)로 기각했다. 임계와의
차이가 0.0001이라 시드가 달랐으면 판정이 뒤집혔을 수 있는데 그걸 알 방법이
없었다. bbungjun 님의 Capability probe round_002는 실제로 뒤집힌 사례다 —
+2.63pp로 "채택"이 났지만 시드 5개 중 2개만 baseline을 넘었고 평균은 baseline
아래였다.
"""

from __future__ import annotations

import math

import pytest

from src.pipeline.seed_sweep import (
    MetricSummary,
    SignificanceVerdict,
    compare_to_baseline,
    run_seed_sweep,
    summarize_metric,
)


# --- 요약 통계 ---


def test_summarize_metric_reports_mean_std_and_range() -> None:
    summary = summarize_metric([0.70, 0.72, 0.74])

    assert summary.n == 3
    assert summary.mean == pytest.approx(0.72)
    # 표본 표준편차(ddof=1) — 시드 3개는 표본이지 모집단이 아니다.
    assert summary.std == pytest.approx(0.02)
    assert summary.minimum == pytest.approx(0.70)
    assert summary.maximum == pytest.approx(0.74)


def test_summarize_metric_single_value_has_undefined_std() -> None:
    """시드 1개면 편차를 잴 수 없다 — 0으로 위장하면 노이즈가 없는 것처럼 보인다."""
    summary = summarize_metric([0.70])

    assert summary.n == 1
    assert summary.mean == pytest.approx(0.70)
    assert math.isnan(summary.std)


def test_summarize_metric_rejects_empty() -> None:
    with pytest.raises(ValueError):
        summarize_metric([])


# --- 유의성 판정 ---


def test_compare_to_baseline_flags_difference_inside_noise() -> None:
    """편차가 큰데 차이가 작으면 '노이즈 범위 안'으로 판정한다."""
    baseline = summarize_metric([0.70, 0.75, 0.80])
    candidate = summarize_metric([0.71, 0.76, 0.81])

    verdict = compare_to_baseline(candidate=candidate, baseline=baseline)

    assert verdict.delta == pytest.approx(0.01)
    assert verdict.within_noise is True


def test_compare_to_baseline_flags_difference_outside_noise() -> None:
    """편차가 작고 차이가 크면 '노이즈 밖'으로 판정한다."""
    baseline = summarize_metric([0.700, 0.701, 0.699])
    candidate = summarize_metric([0.800, 0.801, 0.799])

    verdict = compare_to_baseline(candidate=candidate, baseline=baseline)

    assert verdict.delta == pytest.approx(0.1, abs=1e-6)
    assert verdict.within_noise is False


def test_compare_to_baseline_is_undecidable_with_single_seed() -> None:
    """시드 1개짜리는 편차를 모르므로 판정하지 않는다 — #396이 그 상태였다."""
    baseline = summarize_metric([0.70])
    candidate = summarize_metric([0.75])

    verdict = compare_to_baseline(candidate=candidate, baseline=baseline)

    assert verdict.delta == pytest.approx(0.05)
    assert verdict.within_noise is None
    assert "시드" in verdict.reason


def test_same_delta_flips_verdict_depending_on_seed_spread() -> None:
    """같은 Δ라도 시드 간 편차에 따라 판정이 갈린다 — #407이 필요한 이유다.

    #396은 시드 1개로 +0.0019를 재고 임계 +0.002에 0.0001 모자라 기각했다.
    그 Δ가 편차 대비 큰지 작은지는 시드를 늘려야만 알 수 있는데, 아래 두
    경우가 정확히 그 차이를 보여준다.
    """
    # 시드 간 흔들림이 작으면 같은 Δ가 노이즈 밖이다.
    tight = compare_to_baseline(
        candidate=summarize_metric([0.7465, 0.7455, 0.7470]),
        baseline=summarize_metric([0.7446, 0.7440, 0.7452]),
    )
    # 흔들림이 크면 같은 Δ가 노이즈에 묻힌다.
    noisy = compare_to_baseline(
        candidate=summarize_metric([0.7565, 0.7355, 0.7470]),
        baseline=summarize_metric([0.7546, 0.7340, 0.7452]),
    )

    assert tight.delta == pytest.approx(noisy.delta, abs=1e-3)
    assert tight.within_noise is False
    assert noisy.within_noise is True


# --- 반복 실행 ---


def test_run_seed_sweep_trains_once_per_seed_and_summarizes() -> None:
    calls = []

    def fake_train(*, random_state, **kwargs):
        calls.append(random_state)
        return {42: 0.70, 43: 0.72, 44: 0.74}[random_state]

    result = run_seed_sweep([42, 43, 44], train_once=fake_train)

    assert calls == [42, 43, 44]
    assert result.seeds == [42, 43, 44]
    assert result.metrics == [0.70, 0.72, 0.74]
    assert result.summary.mean == pytest.approx(0.72)


def test_run_seed_sweep_rejects_empty_seed_list() -> None:
    with pytest.raises(ValueError):
        run_seed_sweep([], train_once=lambda **_: 0.0)


def test_run_seed_sweep_rejects_duplicate_seeds() -> None:
    """같은 시드를 두 번 돌리면 같은 결과가 나와 편차가 인위적으로 작아진다."""
    with pytest.raises(ValueError):
        run_seed_sweep([42, 42], train_once=lambda **_: 0.0)


def test_seed_sweep_result_serializes_for_the_issue_template() -> None:
    """이슈 본문 결과 항목에 그대로 옮길 수 있어야 한다(#407 완료조건 4)."""
    result = run_seed_sweep([42, 43], train_once=lambda *, random_state, **_: 0.70)

    payload = result.to_dict()

    assert payload["seeds"] == [42, 43]
    assert payload["metric_name"] == "val_roc_auc"
    assert "mean" in payload["summary"]
    assert "std" in payload["summary"]


def test_metric_summary_is_frozen() -> None:
    summary = MetricSummary(n=1, mean=0.5, std=float("nan"), minimum=0.5, maximum=0.5)
    with pytest.raises(Exception):
        summary.mean = 0.6  # type: ignore[misc]


def test_significance_verdict_carries_reason() -> None:
    baseline = summarize_metric([0.70, 0.71])
    candidate = summarize_metric([0.90, 0.91])

    verdict: SignificanceVerdict = compare_to_baseline(
        candidate=candidate, baseline=baseline
    )

    assert verdict.reason
