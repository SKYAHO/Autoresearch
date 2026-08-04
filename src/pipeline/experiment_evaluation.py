"""실험 결과의 자동 승격 적격성 판정 (#466).

[파이프라인] 학습·채점이 만든 반복 실험 증거와 이후의 모델 레지스트리 승격 사이에서,
사전에 선언된 가설·대조군·시드 정책을 검증하고 승격 가능 여부를 계산하는 구간을
담당한다.

[기능] held-out test ROC-AUC의 30개 짝지은 시드 증거를 검증하고, 양측 95% t
신뢰구간으로 `eligible`, `hold`, `reject` 판정과 이식 가능한 증거 레코드를 만든다.
시간축 신호(#485 §5)를 `confidence`/`robustness_note`/`direction_vs_offline_metric`으로
요약해 판정 산출물에 병기한다(`summarize_temporal_signal`) — 이 신호는 `verdict`를
바꾸지 않고, 사람과 `#472`가 판정의 신뢰도를 읽는 데 쓴다.

[비책임] 모델 학습은 `src/pipeline/train.py`, 지표 산출은
`src/pipeline/evaluate.py`, MLflow artifact 비교 검증은
`src/pipeline/training_comparison.py`, 시간축 **측정**은
`src/pipeline/degradation_eval.py`가 소유한다. 이 모듈은 `degradation_eval`을
**import하지 않는다** — 그 모듈이 끌고 오는 `train`(→ lightgbm)이 판정 경로에
딸려오면 안 되기 때문이다. 호출부가
`temporal_signal_inputs(result)`로 원시값을 뽑아 넘긴다(#485 §5.3). 이 모듈은 그 verifier를 호출해
전달된 comparison JSON을 다시 구성하지만 MLflow/GCS 검증 규칙을 자체 소유하지
않는다. 레지스트리 alias 이동과 dev/production 경계 집행은 후속 #470의 책임이다.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.pipeline.promotion_evidence import (
    PROMOTION_POLICY_VERSION,
    ExperimentPlan,
    ExperimentPlanReceipt,
    PromotionEvidenceStore,
    PromotionEvidenceValidationError,
    create_experiment_plan,  # noqa: F401 - 기존 import 경로 호환 re-export
)
from src.pipeline.seed_sweep import compare_to_baseline, summarize_metric, t_critical_95
from src.pipeline.training_comparison import (
    ComparisonValidationError,
    revalidate_training_comparison,
)
from src.pipeline.training_provenance import TrainingComparisonManifest


POLICY_VERSION = PROMOTION_POLICY_VERSION
PRIMARY_METRIC = "roc_auc"
POLICY_SEEDS = tuple(range(42, 72))
CONFIDENCE_LEVEL = 0.95


def _normalize_utc_datetime(value: datetime) -> datetime:
    """시각을 UTC-aware datetime으로 정규화하고, timezone 없는 값은 거부한다."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone 정보를 포함한 UTC 시각이 필요합니다")
    return value.astimezone(timezone.utc)


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
    PLAN_ID_MISMATCH = "plan_id_mismatch"
    COMPARISON_PLAN_MISMATCH = "comparison_plan_mismatch"
    METRIC_SPLIT_MISMATCH = "metric_split_mismatch"
    TIMESTAMP_TIMEZONE_MISSING = "timestamp_timezone_missing"
    PLAN_RECEIPT_MISSING = "plan_receipt_missing"
    RECEIPT_REVALIDATION_FAILED = "receipt_revalidation_failed"
    METRIC_BINDING_MISMATCH = "metric_binding_mismatch"


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


class PairedSeedObservation(_ImmutableModel):
    """하나의 시드에서 이미 verified comparison을 가리키는 관측."""

    seed: int
    comparison: TrainingComparisonManifest


class PairedSeedEvidence(_ImmutableModel):
    """하나의 사전 선언된 실험 계획에 귀속되는 시드별 비교 증거."""

    evidence_id: str = Field(min_length=1)
    plan_receipt: ExperimentPlanReceipt
    observations: tuple[PairedSeedObservation, ...]


