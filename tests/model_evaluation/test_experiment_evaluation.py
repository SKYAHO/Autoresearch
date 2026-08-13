"""실험 승격 증거 정책의 순수 단위 테스트."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from autoresearch.model_evaluation import experiment_evaluation
from autoresearch.model_evaluation.experiment_evaluation import (
    EvaluationConfidence,
    EvaluationReasonCode,
    EvaluationVerdict,
    ExperimentEvaluation,
    PairedSeedObservation,
    TemporalSignal,
    PromotionDecisionRecord,
    create_experiment_plan,
    create_paired_seed_evidence,
    decide_promotion,
    evaluate_experiment,
    promotion_policy_v1,
)
from autoresearch.model_evaluation.promotion_evidence import (
    ExperimentPlanReceipt,
    GcsObjectReceipt,
    HeldOutMetricEvidence,
    HeldOutMetricReceipt,
    PromotionEvidenceValidationError,
)
from autoresearch.model_training.training_provenance import (
    TrainingComparisonManifest,
    TrainingSeeds,
    VerifiedComparisonPromotionEvidence,
)
from autoresearch.model_evaluation.training_comparison import ComparisonValidationError


PLAN_TIME = datetime(2026, 8, 1, tzinfo=timezone.utc)
# 프로덕션 상수를 import하지 않고 기대값을 여기에 고정한다. import하면 아래
# `required_seeds == POLICY_SEEDS` 단언이 자기 자신과 비교하는 공허한 검사가 된다.
# 프로덕션이 바뀌면 이 테스트가 실패해야 하고, 그 실패가 "의도한 변경인가"를 묻는다.
# 데모 스코프 축소(#574)로 30개에서 3개로 줄였다.
POLICY_SEEDS = tuple(range(42, 45))
_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64


@pytest.fixture(autouse=True)
def _use_verified_comparison_test_double(monkeypatch):
    """evaluator 단위 테스트에서는 Task 3 verifier의 network I/O만 대체한다."""

    def _revalidate(
        comparison: TrainingComparisonManifest, *, promotion_evidence_store: object
    ) -> TrainingComparisonManifest:
        return comparison

    monkeypatch.setattr(
        experiment_evaluation, "revalidate_training_comparison", _revalidate
    )


@dataclass
class _ReceiptStore:
    """evaluator가 GCS receipt를 재검증하는지 확인하는 strict fake store."""

    plan_receipt: ExperimentPlanReceipt
    metric_receipts: list[HeldOutMetricReceipt] = field(default_factory=list)

    def verify_plan_receipt(self, receipt: ExperimentPlanReceipt):
        if receipt != self.plan_receipt:
            raise PromotionEvidenceValidationError("plan receipt가 최신 GCS object와 다릅니다")
        return receipt.plan

    def verify_held_out_metric_receipt(self, receipt: HeldOutMetricReceipt):
        if receipt not in self.metric_receipts:
            raise PromotionEvidenceValidationError("metric receipt sha256가 최신 GCS object와 다릅니다")
        return receipt.evidence


def _object_receipt(*, path: str, sha256: str) -> GcsObjectReceipt:
    return GcsObjectReceipt(
        uri=f"gs://evidence/promotion-evidence/{path}",
        generation="1",
        metageneration="1",
        time_created=PLAN_TIME,
        sha256=sha256,
    )


def _published_plan_store(*, plan=None) -> tuple[_ReceiptStore, ExperimentPlanReceipt]:
    resolved_plan = plan or _plan()
    receipt = ExperimentPlanReceipt(
        plan=resolved_plan,
        object=_object_receipt(path=f"plans/{resolved_plan.plan_id}.json", sha256=_SHA_A),
    )
    return _ReceiptStore(plan_receipt=receipt), receipt


def _metric_receipt(
    *,
    store: _ReceiptStore,
    run_id: str,
    plan_receipt: ExperimentPlanReceipt,
    value: float,
    split_manifest_sha256: str,
    suffix: int,
) -> HeldOutMetricReceipt:
    receipt = HeldOutMetricReceipt(
        evidence=HeldOutMetricEvidence(
            run_id=run_id,
            plan_receipt=plan_receipt,
            value=value,
            split_manifest_sha256=split_manifest_sha256,
            test_membership_sha256=_SHA_B,
            model_artifact_path=f"model/{run_id}.joblib",
            model_artifact_sha256=_SHA_C,
        ),
        object=_object_receipt(
            path=f"metrics/{run_id}/{suffix:064x}.json",
            sha256=f"{suffix:064x}",
        ),
    )
    store.metric_receipts.append(receipt)
    return receipt


def _comparison(
    seed: int,
    *,
    store: _ReceiptStore,
    plan_receipt: ExperimentPlanReceipt,
    baseline_value: float = 0.80,
    challenger_value: float = 0.81,
    snapshot_sha256: str = _SHA_A,
    effective_seeds: TrainingSeeds | None | object = ...,
    promotion_evidence: VerifiedComparisonPromotionEvidence | None | object = ...,
    validated_at: datetime = PLAN_TIME + timedelta(minutes=1),
) -> TrainingComparisonManifest:
    if effective_seeds is ...:
        effective_seeds = TrainingSeeds(
            split_seed=seed,
            model_seed=seed,
            sampler_seed=seed,
        )
    baseline_split = f"{seed:064x}"
    challenger_split = f"{seed:064x}"
    if promotion_evidence is ...:
        promotion_evidence = VerifiedComparisonPromotionEvidence(
            plan_receipt=plan_receipt,
            baseline_metric=_metric_receipt(
                store=store,
                run_id=f"baseline-{seed}",
                plan_receipt=plan_receipt,
                value=baseline_value,
                split_manifest_sha256=baseline_split,
                suffix=seed,
            ),
            challenger_metric=_metric_receipt(
                store=store,
                run_id=f"challenger-{seed}",
                plan_receipt=plan_receipt,
                value=challenger_value,
                split_manifest_sha256=challenger_split,
                suffix=seed + 100,
            ),
        )
    return TrainingComparisonManifest(
        comparison_id=f"comparison-{seed}",
        baseline_run_id=f"baseline-{seed}",
        challenger_run_id=f"challenger-{seed}",
        baseline_snapshot_sha256=snapshot_sha256,
        challenger_snapshot_sha256=snapshot_sha256,
        baseline_snapshot_manifest_sha256=_SHA_B,
        challenger_snapshot_manifest_sha256=_SHA_B,
        baseline_split_manifest_sha256=baseline_split,
        challenger_split_manifest_sha256=challenger_split,
        baseline_feature_columns_sha256=_SHA_C,
        challenger_feature_columns_sha256=_SHA_D,
        baseline_feature_columns=("feature",),
        challenger_feature_columns=("feature", "challenger_feature"),
        effective_seeds=effective_seeds,
        promotion_evidence=promotion_evidence,
        validated_at=validated_at,
    )


def _evidence(
    plan_receipt: ExperimentPlanReceipt,
    *,
    store: _ReceiptStore,
    deltas: tuple[float, ...] = (0.010, 0.011) * 15,
    seeds: tuple[int, ...] = POLICY_SEEDS,
    comparison_overrides: dict[int, TrainingComparisonManifest] | None = None,
    metric_split_overrides: dict[int, str] | None = None,
):
    observations = tuple(
        PairedSeedObservation(
            seed=seed,
            comparison=(comparison_overrides or {}).get(
                seed,
                _comparison(
                    seed,
                    store=store,
                    plan_receipt=plan_receipt,
                    baseline_value=0.80,
                    challenger_value=0.80 + delta,
                ),
            ),
        )
        for seed, delta in zip(seeds, deltas)
    )
    evidence = create_paired_seed_evidence(
        plan_receipt=plan_receipt, observations=observations
    )
    if metric_split_overrides:
        # 새로운 evaluator는 comparison receipt의 split binding을 재검증해야 하므로
        # 이 legacy helper 인자는 직접 metric JSON 주입 대신 comparison을 변조한다.
        observations = tuple(
            observation.model_copy(
                update={
                    "comparison": observation.comparison.model_copy(
                        update={
                            "baseline_split_manifest_sha256": metric_split_overrides.get(
                                observation.seed,
                                observation.comparison.baseline_split_manifest_sha256,
                            )
                        }
                    )
                }
            )
            for observation in observations
        )
        evidence = create_paired_seed_evidence(
            plan_receipt=plan_receipt, observations=observations
        )
    return evidence


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


def test_legacy_plan_id_mismatch_reason_code_remains_parseable_during_transition() -> None:
    assert (
        EvaluationReasonCode("plan_id_mismatch")
        is EvaluationReasonCode.PLAN_ID_MISMATCH
    )


def test_v1_marks_verified_positive_paired_30_seed_evidence_eligible() -> None:
    store, receipt = _published_plan_store()
    evidence = _evidence(receipt, store=store)

    evaluation = evaluate_experiment(
        evidence, promotion_evidence_store=store, evaluated_at=PLAN_TIME
    )
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
    store, receipt = _published_plan_store()
    evidence = _evidence(receipt, store=store, deltas=(-0.020, -0.019) * 15)

    evaluation = evaluate_experiment(
        evidence, promotion_evidence_store=store, evaluated_at=PLAN_TIME
    )

    assert evaluation.verdict is EvaluationVerdict.REJECT
    assert evaluation.confidence_interval_upper is not None
    assert evaluation.confidence_interval_upper <= 0
    assert evaluation.reason_codes == (EvaluationReasonCode.PRIMARY_ROC_AUC_NOT_IMPROVED,)


def test_v1_holds_when_primary_roc_auc_is_inconclusive() -> None:
    store, receipt = _published_plan_store()
    evidence = _evidence(receipt, store=store, deltas=(-0.020, 0.020) * 15)

    evaluation = evaluate_experiment(
        evidence, promotion_evidence_store=store, evaluated_at=PLAN_TIME
    )

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.reason_codes == (EvaluationReasonCode.PRIMARY_ROC_AUC_INCONCLUSIVE,)


def test_v1_holds_for_a_seed_list_that_differs_from_policy() -> None:
    store, receipt = _published_plan_store()
    evidence = _evidence(receipt, store=store, seeds=tuple(range(43, 73)))

    evaluation = evaluate_experiment(
        evidence, promotion_evidence_store=store, evaluated_at=PLAN_TIME
    )

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.paired is False
    assert evaluation.reason_codes == (EvaluationReasonCode.SEED_POLICY_MISMATCH,)


def test_v1_holds_for_unpaired_effective_seed_evidence() -> None:
    store, receipt = _published_plan_store()
    mismatched = _comparison(
        42,
        store=store,
        plan_receipt=receipt,
        effective_seeds=TrainingSeeds(split_seed=42, model_seed=43, sampler_seed=42),
    )
    evidence = _evidence(receipt, store=store, comparison_overrides={42: mismatched})

    evaluation = evaluate_experiment(
        evidence, promotion_evidence_store=store, evaluated_at=PLAN_TIME
    )

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.reason_codes == (EvaluationReasonCode.UNPAIRED_SEED_EVIDENCE,)


def test_v1_holds_for_a_multi_candidate_plan() -> None:
    plan = _plan(candidate_ids=("candidate-a", "candidate-b"))
    store, receipt = _published_plan_store(plan=plan)
    evidence = _evidence(receipt, store=store)

    evaluation = evaluate_experiment(
        evidence, promotion_evidence_store=store, evaluated_at=PLAN_TIME
    )

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.reason_codes == (
        EvaluationReasonCode.MULTIPLE_CANDIDATES_REQUIRE_INDEPENDENT_HOLDOUT,
    )


def test_v1_holds_for_legacy_comparison_without_effective_seed_evidence() -> None:
    store, receipt = _published_plan_store()
    legacy = _comparison(
        42,
        store=store,
        plan_receipt=receipt,
        effective_seeds=None,
        promotion_evidence=None,
    )
    evidence = _evidence(receipt, store=store, comparison_overrides={42: legacy})

    evaluation = evaluate_experiment(
        evidence, promotion_evidence_store=store, evaluated_at=PLAN_TIME
    )

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.paired is False
    assert evaluation.reason_codes == (EvaluationReasonCode.PLAN_RECEIPT_MISSING,)


def test_v1_holds_when_comparisons_use_different_snapshots() -> None:
    store, receipt = _published_plan_store()
    other_snapshot = _comparison(
        42,
        store=store,
        plan_receipt=receipt,
        snapshot_sha256="e" * 64,
    )
    evidence = _evidence(receipt, store=store, comparison_overrides={42: other_snapshot})

    evaluation = evaluate_experiment(
        evidence, promotion_evidence_store=store, evaluated_at=PLAN_TIME
    )

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.reason_codes == (EvaluationReasonCode.SNAPSHOT_MISMATCH,)


def test_v1_does_not_use_caller_comparison_timestamp_as_a_trust_source() -> None:
    plan = _plan(created_at=PLAN_TIME + timedelta(minutes=2))
    store, receipt = _published_plan_store(plan=plan)
    evidence = _evidence(receipt, store=store)

    evaluation = evaluate_experiment(
        evidence, promotion_evidence_store=store, evaluated_at=PLAN_TIME
    )

    assert evaluation.verdict is EvaluationVerdict.ELIGIBLE
    assert evaluation.reason_codes == (
        EvaluationReasonCode.PRIMARY_ROC_AUC_IMPROVED_WITH_95PCT_CONFIDENCE,
    )


def test_v1_holds_when_comparison_is_bound_to_a_different_plan() -> None:
    declared_plan = _plan(candidate_ids=("candidate-declared",))
    different_plan = _plan(candidate_ids=("candidate-evaluated",))
    store, receipt = _published_plan_store(plan=different_plan)
    _, declared_receipt = _published_plan_store(plan=declared_plan)
    comparison = _comparison(42, store=store, plan_receipt=declared_receipt)
    evidence = _evidence(receipt, store=store, comparison_overrides={42: comparison})

    evaluation = evaluate_experiment(
        evidence, promotion_evidence_store=store, evaluated_at=PLAN_TIME
    )

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.reason_codes == (
        EvaluationReasonCode.METRIC_BINDING_MISMATCH,
    )


def test_v1_holds_when_comparison_has_no_predeclared_plan_binding() -> None:
    store, receipt = _published_plan_store()
    unbound = _comparison(
        42, store=store, plan_receipt=receipt, promotion_evidence=None
    )
    evidence = _evidence(receipt, store=store, comparison_overrides={42: unbound})

    evaluation = evaluate_experiment(
        evidence, promotion_evidence_store=store, evaluated_at=PLAN_TIME
    )

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.reason_codes == (
        EvaluationReasonCode.PLAN_RECEIPT_MISSING,
    )


def test_v1_holds_when_metric_evidence_uses_another_test_split() -> None:
    store, receipt = _published_plan_store()
    evidence = _evidence(
        receipt,
        store=store,
        metric_split_overrides={42: "f" * 64},
    )

    evaluation = evaluate_experiment(
        evidence, promotion_evidence_store=store, evaluated_at=PLAN_TIME
    )

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.paired is False
    assert evaluation.reason_codes == (EvaluationReasonCode.METRIC_BINDING_MISMATCH,)


def test_v1_holds_without_statistics_when_metric_receipt_sha_is_stale() -> None:
    store, receipt = _published_plan_store()
    comparison = _comparison(42, store=store, plan_receipt=receipt)
    promotion = comparison.promotion_evidence
    assert promotion is not None
    stale_baseline = promotion.baseline_metric.model_copy(
        update={
            "object": promotion.baseline_metric.object.model_copy(
                update={"sha256": "f" * 64}
            )
        }
    )
    stale_comparison = comparison.model_copy(
        update={
            "promotion_evidence": promotion.model_copy(
                update={"baseline_metric": stale_baseline}
            )
        }
    )
    evidence = _evidence(
        receipt,
        store=store,
        comparison_overrides={42: stale_comparison},
    )

    evaluation = evaluate_experiment(
        evidence, promotion_evidence_store=store, evaluated_at=PLAN_TIME
    )

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.paired is False
    assert evaluation.confidence_interval_lower is None
    assert evaluation.reason_codes == (
        EvaluationReasonCode.RECEIPT_REVALIDATION_FAILED,
    )


def test_paired_observation_rejects_caller_supplied_raw_metric_values() -> None:
    store, receipt = _published_plan_store()
    comparison = _comparison(42, store=store, plan_receipt=receipt)

    with pytest.raises(ValidationError, match="baseline"):
        PairedSeedObservation(
            seed=42,
            comparison=comparison,
            baseline={"value": 0.99},
        )


def test_experiment_plan_rejects_an_empty_candidate_identifier() -> None:
    with pytest.raises(ValidationError, match="candidate_ids"):
        create_experiment_plan(
            hypothesis_id="issue-466-h1",
            control_id="lgbm-2026-08-01",
            candidate_ids=("",),
            created_at=PLAN_TIME,
        )


def test_v1_holds_without_statistics_when_canonical_revalidation_rejects_transport(
    monkeypatch,
) -> None:
    store, receipt = _published_plan_store()

    def _reject_post_hoc_plan(
        comparison: TrainingComparisonManifest, *, promotion_evidence_store: object
    ) -> TrainingComparisonManifest:
        raise ComparisonValidationError("plan receipt가 MLflow run 시작 뒤에 생성됐습니다")

    monkeypatch.setattr(
        experiment_evaluation, "revalidate_training_comparison", _reject_post_hoc_plan
    )
    evidence = _evidence(receipt, store=store)

    evaluation = evaluate_experiment(
        evidence, promotion_evidence_store=store, evaluated_at=PLAN_TIME
    )

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.confidence_interval_lower is None
    assert evaluation.reason_codes == (
        EvaluationReasonCode.RECEIPT_REVALIDATION_FAILED,
    )


def test_v1_records_the_actual_t_critical_for_zero_standard_error() -> None:
    store, receipt = _published_plan_store()
    evidence = _evidence(receipt, store=store, deltas=(0.010,) * len(POLICY_SEEDS))

    evaluation = evaluate_experiment(
        evidence, promotion_evidence_store=store, evaluated_at=PLAN_TIME
    )

    assert evaluation.verdict is EvaluationVerdict.ELIGIBLE
    assert evaluation.standard_error == 0
    # t 임계값은 자유도(seed 수 - 1)에 의존한다. 데모 스코프의 seed 3개면 자유도 2라
    # 양측 95% 임계값이 4.303이다(30개였을 때는 자유도 29, 2.045).
    # 프로덕션은 `t_critical_95(len(paired_deltas) - 1)`로 계산하므로 이 값만 따라온다.
    assert evaluation.t_critical == pytest.approx(4.303, abs=1e-3)


def test_decision_record_is_deterministic_and_excludes_registry_coordinates() -> None:
    store, receipt = _published_plan_store()
    evidence = _evidence(receipt, store=store)
    first = evaluate_experiment(
        evidence, promotion_evidence_store=store, evaluated_at=PLAN_TIME
    )
    second = evaluate_experiment(
        evidence, promotion_evidence_store=store, evaluated_at=PLAN_TIME
    )

    record = PromotionDecisionRecord(
        evaluation=first,
        decision=decide_promotion(first, decided_at=PLAN_TIME),
    )

    assert first.model_dump_json() == second.model_dump_json()
    assert record.decision.evaluation_id == record.evaluation.evaluation_id
    assert '"tracking_uri"' not in record.model_dump_json()
    assert '"champion_alias"' not in record.model_dump_json()


# ---------------------------------------------------------------------------
# #485 §5.3 안 A — 스키마 부착 계약 (PR #527 리뷰 Medium#1)
#
# `summarize_temporal_signal`의 산출 규칙은 test_experiment_evaluation_temporal_signal.py가
# 덮는다. 여기서 고정하는 것은 그 결과가 **판정 산출물에 실려 나가는지**다 — 규칙이
# 맞아도 전파가 빠지면 필드는 영원히 None이고, 그 상태를 잡는 테스트가 없었다.
# ---------------------------------------------------------------------------


def _temporal_signal(
    confidence: EvaluationConfidence = EvaluationConfidence.MEDIUM,
) -> TemporalSignal:
    return TemporalSignal(confidence=confidence, robustness_note="관측 밀도가 낮습니다.")


def test_temporal_signal_rides_on_a_normal_evaluation() -> None:
    store, receipt = _published_plan_store()
    evidence = _evidence(receipt, store=store)
    signal = _temporal_signal()

    evaluation = evaluate_experiment(
        evidence,
        promotion_evidence_store=store,
        evaluated_at=PLAN_TIME,
        temporal_signal=signal,
    )

    assert evaluation.temporal_signal == signal
    # 병기 신호이지 판정 입력이 아니다 — verdict·reason_codes는 그대로여야 한다.
    assert evaluation.verdict is EvaluationVerdict.ELIGIBLE
    assert evaluation.reason_codes == (
        EvaluationReasonCode.PRIMARY_ROC_AUC_IMPROVED_WITH_95PCT_CONFIDENCE,
    )


def test_temporal_signal_rides_on_a_failed_evaluation_too() -> None:
    """hold 경로에서도 버리지 않는다 — 호출 지점 4곳 중 하나만 빠져도 여기서 걸린다."""
    store, receipt = _published_plan_store()
    evidence = _evidence(receipt, store=store, seeds=tuple(range(43, 73)))
    signal = _temporal_signal()

    evaluation = evaluate_experiment(
        evidence,
        promotion_evidence_store=store,
        evaluated_at=PLAN_TIME,
        temporal_signal=signal,
    )

    assert evaluation.verdict is EvaluationVerdict.HOLD
    assert evaluation.reason_codes == (EvaluationReasonCode.SEED_POLICY_MISMATCH,)
    # 판정에 실패했다고 해서 호출부가 준 관측을 조용히 버리지 않는다.
    assert evaluation.temporal_signal == signal


def test_temporal_signal_does_not_change_evaluation_id() -> None:
    """해시 payload 제외 결정의 회귀 가드(spec §5.3).

    `_stable_id` payload를 나중에 `_model_payload(evaluation)` 같은 형태로 리팩터링하면
    이 결정이 조용히 뒤집힌다. 같은 증거·같은 통계면 temporal 유무·내용과 무관하게
    같은 id여야 한다.
    """
    store, receipt = _published_plan_store()
    evidence = _evidence(receipt, store=store)

    def _evaluate(signal: TemporalSignal | None) -> ExperimentEvaluation:
        return evaluate_experiment(
            evidence,
            promotion_evidence_store=store,
            evaluated_at=PLAN_TIME,
            temporal_signal=signal,
        )

    without = _evaluate(None)
    medium = _evaluate(_temporal_signal(EvaluationConfidence.MEDIUM))
    high = _evaluate(_temporal_signal(EvaluationConfidence.HIGH))

    assert without.evaluation_id == medium.evaluation_id == high.evaluation_id
    # 같은 id를 쓰는 소비자(PairedExperimentResult, #472)도 같은 값을 본다.
    assert (
        decide_promotion(without, decided_at=PLAN_TIME).evaluation_id
        == decide_promotion(high, decided_at=PLAN_TIME).evaluation_id
    )
