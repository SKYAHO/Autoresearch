"""paired offline 실험의 요청 검증·판정 사상·결과 payload 생성 (#454).

[파이프라인] 실험 실행 **이후** 구간을 담당한다. baseline/candidate 조건이 각자의
이미지·코드 아카이브·Feast Registry로 학습을 끝내고 나면, 이 모듈이 seed별 두 run을
짝지어 공정성을 재검증하고(`training_comparison`), 사전 선언된 정책으로 판정한 뒤
(`experiment_evaluation`), 후속 dev 입성 게이트가 소비할 단일 결과 payload를 만든다.

[기능] `PairedExperimentRequest`는 조건별 lineage와 seed별 run 좌표를 받는 실행 요청
계약이고, `evaluate_paired_experiment`는 요청 검증 → comparison 재검증 → 판정 →
`comparison_passed`/`comparison_rejected`/`comparison_failed` 사상을 수행한다.
`write_result`는 결과를 원자적으로 게시한다.

[비책임] 학습·평가 실행 자체와 조건별 이미지 빌드, Job 오케스트레이션(Airflow),
GCS/BigQuery IAM(infra), champion alias 이동(#470)은 이 모듈이 다루지 않는다. 통계
판정 규칙과 승격 정책은 `experiment_evaluation`이, 두 run의 provenance equality는
`training_comparison`이, Registry 좌표 규칙은 `autoresearch.experiments.context`가
소유한다 — 여기서 재정의하지 않는다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from autoresearch.experiments.context import BASELINE, CANDIDATE, registry_uri_matches
from src.pipeline.experiment_evaluation import (
    POLICY_SEEDS,
    POLICY_VERSION,
    EvaluationVerdict,
    ExperimentEvaluation,
    PairedSeedObservation,
    create_paired_seed_evidence,
    evaluate_experiment,
)
from src.pipeline.promotion_evidence import ExperimentPlanReceipt, PromotionEvidenceStore
from src.pipeline.training_comparison import (
    ComparisonValidationError,
    verify_training_comparison,
)
from src.pipeline.training_provenance import write_manifest_atomic


CONTRACT_VERSION = "paired-offline-experiment-v1"
RESULT_CONTRACT_VERSION = "paired-offline-experiment-result-v1"

OUTCOME_PASSED = "comparison_passed"
OUTCOME_REJECTED = "comparison_rejected"
OUTCOME_FAILED = "comparison_failed"

_SHA_PATTERN = r"^[0-9a-f]{40}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_EXPERIMENT_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,31}$"
_RUN_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"

# 판정 엔진의 verdict를 실행 계약의 outcome으로 사상한다. `hold`(판정 불가)는
# 성공이 아니다 — 후속 게이트가 승격 후보로 읽지 않도록 실패로 내린다.
_VERDICT_OUTCOMES = {
    EvaluationVerdict.ELIGIBLE: OUTCOME_PASSED,
    EvaluationVerdict.REJECT: OUTCOME_REJECTED,
    EvaluationVerdict.HOLD: OUTCOME_FAILED,
}


class PairedExperimentReason(str, Enum):
    """실행 요청·lineage 단계에서 결정되는 fail-closed 사유."""

    CONDITION_SOURCE_SHA_MISMATCH = "condition_source_sha_mismatch"
    CODE_ARCHIVE_SHA_MISMATCH = "code_archive_sha_mismatch"
    REGISTRY_URI_MISMATCH = "registry_uri_mismatch"
    REGISTRY_NOT_ISOLATED = "registry_not_isolated"
    MISSING_PAIRED_RUN = "missing_paired_run"
    DECLARED_FEATURES_ABSENT = "declared_features_absent"
    UNDECLARED_FEATURE_SCHEMA_DIFFERENCE = "undeclared_feature_schema_difference"
    COMPARISON_VERIFICATION_FAILED = "comparison_verification_failed"
    MODEL_URI_MISSING = "model_uri_missing"


class _ContractModel(BaseModel):
    """실행 계약에 적용하는 불변 pydantic 기본 설정."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class ConditionLineage(_ContractModel):
    """하나의 조건이 실제로 실행한 코드·이미지·Registry 좌표."""

    source_sha: str = Field(pattern=_SHA_PATTERN)
    image_digest: str = Field(pattern=_DIGEST_PATTERN)
    code_archive_sha: str = Field(pattern=_SHA_PATTERN)
    code_archive_uri: str = Field(min_length=1)
    registry_uri: str = Field(min_length=1)
    feature_schema_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    # 승격 후보를 지목하려면 candidate 조건에 불변 모델 식별자가 있어야 한다.
    model_uri: str | None = Field(default=None, min_length=1)