# ============================================================================
# temporal signal — `#425` 다중 신호 판정 (#485 §5)
#
# `RollingOriginResult`를 직접 받지 않고 **원시값**을 받는다. `degradation_eval`을
# import하면 그 모듈이 끌고 오는 `train`(→ lightgbm)이 판정 경로에 딸려온다 — 판정
# 엔진은 지금 ML 의존이 전혀 없고 그 성질을 유지한다. 호출부가 결과에서 값을 읽어
# 넘긴다.
# ============================================================================


class EvaluationConfidence(str, Enum):
    """판정 결과를 얼마나 믿을 수 있는지(`#425`)."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SignalDirection(str, Enum):
    """다른 신호와 방향이 일치하는지(`#425`)."""

    AGREE = "agree"
    DISAGREE = "disagree"
    NOT_APPLICABLE = "not_applicable"


class TemporalSignal(_ImmutableModel):
    """시간축 평가가 판정에 싣는 신호(#485 §5).

    ``verdict``를 바꾸지 않는다 — `#485` 범위에서 temporal은 **병기 신호**이고,
    주 지표 일반화는 `#493`이 소유한다. 사람과 `#472`가 판정의 신뢰도를 읽는 데 쓴다.
    """

    confidence: EvaluationConfidence
    robustness_note: str | None = None
    direction_vs_offline_metric: SignalDirection = SignalDirection.NOT_APPLICABLE


def summarize_temporal_signal(
    *,
    degradation_elapsed_days: int | None,
    recent_roc_auc_mean: float | None,
    valid_day_count: int,
    recent_window_days: int,
    offline_primary_delta: float | None = None,
    temporal_delta: float | None = None,
) -> TemporalSignal:
    """시간축 관측을 `#425` 신호 3종으로 요약한다(spec §5.1·§5.2).

    ``confidence``는 **delta 크기가 아니라 관측 밀도**로 정한다. `#425` 배경
    (round_002)에서 "격차가 임계값의 2.6배였음에도 신뢰할 수 없었던" 원인이 표본
    민감도였고, temporal signal의 대응물이 유효 평가일 수이기 때문이다.

    이 함수는 spec §6의 ``hold``를 통과한 결과에만 부른다 — hold로 걸러지는 조건
    (유효일 < 2 등)을 여기서 다시 검사하지 않는다.

    Args:
        degradation_elapsed_days: 열화가 잡힌 경과일. 미탐지면 ``None``.
        recent_roc_auc_mean: 최근 구간 평균. 표본이 모자라면 ``None``(spec §3).
        valid_day_count: 유효 평가일 수.
        recent_window_days: "최근"의 폭.
        offline_primary_delta: 오프라인 주 지표의 baseline 대비 delta.
        temporal_delta: 같은 비교의 temporal 쪽 delta. 두 조건 비교(`#514`)
            전까지는 ``None``이라 방향 판정이 ``not_applicable``이 된다.

    Returns:
        ``confidence``/``robustness_note``/``direction_vs_offline_metric``.
        ``disagree``나 ``low``는 **실패가 아니다** — 예외를 던지지 않고 정상 종료한다
        (`#425` 완료 조건).

    Raises:
        ValueError: ``recent_window_days``가 1 미만일 때.
    """
    # `summarize_valid_roc_auc`(PR #520 리뷰 Medium#4)와 같은 가드를 여기에도 둔다
    # (PR #527 리뷰 Low#3). `RollingOriginResult.recent_window_days`에는 `ge=1` 제약이
    # 없어 JSON 왕복이나 손으로 조립한 결과로 0이 들어올 수 있는데, 그러면 아래 밀도
    # 임계값이 `valid_day_count < 2`로 조용히 느슨해져 `high`가 더 쉽게 나온다.
    # 잘못된 입력을 낮은 신뢰도가 아니라 **더 높은** 신뢰도로 바꾸는 방향이라 막는다.
    if recent_window_days < 1:
        raise ValueError("recent_window_days는 1 이상이어야 합니다")

    notes: list[str] = []

    if offline_primary_delta is None or temporal_delta is None:
        direction = SignalDirection.NOT_APPLICABLE
    # `> 0` 하나로 갈라 `0.0`과 `-0.0`을 모두 "양수 아님"에 묶는다(PR #527 이해도 확인 1).
    # 의도한 동작이다 — delta의 의미는 "개선했는가"이고, 개선하지 못한 것(`0.0`)은
    # 악화(`-0.05`)와 같은 편에 서는 게 맞다. `0.0`을 제3의 상태로 두면 부동소수 연산
    # 결과가 정확히 `0.0`이냐 `1e-18`이냐에 따라 판정이 갈리는데, 그 차이는 실험적으로
    # 의미가 없다. `-0.0 > 0`도 `False`라 부호 붙은 0도 같은 편에 들어간다.
    #
    # 결과적으로 `offline=0.0, temporal=-0.05`는 `agree`(둘 다 개선 못 함),
    # `offline=0.0, temporal=+0.05`는 `disagree`(한쪽만 개선)가 된다.
    # `#514`에서 이 값이 판정 입력이 되면 전자는 "둘 다 개선 없음"으로 기각 근거가,
    # 후자는 `robustness_note`가 붙은 낮은 신뢰도 결과가 된다 — 어느 쪽도 자동 승격이
    # 아니므로 이 경계가 승격을 느슨하게 만들지는 않는다.
    elif (offline_primary_delta > 0) == (temporal_delta > 0):
        direction = SignalDirection.AGREE
    else:
        direction = SignalDirection.DISAGREE
        notes.append(
            "오프라인 주 지표와 시간축 신호의 방향이 반대입니다 — 두 신호가 같은 결론을 "
            "가리키지 않으므로 단독 판정 근거로 쓰지 마십시오."
        )

    # low가 medium을 이긴다 — 두 조건이 동시 성립하면 신뢰도를 과대평가하지 않는다.
    #
    # spec §5.1은 이 조건을 `hard_retrain_limit_days is not None AND elapsed_days <= 1`로
    # 적었는데 여기서는 앞 절을 `degradation_elapsed_days is not None`으로 쓴다 —
    # `derive_hard_retrain_limit`의 반환 경로상 동치이기 때문이다(PR #527 이해도 확인 2).
    #   - `degradation_point`가 없으면 `limit_days=None`(`no_degradation_observed_within_horizon`
    #     또는 `insufficient_valid_points`)이므로 두 조건 모두 거짓이다.
    #   - `degradation_point`가 있으면 `limit_days`는 양수이거나 `0`으로 clamp된
    #     (`safety_margin_exceeds_degradation_point`) 값이며, **둘 다 `None`이 아니다.**
    #     즉 `elapsed_days`가 있는데 `limit_days`만 `None`인 경우는 없다.
    # 동치가 깨지려면 `derive_hard_retrain_limit`이 `degradation_point`가 있는데도
    # `limit_days=None`을 내는 경로를 새로 만들어야 한다. 그때 이 함수는 `low`여야 할
    # 결과를 `medium`/`high`로 **올려서** 내보내므로, 증상이 "예외"가 아니라 "조용한
    # 신뢰도 과대평가"로 나타난다. 그래서 인자로 받지 않고 여기 근거를 남긴다.
    if degradation_elapsed_days is not None and degradation_elapsed_days <= 1:
        confidence = EvaluationConfidence.LOW
        notes.append(
            f"열화가 {degradation_elapsed_days}일차에 잡혀 곡선을 이루는 표본이 사실상 "
            "1~2개입니다 — 통계적으로 불안정합니다."
        )
    elif recent_roc_auc_mean is None:
        confidence = EvaluationConfidence.LOW
        notes.append(
            "유효 평가일이 recent_window_days보다 적어 최근 구간 평균을 만들지 "
            "못했습니다(표본 부족)."
        )
    elif valid_day_count < recent_window_days + 2 or degradation_elapsed_days is None:
        confidence = EvaluationConfidence.MEDIUM
        if valid_day_count < recent_window_days + 2:
            notes.append(f"유효 평가일이 {valid_day_count}일로 관측 밀도가 낮습니다.")
        if degradation_elapsed_days is None:
            notes.append(
                "관측 기간 안에서 열화가 확인되지 않았습니다 — '안전하다'는 뜻이 아니라 "
                "판정 근거가 약하다는 뜻입니다."
            )
    else:
        confidence = EvaluationConfidence.HIGH

    if direction is SignalDirection.DISAGREE and confidence is EvaluationConfidence.HIGH:
        # 신호가 갈리면 높은 신뢰도로 내보내지 않는다(spec §5.2).
        confidence = EvaluationConfidence.MEDIUM

    return TemporalSignal(
        confidence=confidence,
        robustness_note=" ".join(notes) if notes else None,
        direction_vs_offline_metric=direction,
    )


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
    # 시간축 신호(#485 §5). 없으면 None — temporal 평가를 돌리지 않은 실행이다.
    # **`verdict`에 영향을 주지 않는다**(병기 신호). 그래서 `evaluation_id` 해시
    # payload에도 넣지 않는다 — 같은 통계·같은 판정이면 같은 id여야 하고, temporal이
    # 실제로 판정을 바꾸게 되면(#514/#425 확장) 그때 id 근거도 함께 바꾼다.
    temporal_signal: TemporalSignal | None = None
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
    plan_receipt: ExperimentPlanReceipt,
    observations: tuple[PairedSeedObservation, ...],
) -> PairedSeedEvidence:
    """plan receipt 좌표와 comparison ID로 이식 가능한 증거 레코드를 만든다."""
    payload = {
        "plan_receipt": _model_payload(plan_receipt),
        "observations": [
            {
                "seed": observation.seed,
                "comparison_id": observation.comparison.comparison_id,
            }
            for observation in observations
        ],
    }
    return PairedSeedEvidence(
        evidence_id=_stable_id("paired-seed-evidence", payload),
        plan_receipt=plan_receipt,
        observations=observations,
    )


def _failed_evaluation(
    *,
    plan_id: str,
    evidence: PairedSeedEvidence,
    reason_codes: set[EvaluationReasonCode],
    evaluated_at: datetime,
    temporal_signal: TemporalSignal | None = None,
) -> ExperimentEvaluation:
    """근거가 불완전할 때 통계를 추정하지 않는 fail-closed 평가를 만든다.

    ``temporal_signal``은 hold 결과에도 그대로 싣는다 — 호출부가 준 정보를 조용히
    버리지 않는다. hold 사유(통계 근거 부족)와 temporal 신호는 서로 다른 축이다.
    """
    reasons = tuple(sorted(reason_codes, key=lambda reason: reason.value))
    payload = {
        "evidence_id": evidence.evidence_id,
        "plan_id": plan_id,
        "policy_version": POLICY_VERSION,
        "paired": False,
        "verdict": EvaluationVerdict.HOLD.value,
        "reason_codes": [reason.value for reason in reasons],
    }
    return ExperimentEvaluation(
        evaluation_id=_stable_id("experiment-evaluation", payload),
        evidence_id=evidence.evidence_id,
        plan_id=plan_id,
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
        temporal_signal=temporal_signal,
        evaluated_at=evaluated_at,
    )


def _evidence_reason_codes(
    *, plan: ExperimentPlan, evidence: PairedSeedEvidence, policy: PromotionPolicy
) -> set[EvaluationReasonCode]:
    """자동 승격에 필요한 provenance·사전 선언·짝지음 근거를 검사한다."""
    reasons: set[EvaluationReasonCode] = set()
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

        if comparison.promotion_evidence is None:
            reasons.add(EvaluationReasonCode.PLAN_RECEIPT_MISSING)

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

    if len(snapshot_hashes) != 1:
        reasons.add(EvaluationReasonCode.SNAPSHOT_MISMATCH)
    return reasons


def _verified_metric_values(
    observation: PairedSeedObservation,
    *,
    store: PromotionEvidenceStore,
    plan_receipt: ExperimentPlanReceipt,
) -> tuple[float, float]:
    """comparison receipt를 GCS에서 다시 읽어 실제 paired ROC-AUC만 반환한다."""
    promotion = observation.comparison.promotion_evidence
    if promotion is None:
        raise PromotionEvidenceValidationError("comparison promotion evidence가 없습니다")
    if promotion.plan_receipt != plan_receipt:
        raise ValueError("comparison plan receipt가 evidence와 다릅니다")
    baseline = store.verify_held_out_metric_receipt(promotion.baseline_metric)
    challenger = store.verify_held_out_metric_receipt(promotion.challenger_metric)
    comparison = observation.comparison
    if (
        baseline.plan_receipt != plan_receipt
        or challenger.plan_receipt != plan_receipt
        or baseline.run_id != comparison.baseline_run_id
        or challenger.run_id != comparison.challenger_run_id
        or baseline.split_manifest_sha256 != comparison.baseline_split_manifest_sha256
        or challenger.split_manifest_sha256 != comparison.challenger_split_manifest_sha256
        or baseline.metric_name != PRIMARY_METRIC
        or challenger.metric_name != PRIMARY_METRIC
        or baseline.dataset_split != "test"
        or challenger.dataset_split != "test"
        or not isfinite(baseline.value)
        or not isfinite(challenger.value)
    ):
        raise ValueError("verified metric가 comparison binding과 다릅니다")
    return baseline.value, challenger.value


def evaluate_experiment(
    evidence: PairedSeedEvidence,
    *,
    promotion_evidence_store: PromotionEvidenceStore,
    evaluated_at: datetime | None = None,
    temporal_signal: TemporalSignal | None = None,
) -> ExperimentEvaluation:
    """고정된 v1 정책으로 실험 증거를 평가한다.

    `eligible`은 평균 ROC-AUC 개선과 양측 95% 신뢰구간 하한의 양수를 모두
    만족할 때만 나온다. 근거가 하나라도 누락되면 통계를 보정하거나 추가 실행하지
    않고 `hold`로 fail-closed 한다.

    ``temporal_signal``(#485 §5)은 결과에 **그대로 실려 나갈 뿐 판정을 바꾸지
    않는다**. `#485` 범위에서 temporal은 병기 신호이고, 주 지표 일반화는 `#493`이
    소유한다. 호출부가 ``summarize_temporal_signal``로 만들어 넘긴다 — 이 모듈은
    ``degradation_eval``을 import하지 않는다(ML 의존을 판정 경로에 들이지 않기 위해).
    """
    policy = promotion_policy_v1()
    evaluation_time = _normalize_utc_datetime(evaluated_at or _utc_now())
    plan_id = evidence.plan_receipt.plan.plan_id
    try:
        plan = promotion_evidence_store.verify_plan_receipt(evidence.plan_receipt)
    except PromotionEvidenceValidationError:
        return _failed_evaluation(
            plan_id=plan_id,
            evidence=evidence,
            reason_codes={EvaluationReasonCode.RECEIPT_REVALIDATION_FAILED},
            evaluated_at=evaluation_time,
            temporal_signal=temporal_signal,
        )
    reasons: set[EvaluationReasonCode] = set()
    canonical_observations: list[PairedSeedObservation] = []
    for observation in evidence.observations:
        if observation.comparison.promotion_evidence is None:
            reasons.add(EvaluationReasonCode.PLAN_RECEIPT_MISSING)
            continue
        try:
            canonical_comparison = revalidate_training_comparison(
                observation.comparison,
                promotion_evidence_store=promotion_evidence_store,
            )
        except ComparisonValidationError:
            reasons.add(EvaluationReasonCode.RECEIPT_REVALIDATION_FAILED)
            continue
        canonical_observations.append(
            observation.model_copy(update={"comparison": canonical_comparison})
        )
    if reasons:
        return _failed_evaluation(
            plan_id=plan.plan_id,
            evidence=evidence,
            reason_codes=reasons,
            evaluated_at=evaluation_time,
            temporal_signal=temporal_signal,
        )
    canonical_evidence = evidence.model_copy(
        update={"observations": tuple(canonical_observations)}
    )
    reasons = _evidence_reason_codes(
        plan=plan, evidence=canonical_evidence, policy=policy
    )
    if reasons:
        return _failed_evaluation(
            plan_id=plan.plan_id,
            evidence=evidence,
            reason_codes=reasons,
            evaluated_at=evaluation_time,
            temporal_signal=temporal_signal,
        )

    metric_values: list[tuple[float, float]] = []
    for observation in canonical_evidence.observations:
        try:
            metric_values.append(
                _verified_metric_values(
                    observation,
                    store=promotion_evidence_store,
                    plan_receipt=evidence.plan_receipt,
                )
            )
        except PromotionEvidenceValidationError:
            reasons.add(EvaluationReasonCode.RECEIPT_REVALIDATION_FAILED)
        except ValueError:
            reasons.add(EvaluationReasonCode.METRIC_BINDING_MISMATCH)
    if reasons:
        return _failed_evaluation(
            plan_id=plan.plan_id,
            evidence=evidence,
            reason_codes=reasons,
            evaluated_at=evaluation_time,
            temporal_signal=temporal_signal,
        )

    baseline_values = tuple(values[0] for values in metric_values)
    challenger_values = tuple(values[1] for values in metric_values)
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
        temporal_signal=temporal_signal,
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
