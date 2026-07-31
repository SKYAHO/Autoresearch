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
    _T_CRITICAL_95,
    MetricSummary,
    SeedSweepError,
    _t_critical_95,
    paired_deltas_by_seed,
    validate_seeds,
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
    # 시드 간 흔들림이 작으면 같은 Δ(=0.1)가 노이즈 밖이다.
    tight = compare_to_baseline(
        candidate=summarize_metric([0.801, 0.800, 0.799]),
        baseline=summarize_metric([0.701, 0.700, 0.699]),
    )
    # 흔들림이 크면 같은 Δ가 노이즈에 묻힌다.
    noisy = compare_to_baseline(
        candidate=summarize_metric([0.85, 0.75, 0.80]),
        baseline=summarize_metric([0.75, 0.65, 0.70]),
    )

    assert tight.delta == pytest.approx(0.1, abs=1e-6)
    assert noisy.delta == pytest.approx(0.1, abs=1e-6)
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


# --- 리뷰 반영: 시드 짝지음 · t 임계값 · 부분 결과 (#407 리뷰) ---


def test_paired_comparison_is_more_sensitive_than_unpaired() -> None:
    """같은 시드로 두 조건을 돌렸으면 분할 노이즈는 짝지어 빼면 상쇄된다(#407 리뷰 2).

    아래 두 조건은 세 시드에서 **같은 방향으로 함께** 움직이고 시드별 Δ가 매우
    안정적이다. 짝짓지 않으면 그 공통 흔들림이 분모에 남아 "노이즈 안"으로
    둔감해진다 — #396류 판정에서 결론이 갈리는 지점이다.
    """
    baseline_values = [0.7546, 0.7340, 0.7452]
    candidate_values = [0.7565, 0.7355, 0.7470]
    deltas = [c - b for c, b in zip(candidate_values, baseline_values)]

    unpaired = compare_to_baseline(
        candidate=summarize_metric(candidate_values),
        baseline=summarize_metric(baseline_values),
    )
    paired = compare_to_baseline(
        candidate=summarize_metric(candidate_values),
        baseline=summarize_metric(baseline_values),
        paired_deltas=deltas,
    )

    assert unpaired.within_noise is True
    assert paired.within_noise is False
    # 짝지으면 표준오차가 훨씬 작아진다.
    assert paired.standard_error < unpaired.standard_error


def test_paired_deltas_by_seed_requires_identical_seed_lists() -> None:
    """짝지을 수 없는데 짝지은 척하면 판정이 낙관적으로 기운다."""
    a = run_seed_sweep([42, 43], train_once=lambda *, random_state, **_: 0.7)
    b = run_seed_sweep([42, 44], train_once=lambda *, random_state, **_: 0.7)

    with pytest.raises(ValueError):
        paired_deltas_by_seed(candidate=a, baseline=b)


def test_paired_deltas_by_seed_pairs_in_order() -> None:
    a = run_seed_sweep([42, 43], train_once=lambda *, random_state, **_: {42: 0.80, 43: 0.82}[random_state])
    b = run_seed_sweep([42, 43], train_once=lambda *, random_state, **_: {42: 0.78, 43: 0.79}[random_state])

    deltas = paired_deltas_by_seed(candidate=a, baseline=b)

    assert deltas == pytest.approx([0.02, 0.03])


def test_threshold_uses_t_critical_not_fixed_two_sigma() -> None:
    """시드 3개(자유도 2)의 임계값은 2.0이 아니라 4.303이어야 한다(#407 리뷰 3).

    고정 2.0을 쓰면 작은 표본에서 '노이즈 밖'을 실제보다 자주 선언한다 —
    거짓 채택을 막으려는 모듈이 관대한 쪽으로 기우는 셈이다.
    """
    deltas = [0.0019, 0.0015, 0.0018]
    verdict = compare_to_baseline(
        candidate=summarize_metric([0.75, 0.76, 0.77]),
        baseline=summarize_metric([0.74, 0.75, 0.76]),
        paired_deltas=deltas,
    )

    paired = summarize_metric(deltas)
    expected_se = paired.std / math.sqrt(3)
    assert verdict.standard_error == pytest.approx(expected_se)
    assert verdict.threshold == pytest.approx(4.303 * expected_se)


def test_paired_comparison_needs_two_seeds() -> None:
    verdict = compare_to_baseline(
        candidate=summarize_metric([0.75]),
        baseline=summarize_metric([0.74]),
        paired_deltas=[0.01],
    )

    assert verdict.within_noise is None


def test_run_seed_sweep_keeps_completed_metrics_on_failure() -> None:
    """중간에 죽어도 이미 끝난 시드 결과가 남는다 — 학습 1회가 비싸다(#407 리뷰 4)."""

    def flaky(*, random_state, **_):
        if random_state == 44:
            raise RuntimeError("학습 실패(시뮬레이션)")
        return {42: 0.70, 43: 0.72}[random_state]

    with pytest.raises(SeedSweepError) as excinfo:
        run_seed_sweep([42, 43, 44], train_once=flaky)

    assert excinfo.value.completed == {42: 0.70, 43: 0.72}
    assert "44" in str(excinfo.value)


