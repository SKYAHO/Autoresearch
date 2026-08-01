"""실험 결과의 자동 승격 적격성 판정 (#466).

[파이프라인] 학습·채점이 만든 반복 실험 증거와 이후의 모델 레지스트리 승격 사이에서,
사전에 선언된 가설·대조군·시드 정책을 검증하고 승격 가능 여부를 계산하는 구간을
담당한다.

[기능] held-out test ROC-AUC의 30개 짝지은 시드 증거를 검증하고, 양측 95% t
신뢰구간으로 `eligible`, `hold`, `reject` 판정과 이식 가능한 증거 레코드를 만든다.

[비책임] 모델 학습은 `src/pipeline/train.py`, 지표 산출은
`src/pipeline/evaluate.py`, MLflow artifact 비교 검증은
`src/pipeline/training_comparison.py`가 소유한다. 레지스트리 alias 이동과 dev/
production 경계 집행은 후속 #470의 책임이며 이 모듈은 외부 시스템을 호출하지
않는다.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.pipeline.promotion_evidence import (
    PROMOTION_POLICY_VERSION,
    ExperimentPlan,
    create_experiment_plan,  # noqa: F401 - 기존 import 경로 호환 re-export
)
from src.pipeline.seed_sweep import compare_to_baseline, summarize_metric, t_critical_95
from src.pipeline.training_provenance import SHA256_PATTERN, TrainingComparisonManifest


POLICY_VERSION = PROMOTION_POLICY_VERSION
PRIMARY_METRIC = "roc_auc"
POLICY_SEEDS = tuple(range(42, 72))
CONFIDENCE_LEVEL = 0.95


def _normalize_utc_datetime(value: datetime) -> datetime:
    """시각을 UTC-aware datetime으로 정규화하고, timezone 없는 값은 거부한다."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone 정보를 포함한 UTC 시각이 필요합니다")
    return value.astimezone(timezone.utc)


def _has_timezone(value: datetime) -> bool:
    """legacy manifest의 시각을 비교해도 되는지 확인한다."""
    return value.tzinfo is not None and value.utcoffset() is not None


