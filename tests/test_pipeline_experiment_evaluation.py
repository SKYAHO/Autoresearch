"""실험 승격 증거 정책의 순수 단위 테스트."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.pipeline.experiment_evaluation import (
    EvaluationReasonCode,
    EvaluationVerdict,
    PairedSeedObservation,
    PromotionDecisionRecord,
    create_experiment_plan,
    create_paired_seed_evidence,
    decide_promotion,
    evaluate_experiment,
    promotion_policy_v1,
)
from src.pipeline.training_provenance import TrainingComparisonManifest, TrainingSeeds


PLAN_TIME = datetime(2026, 8, 1, tzinfo=timezone.utc)
POLICY_SEEDS = tuple(range(42, 72))
_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64


def _comparison(
    seed: int,
    *,
    snapshot_sha256: str = _SHA_A,
    effective_seeds: TrainingSeeds | None | object = ...,
    validated_at: datetime = PLAN_TIME + timedelta(minutes=1),
) -> TrainingComparisonManifest:
    if effective_seeds is ...:
        effective_seeds = TrainingSeeds(
            split_seed=seed,
            model_seed=seed,
            sampler_seed=seed,
        )
    return TrainingComparisonManifest(
        comparison_id=f"comparison-{seed}",
        baseline_run_id=f"baseline-{seed}",
        challenger_run_id=f"challenger-{seed}",
        baseline_snapshot_sha256=snapshot_sha256,
        challenger_snapshot_sha256=snapshot_sha256,
        baseline_snapshot_manifest_sha256=_SHA_B,
        challenger_snapshot_manifest_sha256=_SHA_B,
        baseline_split_manifest_sha256=f"{seed:064x}",
        challenger_split_manifest_sha256=f"{seed:064x}",
        baseline_feature_columns_sha256=_SHA_C,
        challenger_feature_columns_sha256=_SHA_D,
        baseline_feature_columns=("feature",),
        challenger_feature_columns=("feature", "challenger_feature"),
        effective_seeds=effective_seeds,
        validated_at=validated_at,
    )


def _evidence(
    plan_id: str,
    *,
    deltas: tuple[float, ...] = (0.010, 0.011) * 15,
    seeds: tuple[int, ...] = POLICY_SEEDS,
    comparison_overrides: dict[int, TrainingComparisonManifest] | None = None,
):
    observations = tuple(
        PairedSeedObservation(
            seed=seed,
            baseline_roc_auc=0.80,
            challenger_roc_auc=0.80 + delta,
            comparison=(comparison_overrides or {}).get(seed, _comparison(seed)),
        )
        for seed, delta in zip(seeds, deltas)
    )
    return create_paired_seed_evidence(plan_id=plan_id, observations=observations)


def _plan(*, candidate_ids: tuple[str, ...] = ("candidate-sha",), created_at=PLAN_TIME):
    return create_experiment_plan(
        hypothesis_id="issue-466-h1",
        control_id="lgbm-2026-08-01",
        candidate_ids=candidate_ids,
        created_at=created_at,
    )


def test_policy_v1_is_exact_and_not_caller_configurable() -> None:
    policy = promotion_policy_v1()

    assert policy.policy_version == "promotion-policy-v1"
    assert policy.primary_metric == "roc_auc"
    assert policy.required_seeds == POLICY_SEEDS
    assert policy.require_paired_comparison is True


def test_v1_marks_positive_paired_30_seed_evidence_eligible() -> None:
    plan = _plan()
    evidence = _evidence(plan.plan_id)

    evaluation = evaluate_experiment(plan, evidence, evaluated_at=PLAN_TIME)
    decision = decide_promotion(evaluation, decided_at=PLAN_TIME)

    assert evaluation.metric_name == "roc_auc"
    assert evaluation.required_seeds == POLICY_SEEDS
    assert evaluation.paired is True
    assert evaluation.confidence_interval_lower is not None
    assert evaluation.confidence_interval_lower > 0
    assert evaluation.verdict is EvaluationVerdict.ELIGIBLE
    assert evaluation.reason_codes == (
        EvaluationReasonCode.PRIMARY_ROC_AUC_IMPROVED_WITH_95PCT_CONFIDENCE,
    )
    assert decision.verdict is EvaluationVerdict.ELIGIBLE
    assert decision.evaluation_id == evaluation.evaluation_id


def test_v1_rejects_when_upper_confidence_bound_is_not_positive() -> None:
    plan = _plan()
    evidence = _evidence(plan.plan_id, deltas=(-0.020, -0.019) * 15)

    evaluation = evaluate_experiment(plan, evidence, evaluated_at=PLAN_TIME)

    assert evaluation.verdict is EvaluationVerdict.REJECT
    assert evaluation.confidence_interval_upper is not None
    assert evaluation.confidence_interval_upper <= 0
    assert evaluation.reason_codes == (EvaluationReasonCode.PRIMARY_ROC_AUC_NOT_IMPROVED,)


def test_v1_holds_when_primary_roc_auc_is_inconclusive() -> None:
    plan = _plan()
    evidence = _evidence(plan.plan_id, deltas=(-0.020, 0.020) * 15)

    evaluation = evaluate_experiment(plan, evidence, evaluated_at=PLAN_TIME)

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.reason_codes == (EvaluationReasonCode.PRIMARY_ROC_AUC_INCONCLUSIVE,)


def test_v1_holds_for_a_seed_list_that_differs_from_policy() -> None:
    plan = _plan()
    evidence = _evidence(plan.plan_id, seeds=tuple(range(43, 73)))

    evaluation = evaluate_experiment(plan, evidence, evaluated_at=PLAN_TIME)

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.paired is False
    assert evaluation.reason_codes == (EvaluationReasonCode.SEED_POLICY_MISMATCH,)


def test_v1_holds_for_unpaired_effective_seed_evidence() -> None:
    plan = _plan()
    mismatched = _comparison(
        42,
        effective_seeds=TrainingSeeds(split_seed=42, model_seed=43, sampler_seed=42),
    )
    evidence = _evidence(plan.plan_id, comparison_overrides={42: mismatched})

    evaluation = evaluate_experiment(plan, evidence, evaluated_at=PLAN_TIME)

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.reason_codes == (EvaluationReasonCode.UNPAIRED_SEED_EVIDENCE,)


def test_v1_holds_for_a_multi_candidate_plan() -> None:
    plan = _plan(candidate_ids=("candidate-a", "candidate-b"))
    evidence = _evidence(plan.plan_id)

    evaluation = evaluate_experiment(plan, evidence, evaluated_at=PLAN_TIME)

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.reason_codes == (
        EvaluationReasonCode.MULTIPLE_CANDIDATES_REQUIRE_INDEPENDENT_HOLDOUT,
    )


def test_v1_holds_for_legacy_comparison_without_effective_seed_evidence() -> None:
    plan = _plan()
    legacy = _comparison(42, effective_seeds=None)
    evidence = _evidence(plan.plan_id, comparison_overrides={42: legacy})

    evaluation = evaluate_experiment(plan, evidence, evaluated_at=PLAN_TIME)

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.reason_codes == (EvaluationReasonCode.EFFECTIVE_SEEDS_MISSING,)


def test_v1_holds_when_comparisons_use_different_snapshots() -> None:
    plan = _plan()
    other_snapshot = _comparison(42, snapshot_sha256="e" * 64)
    evidence = _evidence(plan.plan_id, comparison_overrides={42: other_snapshot})

    evaluation = evaluate_experiment(plan, evidence, evaluated_at=PLAN_TIME)

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.reason_codes == (EvaluationReasonCode.SNAPSHOT_MISMATCH,)


def test_v1_holds_when_plan_was_not_predeclared_before_comparison() -> None:
    plan = _plan(created_at=PLAN_TIME + timedelta(minutes=2))
    evidence = _evidence(plan.plan_id)

    evaluation = evaluate_experiment(plan, evidence, evaluated_at=PLAN_TIME)

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.reason_codes == (EvaluationReasonCode.PLAN_NOT_PREDECLARED,)


def test_decision_record_is_deterministic_and_excludes_registry_coordinates() -> None:
    plan = _plan()
    evidence = _evidence(plan.plan_id)
    first = evaluate_experiment(plan, evidence, evaluated_at=PLAN_TIME)
    second = evaluate_experiment(plan, evidence, evaluated_at=PLAN_TIME)

    record = PromotionDecisionRecord(
        evaluation=first,
        decision=decide_promotion(first, decided_at=PLAN_TIME),
    )

    assert first.model_dump_json() == second.model_dump_json()
    assert record.decision.evaluation_id == record.evaluation.evaluation_id
    assert '"tracking_uri"' not in record.model_dump_json()
    assert '"champion_alias"' not in record.model_dump_json()