class SeedRun(_ContractModel):
    """하나의 seed에서 짝지어 실행한 baseline/candidate run 좌표."""

    seed: int
    run_id: str = Field(pattern=_RUN_ID_PATTERN)
    baseline_mlflow_run_id: str = Field(min_length=1)
    candidate_mlflow_run_id: str = Field(min_length=1)
    artifact_uri: str = Field(min_length=1)
    log_uri: str = Field(min_length=1)


class SeedRunResult(_ContractModel):
    """결과 payload가 남기는 seed별 실행·검증 좌표."""

    seed: int
    run_id: str
    comparison_id: str | None
    artifact_uri: str
    log_uri: str


class PairedExperimentRequest(_ContractModel):
    """조건별 실행이 끝난 뒤 비교·판정을 요청하는 계약."""

    contract_version: Literal["paired-offline-experiment-v1"] = CONTRACT_VERSION
    issue_number: int = Field(gt=0)
    issue_branch: str = Field(min_length=1)
    experiment_id: str = Field(pattern=_EXPERIMENT_ID_PATTERN)
    base_dev_sha: str = Field(pattern=_SHA_PATTERN)
    candidate_sha: str = Field(pattern=_SHA_PATTERN)
    feature_service: str = Field(min_length=1)
    extra_features: tuple[str, ...] = ()
    dataset_snapshot_uri: str = Field(min_length=1)
    dataset_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    split_hash: str = Field(pattern=_SHA256_PATTERN)
    training_config_fingerprint: str = Field(pattern=_SHA256_PATTERN)
    plan_receipt: ExperimentPlanReceipt
    baseline: ConditionLineage
    candidate: ConditionLineage
    runs: tuple[SeedRun, ...] = Field(min_length=1)

    @field_validator("extra_features")
    @classmethod
    def _validate_extra_features(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        names = [name.strip() for name in value]
        if any(not name for name in names):
            raise ValueError("extra_features에 빈 이름을 넣을 수 없습니다")
        if "clicked" in names:
            raise ValueError("extra_features에 라벨 컬럼(clicked)을 넣을 수 없습니다")
        if len(set(names)) != len(names):
            raise ValueError("extra_features에 중복된 이름이 있습니다")
        return tuple(names)

    @field_validator("runs")
    @classmethod
    def _validate_unique_seeds(cls, value: tuple[SeedRun, ...]) -> tuple[SeedRun, ...]:
        seeds = [run.seed for run in value]
        if len(set(seeds)) != len(seeds):
            raise ValueError("같은 seed를 두 번 실행한 결과는 짝지을 수 없습니다")
        return value


class PairedExperimentResult(_ContractModel):
    """dev 입성 게이트가 소비하는 단일 비교 결과."""

    contract_version: Literal["paired-offline-experiment-result-v1"] = (
        RESULT_CONTRACT_VERSION
    )
    outcome: Literal["comparison_passed", "comparison_rejected", "comparison_failed"]
    decision_reason: str
    reason_codes: tuple[str, ...]
    issue_number: int
    issue_branch: str
    experiment_id: str
    base_dev_sha: str
    candidate_sha: str
    baseline: ConditionLineage
    candidate: ConditionLineage
    feature_service: str
    extra_features: tuple[str, ...]
    dataset_snapshot_uri: str
    dataset_fingerprint: str
    split_hash: str
    training_config_fingerprint: str
    plan_id: str
    policy_version: str = POLICY_VERSION
    metric_name: str | None = None
    primary_baseline: float | None = None
    primary_candidate: float | None = None
    paired_delta_mean: float | None = None
    confidence_interval_lower: float | None = None
    confidence_interval_upper: float | None = None
    seeds: tuple[int, ...]
    runs: tuple[SeedRunResult, ...]
    model_uri: str | None = None
    evaluated_at: datetime


def _validate_condition(
    lineage: ConditionLineage,
    *,
    condition: str,
    expected_sha: str,
    issue_number: int,
    experiment_id: str,
) -> list[PairedExperimentReason]:
    """한 조건의 코드·Registry lineage가 선언과 같은지 확인한다."""
    reasons: list[PairedExperimentReason] = []
    if lineage.source_sha != expected_sha:
        reasons.append(PairedExperimentReason.CONDITION_SOURCE_SHA_MISMATCH)
    # bootstrap이 code/latest.txt로 fallback하면 candidate 코드가 아니라 main 코드가
    # 실행된다. 그 실행은 성공처럼 보이므로 SHA 일치를 명시적으로 요구한다(#454).
    if lineage.code_archive_sha != lineage.source_sha:
        reasons.append(PairedExperimentReason.CODE_ARCHIVE_SHA_MISMATCH)
    if not registry_uri_matches(
        lineage.registry_uri,
        issue_number=issue_number,
        experiment_id=experiment_id,
        condition=condition,
        source_sha=lineage.source_sha,
    ):
        reasons.append(PairedExperimentReason.REGISTRY_URI_MISMATCH)
    return reasons


def _validate_request(request: PairedExperimentRequest) -> list[PairedExperimentReason]:
    """판정 엔진을 부르기 전에 확인할 수 있는 fail-closed 조건을 모두 검사한다."""
    reasons: list[PairedExperimentReason] = []
    reasons.extend(
        _validate_condition(
            request.baseline,
            condition=BASELINE,
            expected_sha=request.base_dev_sha,
            issue_number=request.issue_number,
            experiment_id=request.experiment_id,
        )
    )
    reasons.extend(
        _validate_condition(
            request.candidate,
            condition=CANDIDATE,
            expected_sha=request.candidate_sha,
            issue_number=request.issue_number,
            experiment_id=request.experiment_id,
        )
    )
    if request.baseline.registry_uri == request.candidate.registry_uri:
        reasons.append(PairedExperimentReason.REGISTRY_NOT_ISOLATED)

    missing = sorted(set(POLICY_SEEDS) - {run.seed for run in request.runs})
    if missing:
        reasons.append(PairedExperimentReason.MISSING_PAIRED_RUN)

    # 학습 스키마 차이는 선언한 실험 피처로만 설명돼야 한다. 선언했는데 스키마가
    # 같으면 그 피처가 학습 CSV까지 오지 못한 것이고(조립 절단), 선언하지 않았는데
    # 다르면 비교 조건이 어긋난 것이다.
    schemas_differ = (
        request.baseline.feature_schema_fingerprint
        != request.candidate.feature_schema_fingerprint
    )
    if request.extra_features and not schemas_differ:
        reasons.append(PairedExperimentReason.DECLARED_FEATURES_ABSENT)
    if not request.extra_features and schemas_differ:
        reasons.append(PairedExperimentReason.UNDECLARED_FEATURE_SCHEMA_DIFFERENCE)

    if request.candidate.model_uri is None:
        reasons.append(PairedExperimentReason.MODEL_URI_MISSING)
    return reasons


def _result(
    request: PairedExperimentRequest,
    *,
    outcome: str,
    reasons: tuple[str, ...],
    evaluated_at: datetime,
    comparisons: dict[int, str] | None = None,
    evaluation: ExperimentEvaluation | None = None,
) -> PairedExperimentResult:
    """실패·기각·통과가 같은 형식과 lineage를 갖도록 결과를 조립한다."""
    resolved = comparisons or {}
    return PairedExperimentResult(
        outcome=outcome,
        decision_reason=reasons[0] if reasons else "criteria_met",
        reason_codes=reasons,
        issue_number=request.issue_number,
        issue_branch=request.issue_branch,
        experiment_id=request.experiment_id,
        base_dev_sha=request.base_dev_sha,
        candidate_sha=request.candidate_sha,
        baseline=request.baseline,
        candidate=request.candidate,
        feature_service=request.feature_service,
        extra_features=request.extra_features,
        dataset_snapshot_uri=request.dataset_snapshot_uri,
        dataset_fingerprint=request.dataset_fingerprint,
        split_hash=request.split_hash,
        training_config_fingerprint=request.training_config_fingerprint,
        plan_id=request.plan_receipt.plan.plan_id,
        metric_name=evaluation.metric_name if evaluation else None,
        primary_baseline=evaluation.baseline_mean if evaluation else None,
        primary_candidate=evaluation.challenger_mean if evaluation else None,
        paired_delta_mean=evaluation.paired_delta_mean if evaluation else None,
        confidence_interval_lower=(
            evaluation.confidence_interval_lower if evaluation else None
        ),
        confidence_interval_upper=(
            evaluation.confidence_interval_upper if evaluation else None
        ),
        seeds=tuple(run.seed for run in request.runs),
        runs=tuple(
            SeedRunResult(
                seed=run.seed,
                run_id=run.run_id,
                comparison_id=resolved.get(run.seed),
                artifact_uri=run.artifact_uri,
                log_uri=run.log_uri,
            )
            for run in request.runs
        ),
        # 승격 후보 식별자는 통과한 결과에만 싣는다 — 실패·기각 payload가 승격
        # 입력으로 재사용되는 사고를 구조적으로 막는다.
        model_uri=request.candidate.model_uri if outcome == OUTCOME_PASSED else None,
        evaluated_at=evaluated_at,
    )


def evaluate_paired_experiment(
    request: PairedExperimentRequest,
    *,
    promotion_evidence_store: PromotionEvidenceStore,
    workspace: Path,
    evaluated_at: datetime | None = None,
) -> PairedExperimentResult:
    """paired 실행 결과를 검증·판정해 단일 결과 payload를 만든다.

    Args:
        request: 조건별 lineage와 seed별 run 좌표를 담은 실행 요청.
        promotion_evidence_store: comparison·판정 재검증에 쓰는 write-once evidence store.
        workspace: seed별 verified comparison manifest를 게시할 로컬 디렉터리.
        evaluated_at: 결과에 기록할 UTC 시각(생략 시 판정 시각).

    Returns:
        `comparison_passed`/`comparison_rejected`/`comparison_failed` 중 하나의 결과.
        요청 검증이나 comparison 재검증이 실패하면 판정 엔진을 부르지 않는다 —
        판정할 수 없는 상태를 통과로 해석하지 않기 위해서다.
    """
    request_reasons = _validate_request(request)
    decided_at = evaluated_at or datetime.now(timezone.utc)
    if request_reasons:
        return _result(
            request,
            outcome=OUTCOME_FAILED,
            reasons=tuple(reason.value for reason in request_reasons),
            evaluated_at=decided_at,
        )

    workspace.mkdir(parents=True, exist_ok=True)
    observations: list[PairedSeedObservation] = []
    comparisons: dict[int, str] = {}
    for run in sorted(request.runs, key=lambda item: item.seed):
        try:
            comparison = verify_training_comparison(
                run.baseline_mlflow_run_id,
                run.candidate_mlflow_run_id,
                workspace / f"comparison-seed-{run.seed}.json",
                promotion_evidence_store=promotion_evidence_store,
            )
        except ComparisonValidationError:
            # 예외 원문에는 backend credential이나 signed URL이 포함될 수 있으므로
            # 결과 payload에 복사하지 않고 안정된 사유 코드만 남긴다.
            return _result(
                request,
                outcome=OUTCOME_FAILED,
                reasons=(PairedExperimentReason.COMPARISON_VERIFICATION_FAILED.value,),
                evaluated_at=decided_at,
                comparisons=comparisons,
            )
        comparisons[run.seed] = comparison.comparison_id
        observations.append(PairedSeedObservation(seed=run.seed, comparison=comparison))

    evidence = create_paired_seed_evidence(
        plan_receipt=request.plan_receipt,
        observations=tuple(observations),
    )
    evaluation = evaluate_experiment(
        evidence,
        promotion_evidence_store=promotion_evidence_store,
        evaluated_at=evaluated_at,
    )
    return _result(
        request,
        outcome=_VERDICT_OUTCOMES[evaluation.verdict],
        reasons=tuple(reason.value for reason in evaluation.reason_codes),
        evaluated_at=evaluation.evaluated_at,
        comparisons=comparisons,
        evaluation=evaluation,
    )


def write_result(result: PairedExperimentResult, output_path: Path) -> None:
    """결과 payload를 원자적으로 게시한다(부분 파일을 남기지 않는다)."""
    write_manifest_atomic(result, Path(output_path))