class _ImmutableModel(BaseModel):
    """승격 증거 계약에 적용하는 불변 Pydantic 기본 설정."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        allow_inf_nan=False,
    )


class EvaluationVerdict(str, Enum):
    """사전 선언된 정책에 따른 후보 모델의 승격 적격성."""

    ELIGIBLE = "eligible"
    HOLD = "hold"
    REJECT = "reject"


class MetricDirection(str, Enum):
    """주 지표가 개선으로 인정되는 방향."""

    MAXIMIZE = "maximize"


class EvaluationReasonCode(str, Enum):
    """결정론적 판정 근거 코드."""

    PRIMARY_ROC_AUC_IMPROVED_WITH_95PCT_CONFIDENCE = (
        "primary_roc_auc_improved_with_95pct_confidence"
    )
    PRIMARY_ROC_AUC_NOT_IMPROVED = "primary_roc_auc_not_improved"
    PRIMARY_ROC_AUC_INCONCLUSIVE = "primary_roc_auc_inconclusive"
    SEED_POLICY_MISMATCH = "seed_policy_mismatch"
    UNPAIRED_SEED_EVIDENCE = "unpaired_seed_evidence"
    MULTIPLE_CANDIDATES_REQUIRE_INDEPENDENT_HOLDOUT = (
        "multiple_candidates_require_independent_holdout"
    )
    EFFECTIVE_SEEDS_MISSING = "effective_seeds_missing"
    SNAPSHOT_MISMATCH = "snapshot_mismatch"
    PLAN_NOT_PREDECLARED = "plan_not_predeclared"
    PLAN_EVIDENCE_MISMATCH = "plan_evidence_mismatch"
    DUPLICATE_COMPARISON_EVIDENCE = "duplicate_comparison_evidence"
    COMPARISON_PLAN_MISMATCH = "comparison_plan_mismatch"
    METRIC_SPLIT_MISMATCH = "metric_split_mismatch"
    TIMESTAMP_TIMEZONE_MISSING = "timestamp_timezone_missing"


class PromotionPolicy(_ImmutableModel):
    """자동 판정에 고정된 v1 정책."""

    policy_version: Literal["promotion-policy-v1"] = POLICY_VERSION
    primary_metric: Literal["roc_auc"] = PRIMARY_METRIC
    direction: MetricDirection = MetricDirection.MAXIMIZE
    required_seeds: tuple[int, ...] = POLICY_SEEDS
    require_paired_comparison: Literal[True] = True
    confidence_level: float = CONFIDENCE_LEVEL
    multiple_candidate_policy: Literal["independent_holdout_required"] = (
        "independent_holdout_required"
    )


class HeldOutRocAucEvidence(_ImmutableModel):
    """immutable held-out test ROC-AUC와 해당 test split의 provenance."""

    metric_name: Literal["roc_auc"] = PRIMARY_METRIC
    dataset_split: Literal["test"] = "test"
    value: float = Field(ge=0, le=1)
    split_manifest_sha256: str = Field(pattern=SHA256_PATTERN)


class PairedSeedObservation(_ImmutableModel):
    """하나의 시드에서 같은 조건으로 비교한 baseline/challenger ROC-AUC."""

    seed: int
    baseline: HeldOutRocAucEvidence
    challenger: HeldOutRocAucEvidence
    comparison: TrainingComparisonManifest


class PairedSeedEvidence(_ImmutableModel):
    """하나의 사전 선언된 실험 계획에 귀속되는 시드별 비교 증거."""

    evidence_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    observations: tuple[PairedSeedObservation, ...]


class ExperimentEvaluation(_ImmutableModel):
    """v1 정책을 증거에 적용한 통계·안전장치 결과."""

    evaluation_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    policy_version: Literal["promotion-policy-v1"] = POLICY_VERSION
    metric_name: Literal["roc_auc"] = PRIMARY_METRIC
    required_seeds: tuple[int, ...] = POLICY_SEEDS
    paired: bool
    baseline_mean: float | None
    challenger_mean: float | None
    paired_delta_mean: float | None
    standard_error: float | None
    t_critical: float | None
    confidence_interval_lower: float | None
    confidence_interval_upper: float | None
    verdict: EvaluationVerdict
    reason_codes: tuple[EvaluationReasonCode, ...]
    evaluated_at: datetime

    @field_validator("evaluated_at")
    @classmethod
    def _normalize_evaluated_at(cls, value: datetime) -> datetime:
        return _normalize_utc_datetime(value)


class PromotionDecision(_ImmutableModel):
    """평가 결과를 이후 승격 작업이 소비할 수 있게 고정한 판정 레코드."""

    decision_id: str = Field(min_length=1)
    evaluation_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    policy_version: Literal["promotion-policy-v1"] = POLICY_VERSION
    verdict: EvaluationVerdict
    reason_codes: tuple[EvaluationReasonCode, ...]
    decided_at: datetime

    @field_validator("decided_at")
    @classmethod
    def _normalize_decided_at(cls, value: datetime) -> datetime:
        return _normalize_utc_datetime(value)


class PromotionDecisionRecord(_ImmutableModel):
    """이식·감사 가능한 평가와 승격 판정의 묶음."""

    evaluation: ExperimentEvaluation
    decision: PromotionDecision


def _json_default(value: object) -> str:
    """stable identifier의 JSON 입력에서 표준 JSON 밖 값을 정규화한다."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"canonical JSON으로 정규화할 수 없는 값입니다: {value!r}")


def _canonical_sha256(value: object) -> str:
    """JSON 정규화 후 SHA-256을 계산한다."""
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _model_payload(model: BaseModel) -> dict[str, object]:
    """Pydantic 모델을 stable JSON-compatible payload로 바꾼다."""
    return model.model_dump(mode="json")


def _utc_now() -> datetime:
    """UTC-aware 현재 시각을 반환한다."""
    return datetime.now(timezone.utc)


def _stable_id(prefix: str, value: object) -> str:
    """외부 저장소 좌표 없이 계약 내용 전체로 stable identifier를 만든다."""
    return f"{prefix}-{_canonical_sha256(value)}"


def promotion_policy_v1() -> PromotionPolicy:
    """호출자가 완화할 수 없는 v1 자동 승격 정책을 반환한다."""
    return PromotionPolicy()


def create_paired_seed_evidence(
    *,
    plan_id: str,
    observations: tuple[PairedSeedObservation, ...],
) -> PairedSeedEvidence:
    """시드별 원본 관측치를 보존한 이식 가능한 증거 레코드를 만든다."""
    payload = {
        "plan_id": plan_id,
        "observations": [
            _model_payload(observation) for observation in observations
        ],
    }
    return PairedSeedEvidence(
        evidence_id=_stable_id("paired-seed-evidence", payload),
        plan_id=plan_id,
        observations=observations,
    )