def test_validate_seeds_can_run_before_side_effects() -> None:
    """호출부가 디렉토리 생성 전에 부를 수 있어야 한다(#407 리뷰 5)."""
    validate_seeds([42, 43])
    with pytest.raises(ValueError):
        validate_seeds([])
    with pytest.raises(ValueError):
        validate_seeds([42, 42])


# --- NaN 지표 가드 (#445) ---


def test_run_seed_sweep_rejects_nan_metric() -> None:
    """학습이 NaN 지표를 돌려주면 즉시 실패한다(#445).

    `TrainingOutcome.val_roc_auc`의 기본값이 NaN이라, 학습을 거치지 않은 outcome이
    섞이면 평균·편차가 조용히 NaN이 된다. 요약이 NaN이면 판정도 NaN이라 "노이즈
    안/밖"이 아니라 아무 말도 못 하는 결과가 나온다.
    """
    with pytest.raises(SeedSweepError) as excinfo:
        run_seed_sweep([42, 43], train_once=lambda *, random_state, **_: float("nan"))

    assert "42" in str(excinfo.value)


def test_run_seed_sweep_rejects_infinite_metric() -> None:
    with pytest.raises(SeedSweepError):
        run_seed_sweep([42], train_once=lambda *, random_state, **_: float("inf"))


def test_run_seed_sweep_nan_error_keeps_earlier_seeds() -> None:
    """NaN으로 멈춰도 앞선 시드 결과는 남는다."""

    def flaky(*, random_state, **_):
        return 0.70 if random_state == 42 else float("nan")

    with pytest.raises(SeedSweepError) as excinfo:
        run_seed_sweep([42, 43], train_once=flaky)

    assert excinfo.value.completed == {42: 0.70}


def test_summarize_metric_rejects_nan() -> None:
    """통계 계층도 NaN을 받지 않는다 — 평균·편차가 조용히 NaN이 되는 것을 막는다."""
    with pytest.raises(ValueError):
        summarize_metric([0.70, float("nan")])


def test_t_critical_table_covers_realistic_seed_counts_without_approximating() -> None:
    """df 1~30은 표에서 정확히 조회된다 — 이 구간에 근사가 끼면 안 된다.

    빈칸을 남기면 `reason` 문자열에 "경계(±t2.228, df=14)"처럼 **df와 맞지 않는
    t값**이 짝지어 기록된다. 판정 근거를 만드는 모듈이라 그 기록 자체가 틀리면
    안 된다(#456 리뷰 1).
    """
    assert set(_T_CRITICAL_95) == set(range(1, 31))
    for degrees_of_freedom in range(1, 31):
        assert _t_critical_95(degrees_of_freedom) == _T_CRITICAL_95[degrees_of_freedom]


def test_t_critical_beyond_the_table_holds_the_most_conservative_entry() -> None:
    """표를 넘는 자유도는 하한(df=30)을 유지한다.

    1.96(정규 근사)으로 낮추면 df 31~60 구간에서 실제보다 관대해진다 — t(40)=2.023이라
    1.96은 참값보다 작다. 2.042를 유지하면 오차가 4.19%로 수렴하며 방향은 늘 보수적이다.
    """
    for degrees_of_freedom in (31, 39, 100, 1000):
        assert _t_critical_95(degrees_of_freedom) == _T_CRITICAL_95[30]


def test_t_critical_rejects_degrees_of_freedom_below_the_table() -> None:
    """자유도 1 미만은 호출부 계약이 깨진 신호라 조용히 흡수하지 않는다.

    두 호출부 모두 시드 2개 이상(df >= 1)을 보장하므로 실제로는 도달하지 않는다.
    도달했다면 그 보장이 깨진 것이므로 12.706을 돌려주는 대신 즉시 세운다.
    """
    with pytest.raises(ValueError, match="호출부 계약"):
        _t_critical_95(0)


def test_t_critical_never_declares_outside_noise_more_easily_than_the_true_value() -> None:
    """자유도별 임계값이 참 t값 이상이어야 한다 — 회귀 방지용 성질 검사.

    표를 채우든 비우든 이 성질만은 유지돼야 하므로 구현 방식이 아니라 **의도**를
    고정한다. 참값은 scipy 없이 검증할 수 있도록 박아둔다(양측 95%, 소수 3자리).
    이 PR 이전 코드는 df 11~14에서 2.131을 돌려줘 이 성질을 깼다.
    """
    true_t = {
        11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
        16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093,
        21: 2.080, 25: 2.060, 29: 2.045,
        # 표 밖 — 근사가 남은 유일한 구간이라 여기가 실질적인 회귀 위험 지점이다.
        39: 2.023, 60: 2.000, 100: 1.984,
    }
    for degrees_of_freedom, expected in true_t.items():
        used = _t_critical_95(degrees_of_freedom)
        assert used >= expected, (
            f"df={degrees_of_freedom}: 임계값 {used}가 참값 {expected}보다 작다 — "
            "경계가 실제보다 좁아져 유의 판정이 쉬워진다."
        )