def _failed_evaluation(
    *,
    plan: ExperimentPlan,
    evidence: PairedSeedEvidence,
    reason_codes: set[EvaluationReasonCode],
    evaluated_at: datetime,
) -> ExperimentEvaluation:
    """근거가 불완전할 때 통계를 추정하지 않는 fail-closed 평가를 만든다."""
    reasons = tuple(sorted(reason_codes, key=lambda reason: reason.value))
    payload = {
        "evidence_id": evidence.evidence_id,
        "plan_id": plan.plan_id,
        "policy_version": POLICY_VERSION,
        "paired": False,
        "verdict": EvaluationVerdict.HOLD.value,
        "reason_codes": [reason.value for reason in reasons],
    }
    return ExperimentEvaluation(
        evaluation_id=_stable_id("experiment-evaluation", payload),
        evidence_id=evidence.evidence_id,
        plan_id=plan.plan_id,
        paired=False,
        baseline_mean=None,
        challenger_mean=None,
        paired_delta_mean=None,
        standard_error=None,
        t_critical=None,
        confidence_interval_lower=None,
        confidence_interval_upper=None,
        verdict=EvaluationVerdict.HOLD,
        reason_codes=reasons,
        evaluated_at=evaluated_at,
    )


def _evidence_reason_codes(
    *, plan: ExperimentPlan, evidence: PairedSeedEvidence, policy: PromotionPolicy
) -> set[EvaluationReasonCode]:
    """자동 승격에 필요한 provenance·사전 선언·짝지음 근거를 검사한다."""
    reasons: set[EvaluationReasonCode] = set()
    if plan.plan_id != evidence.plan_id:
        reasons.add(EvaluationReasonCode.PLAN_EVIDENCE_MISMATCH)
    if len(plan.candidate_ids) != 1:
        reasons.add(
            EvaluationReasonCode.MULTIPLE_CANDIDATES_REQUIRE_INDEPENDENT_HOLDOUT
        )

    observations = evidence.observations
    if tuple(observation.seed for observation in observations) != policy.required_seeds:
        reasons.add(EvaluationReasonCode.SEED_POLICY_MISMATCH)

    comparison_ids: set[str] = set()
    snapshot_hashes: set[str] = set()
    for observation in observations:
        comparison = observation.comparison
        if comparison.comparison_id in comparison_ids:
            reasons.add(EvaluationReasonCode.DUPLICATE_COMPARISON_EVIDENCE)
        comparison_ids.add(comparison.comparison_id)

        if comparison.experiment_plan_id != plan.plan_id:
            reasons.add(EvaluationReasonCode.COMPARISON_PLAN_MISMATCH)

        effective_seeds = comparison.effective_seeds
        if effective_seeds is None:
            reasons.add(EvaluationReasonCode.EFFECTIVE_SEEDS_MISSING)
        elif (
            effective_seeds.split_seed != observation.seed
            or effective_seeds.model_seed != observation.seed
            or effective_seeds.sampler_seed != observation.seed
        ):
            reasons.add(EvaluationReasonCode.UNPAIRED_SEED_EVIDENCE)

        if comparison.baseline_snapshot_sha256 != comparison.challenger_snapshot_sha256:
            reasons.add(EvaluationReasonCode.SNAPSHOT_MISMATCH)
        snapshot_hashes.add(comparison.baseline_snapshot_sha256)
        snapshot_hashes.add(comparison.challenger_snapshot_sha256)

        if not _has_timezone(comparison.validated_at):
            reasons.add(EvaluationReasonCode.TIMESTAMP_TIMEZONE_MISSING)
        elif plan.created_at > comparison.validated_at:
            reasons.add(EvaluationReasonCode.PLAN_NOT_PREDECLARED)

        if (
            observation.baseline.split_manifest_sha256
            != comparison.baseline_split_manifest_sha256
            or observation.challenger.split_manifest_sha256
            != comparison.challenger_split_manifest_sha256
        ):
            reasons.add(EvaluationReasonCode.METRIC_SPLIT_MISMATCH)

    if len(snapshot_hashes) != 1:
        reasons.add(EvaluationReasonCode.SNAPSHOT_MISMATCH)
    return reasons


def evaluate_experiment(
    plan: ExperimentPlan,
    evidence: PairedSeedEvidence,
    *,
    evaluated_at: datetime | None = None,
) -> ExperimentEvaluation:
    """고정된 v1 정책으로 실험 증거를 평가한다.

    `eligible`은 평균 ROC-AUC 개선과 양측 95% 신뢰구간 하한의 양수를 모두
    만족할 때만 나온다. 근거가 하나라도 누락되면 통계를 보정하거나 추가 실행하지
    않고 `hold`로 fail-closed 한다.
    """
    policy = promotion_policy_v1()
    evaluation_time = _normalize_utc_datetime(evaluated_at or _utc_now())
    reasons = _evidence_reason_codes(plan=plan, evidence=evidence, policy=policy)
    if reasons:
        return _failed_evaluation(
            plan=plan,
            evidence=evidence,
            reason_codes=reasons,
            evaluated_at=evaluation_time,
        )

    baseline_values = tuple(
        observation.baseline.value for observation in evidence.observations
    )
    challenger_values = tuple(
        observation.challenger.value for observation in evidence.observations
    )
    paired_deltas = tuple(
        challenger - baseline
        for baseline, challenger in zip(baseline_values, challenger_values)
    )
    baseline = summarize_metric(baseline_values)
    challenger = summarize_metric(challenger_values)
    significance = compare_to_baseline(
        candidate=challenger,
        baseline=baseline,
        paired_deltas=paired_deltas,
    )
    t_critical = t_critical_95(len(paired_deltas) - 1)
    confidence_interval_lower = significance.delta - significance.threshold
    confidence_interval_upper = significance.delta + significance.threshold

    if significance.delta > 0 and confidence_interval_lower > 0:
        verdict = EvaluationVerdict.ELIGIBLE
        reason_codes = (
            EvaluationReasonCode.PRIMARY_ROC_AUC_IMPROVED_WITH_95PCT_CONFIDENCE,
        )
    elif confidence_interval_upper <= 0:
        verdict = EvaluationVerdict.REJECT
        reason_codes = (EvaluationReasonCode.PRIMARY_ROC_AUC_NOT_IMPROVED,)
    else:
        verdict = EvaluationVerdict.HOLD
        reason_codes = (EvaluationReasonCode.PRIMARY_ROC_AUC_INCONCLUSIVE,)

    payload = {
        "evidence_id": evidence.evidence_id,
        "plan_id": plan.plan_id,
        "policy_version": policy.policy_version,
        "paired": True,
        "baseline_mean": baseline.mean,
        "challenger_mean": challenger.mean,
        "paired_delta_mean": significance.delta,
        "standard_error": significance.standard_error,
        "t_critical": t_critical,
        "confidence_interval_lower": confidence_interval_lower,
        "confidence_interval_upper": confidence_interval_upper,
        "verdict": verdict.value,
        "reason_codes": [reason.value for reason in reason_codes],
    }
    return ExperimentEvaluation(
        evaluation_id=_stable_id("experiment-evaluation", payload),
        evidence_id=evidence.evidence_id,
        plan_id=plan.plan_id,
        paired=True,
        baseline_mean=baseline.mean,
        challenger_mean=challenger.mean,
        paired_delta_mean=significance.delta,
        standard_error=significance.standard_error,
        t_critical=t_critical,
        confidence_interval_lower=confidence_interval_lower,
        confidence_interval_upper=confidence_interval_upper,
        verdict=verdict,
        reason_codes=reason_codes,
        evaluated_at=evaluation_time,
    )


def decide_promotion(
    evaluation: ExperimentEvaluation,
    *,
    decided_at: datetime | None = None,
) -> PromotionDecision:
    """평가 결과를 그대로 보존하는 승격 판정 계약을 만든다.

    이 함수는 레지스트리 alias를 이동하지 않는다. 후속 작업은 이 레코드의
    `eligible` 판정과 별도의 대상·동시성 안전장치를 검증한 뒤에만 상태를 바꿔야 한다.
    """
    payload = {
        "evaluation_id": evaluation.evaluation_id,
        "plan_id": evaluation.plan_id,
        "policy_version": evaluation.policy_version,
        "verdict": evaluation.verdict.value,
        "reason_codes": [reason.value for reason in evaluation.reason_codes],
    }
    return PromotionDecision(
        decision_id=_stable_id("promotion-decision", payload),
        evaluation_id=evaluation.evaluation_id,
        plan_id=evaluation.plan_id,
        verdict=evaluation.verdict,
        reason_codes=evaluation.reason_codes,
        decided_at=_normalize_utc_datetime(decided_at or _utc_now()),
    )
