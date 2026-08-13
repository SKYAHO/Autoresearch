"""paired offline 실험의 요청 검증·판정 사상·결과 payload를 검증한다(#454).

MLflow·GCS 없이 경계만 대체한다: comparison 검증(`verify_training_comparison`)과
판정 엔진(`evaluate_experiment`)을 monkeypatch로 바꾸고, 이 모듈이 소유한 요청
검증·재검증 대조·fail-closed 규칙·outcome 사상·payload 구성을 본다. 통계 판정
자체는 tests/test_pipeline_experiment_evaluation.py가 검증한다.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from autoresearch.model_evaluation import paired_experiment
from autoresearch.model_evaluation.experiment_evaluation import (
    POLICY_SEEDS,
    EvaluationReasonCode,
    EvaluationVerdict,
    ExperimentEvaluation,
)
from autoresearch.model_evaluation.paired_experiment import (
    PairedExperimentReason,
    PairedExperimentRequest,
    evaluate_paired_experiment,
)
from autoresearch.model_evaluation.promotion_evidence import (
    ExperimentPlanReceipt,
    GcsObjectReceipt,
    create_experiment_plan,
)
from autoresearch.model_evaluation.training_comparison import ComparisonValidationError
from autoresearch.model_training.training_provenance import TrainingComparisonManifest, TrainingSeeds


BASE_SHA = "a" * 40
CANDIDATE_SHA = "b" * 40
DIGEST = "sha256:" + "c" * 64
BASELINE_FINGERPRINT = "d" * 64
CANDIDATE_FINGERPRINT = "e" * 64
DATASET_FINGERPRINT = "1" * 64
SPLIT_HASH = "2" * 64
REGISTRY_ROOT = "gs://registry-bucket"
ARTIFACT_ROOT = "gs://artifact-bucket"
MODEL_URI = "models:/ctr-model/12"
PROD_COLUMNS = ("view_count", "like_count")
EXPERIMENT_COLUMN = "views_per_day"
EVALUATED_AT = datetime(2026, 8, 3, tzinfo=timezone.utc)


def _plan_receipt() -> ExperimentPlanReceipt:
    plan = create_experiment_plan(
        hypothesis_id="issue-449",
        control_id=BASE_SHA,
        candidate_ids=(CANDIDATE_SHA,),
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    return ExperimentPlanReceipt(
        plan=plan,
        object=GcsObjectReceipt(
            uri=f"gs://evidence-bucket/experiments/plans/{plan.plan_id}.json",
            generation="1",
            metageneration="1",
            time_created=datetime(2026, 8, 1, tzinfo=timezone.utc),
            sha256="f" * 64,
        ),
    )


def _registry_uri(condition: str, source_sha: str) -> str:
    return (
        f"{REGISTRY_ROOT}/experiments/449/primary/{condition}/{source_sha}/registry.db"
    )


def _condition(condition: str, source_sha: str, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "source_sha": source_sha,
        "image_digest": DIGEST,
        "code_archive_sha": source_sha,
        "code_archive_uri": f"gs://code-bucket/code/{source_sha}.tar.gz",
        "registry_uri": _registry_uri(condition, source_sha),
        "feature_schema_fingerprint": (
            BASELINE_FINGERPRINT if condition == "baseline" else CANDIDATE_FINGERPRINT
        ),
    }
    if condition == "candidate":
        values["model_uri"] = MODEL_URI
    values.update(overrides)
    return values


def _runs(seeds: tuple[int, ...] = POLICY_SEEDS) -> list[dict[str, object]]:
    return [
        {
            "seed": seed,
            "run_id": f"seed-{seed}",
            "baseline_mlflow_run_id": f"baseline-{seed}",
            "candidate_mlflow_run_id": f"candidate-{seed}",
            "artifact_uri": f"{ARTIFACT_ROOT}/experiments/449/primary/candidate/{CANDIDATE_SHA}/seed-{seed}/",
            "log_uri": f"{ARTIFACT_ROOT}/experiments/449/primary/candidate/{CANDIDATE_SHA}/seed-{seed}/log.txt",
        }
        for seed in seeds
    ]


def _request(**overrides: object) -> PairedExperimentRequest:
    payload: dict[str, object] = {
        "contract_version": "paired-offline-experiment-v1",
        "issue_number": 449,
        "issue_branch": "exp/449-example",
        "experiment_id": "primary",
        "base_dev_sha": BASE_SHA,
        "candidate_sha": CANDIDATE_SHA,
        "feature_service": "ctr_training_v1",
        "extra_features": [EXPERIMENT_COLUMN],
        "registry_root": REGISTRY_ROOT,
        "dataset_snapshot_uri": "gs://artifact-bucket/snapshots/manifest.json",
        "dataset_fingerprint": DATASET_FINGERPRINT,
        "split_hash": SPLIT_HASH,
        "training_config_fingerprint": "3" * 64,
        "plan_receipt": _plan_receipt().model_dump(mode="json"),
        "baseline": _condition("baseline", BASE_SHA),
        "candidate": _condition("candidate", CANDIDATE_SHA),
        "runs": _runs(),
    }
    payload.update(overrides)
    return PairedExperimentRequest.model_validate(payload)


def _comparison(seed: int, **overrides: object) -> TrainingComparisonManifest:
    """재검증이 MLflow artifact에서 다시 만든 comparison manifest의 test double."""
    values: dict[str, object] = {
        "comparison_id": f"comparison-{seed}",
        "baseline_run_id": f"baseline-{seed}",
        "challenger_run_id": f"candidate-{seed}",
        "baseline_snapshot_sha256": DATASET_FINGERPRINT,
        "challenger_snapshot_sha256": DATASET_FINGERPRINT,
        "baseline_snapshot_manifest_sha256": "4" * 64,
        "challenger_snapshot_manifest_sha256": "4" * 64,
        "baseline_split_manifest_sha256": SPLIT_HASH,
        "challenger_split_manifest_sha256": SPLIT_HASH,
        "baseline_feature_columns_sha256": BASELINE_FINGERPRINT,
        "challenger_feature_columns_sha256": CANDIDATE_FINGERPRINT,
        "baseline_feature_columns": list(PROD_COLUMNS),
        "challenger_feature_columns": [*PROD_COLUMNS, EXPERIMENT_COLUMN],
        "effective_seeds": TrainingSeeds(
            split_seed=seed, model_seed=seed, sampler_seed=seed
        ),
        "validated_at": EVALUATED_AT,
    }
    values.update(overrides)
    return TrainingComparisonManifest.model_validate(values)


def _evaluation(
    verdict: EvaluationVerdict,
    reason: EvaluationReasonCode,
) -> ExperimentEvaluation:
    return ExperimentEvaluation(
        evaluation_id="evaluation-1",
        evidence_id="evidence-1",
        plan_id=_plan_receipt().plan.plan_id,
        paired=True,
        baseline_mean=0.7780,
        challenger_mean=0.7812,
        paired_delta_mean=0.0032,
        standard_error=0.0004,
        t_critical=2.045,
        confidence_interval_lower=0.0024,
        confidence_interval_upper=0.0040,
        verdict=verdict,
        reason_codes=(reason,),
        evaluated_at=EVALUATED_AT,
    )


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """comparison 검증과 판정 엔진을 대체하고 호출 이력을 남긴다.

    `comparison_overrides`를 바꾸면 재검증이 돌려주는 manifest를 조정할 수 있어,
    "요청이 신고한 값과 실제 run이 다르다"는 상황을 만들 수 있다.
    """
    calls: dict[str, object] = {"verify": [], "evaluate": [], "comparison_overrides": {}}

    def fake_verify(
        baseline_run_id: str,
        challenger_run_id: str,
        output_path: Path,
        **kwargs: object,
    ) -> TrainingComparisonManifest:
        calls["verify"].append((baseline_run_id, challenger_run_id))
        seed = int(str(baseline_run_id).rsplit("-", 1)[-1])
        return _comparison(seed, **calls["comparison_overrides"])

    def fake_evaluate(evidence: object, **kwargs: object) -> ExperimentEvaluation:
        calls["evaluate"].append(evidence)
        return _evaluation(
            EvaluationVerdict.ELIGIBLE,
            EvaluationReasonCode.PRIMARY_ROC_AUC_IMPROVED_WITH_95PCT_CONFIDENCE,
        )

    monkeypatch.setattr(paired_experiment, "verify_training_comparison", fake_verify)
    monkeypatch.setattr(paired_experiment, "evaluate_experiment", fake_evaluate)
    return calls


def _evaluate(request: PairedExperimentRequest, tmp_path: Path):
    return evaluate_paired_experiment(
        request,
        promotion_evidence_store=object(),
        workspace=tmp_path,
        evaluated_at=EVALUATED_AT,
    )


def test_eligible_verdict_becomes_comparison_passed(tmp_path, stubbed) -> None:
    result = _evaluate(_request(), tmp_path)

    assert result.outcome == "comparison_passed"
    assert result.primary_baseline == pytest.approx(0.7780)
    assert result.primary_candidate == pytest.approx(0.7812)
    assert result.paired_delta_mean == pytest.approx(0.0032)
    assert result.model_uri == MODEL_URI
    assert len(stubbed["verify"]) == len(POLICY_SEEDS)


def test_passed_result_carries_audit_identifiers(tmp_path, stubbed) -> None:
    # 판정 레코드를 되짚을 수 있어야 한다 — plan_id만으로는 같은 plan의 재실행을
    # 구분하지 못한다.
    result = _evaluate(_request(), tmp_path)

    assert result.evidence_id == "evidence-1"
    assert result.evaluation_id == "evaluation-1"
    assert result.decision_id is not None


def test_reject_verdict_becomes_comparison_rejected(tmp_path, monkeypatch, stubbed) -> None:
    monkeypatch.setattr(
        paired_experiment,
        "evaluate_experiment",
        lambda evidence, **kwargs: _evaluation(
            EvaluationVerdict.REJECT, EvaluationReasonCode.PRIMARY_ROC_AUC_NOT_IMPROVED
        ),
    )

    result = _evaluate(_request(), tmp_path)

    assert result.outcome == "comparison_rejected"
    assert result.decision_reason == "primary_roc_auc_not_improved"
    assert result.model_uri is None
    # 조건 lineage에도 승격 후보 식별자를 남기지 않는다.
    assert result.candidate.model_uri is None
    assert result.candidate.image_digest == DIGEST


def test_hold_verdict_becomes_comparison_failed(tmp_path, monkeypatch, stubbed) -> None:
    monkeypatch.setattr(
        paired_experiment,
        "evaluate_experiment",
        lambda evidence, **kwargs: _evaluation(
            EvaluationVerdict.HOLD, EvaluationReasonCode.SEED_POLICY_MISMATCH
        ),
    )

    result = _evaluate(_request(), tmp_path)

    # 판정 불가는 통과가 아니다 — 후속 게이트가 승격 후보로 읽지 않도록 실패로 남긴다.
    assert result.outcome == "comparison_failed"
    assert result.reason_codes == ("seed_policy_mismatch",)


def test_code_archive_sha_must_equal_condition_source_sha(tmp_path, stubbed) -> None:
    request = _request(
        candidate=_condition("candidate", CANDIDATE_SHA, code_archive_sha="0" * 40)
    )

    result = _evaluate(request, tmp_path)

    assert result.outcome == "comparison_failed"
    assert PairedExperimentReason.CODE_ARCHIVE_SHA_MISMATCH.value in result.reason_codes
    # 비싼 comparison 검증까지 가지 않고 요청 단계에서 멈춘다.
    assert stubbed["verify"] == []


@pytest.mark.parametrize(
    "registry_uri",
    [
        # 다른 이슈·실험 좌표
        "gs://registry-bucket/experiments/999/other/candidate/" + "b" * 40 + "/registry.db",
        # 선언한 root 밖의 bucket
        "gs://other-bucket/experiments/449/primary/candidate/" + "b" * 40 + "/registry.db",
        # userinfo가 박힌 URI(결과·로그로 그대로 나간다)
        "gs://user:token@registry-bucket/experiments/449/primary/candidate/"
        + "b" * 40
        + "/registry.db",
        # 스킴 위조
        "https://registry-bucket/experiments/449/primary/candidate/" + "b" * 40 + "/registry.db",
        # 같은 좌표를 주장하는 다른 object(이중 슬래시·끝 슬래시)
        "gs://registry-bucket//experiments/449/primary/candidate/" + "b" * 40 + "/registry.db",
        "gs://registry-bucket/experiments/449/primary/candidate/" + "b" * 40 + "/registry.db/",
        # baseline 조건 경로를 candidate로 신고
        "gs://registry-bucket/experiments/449/primary/baseline/" + "b" * 40 + "/registry.db",
    ],
)
def test_candidate_registry_must_match_declared_coordinates(
    tmp_path, stubbed, registry_uri: str
) -> None:
    request = _request(
        candidate=_condition("candidate", CANDIDATE_SHA, registry_uri=registry_uri)
    )

    result = _evaluate(request, tmp_path)

    assert result.outcome == "comparison_failed"
    assert PairedExperimentReason.REGISTRY_URI_MISMATCH.value in result.reason_codes
    assert stubbed["verify"] == []


def test_legacy_candidate_registry_path_is_accepted(tmp_path, stubbed) -> None:
    request = _request(
        candidate=_condition(
            "candidate",
            CANDIDATE_SHA,
            registry_uri=f"{REGISTRY_ROOT}/experiments/449/primary/{CANDIDATE_SHA}/registry.db",
        )
    )

    result = _evaluate(request, tmp_path)

    assert result.outcome == "comparison_passed"


def test_baseline_may_not_use_legacy_registry_path(tmp_path, stubbed) -> None:
    # legacy 경로에는 조건 구간이 없어 candidate 산출물과 구분되지 않는다.
    request = _request(
        baseline=_condition(
            "baseline",
            BASE_SHA,
            registry_uri=f"{REGISTRY_ROOT}/experiments/449/primary/{BASE_SHA}/registry.db",
        )
    )

    result = _evaluate(request, tmp_path)

    assert result.outcome == "comparison_failed"
    assert PairedExperimentReason.REGISTRY_URI_MISMATCH.value in result.reason_codes


def test_missing_paired_seed_never_passes(tmp_path, stubbed) -> None:
    request = _request(runs=_runs(POLICY_SEEDS[:-1]))

    result = _evaluate(request, tmp_path)

    assert result.outcome == "comparison_failed"
    assert PairedExperimentReason.MISSING_PAIRED_RUN.value in result.reason_codes
    assert stubbed["verify"] == []


def test_seed_outside_policy_stops_before_expensive_verification(tmp_path, stubbed) -> None:
    request = _request(runs=_runs((*POLICY_SEEDS, 999)))

    result = _evaluate(request, tmp_path)

    assert result.outcome == "comparison_failed"
    assert PairedExperimentReason.SEED_POLICY_MISMATCH.value in result.reason_codes
    # 정책 밖 seed도 판정 엔진이 결국 거부하지만, 거기까지 가면 seed마다 MLflow·GCS
    # 재검증을 모두 수행한 뒤에야 실패한다.
    assert stubbed["verify"] == []


def test_duplicate_seed_is_rejected_by_request_model() -> None:
    with pytest.raises(ValueError, match="seed"):
        _request(runs=_runs((POLICY_SEEDS[0], POLICY_SEEDS[0])))


def test_declared_feature_must_appear_in_verified_training_columns(
    tmp_path, stubbed
) -> None:
    # 조립이 실험 피처를 잘라내 두 조건이 같은 21피처로 학습된 상황. 요청은 여전히
    # 서로 다른 fingerprint를 신고하므로, 자기신고 값만 보면 통과해 버린다.
    stubbed["comparison_overrides"] = {
        "challenger_feature_columns": list(PROD_COLUMNS),
        "challenger_feature_columns_sha256": CANDIDATE_FINGERPRINT,
    }

    result = _evaluate(_request(), tmp_path)

    assert result.outcome == "comparison_failed"
    assert PairedExperimentReason.DECLARED_FEATURES_ABSENT.value in result.reason_codes
    assert stubbed["evaluate"] == []


def test_undeclared_training_column_difference_is_rejected(tmp_path, stubbed) -> None:
    stubbed["comparison_overrides"] = {
        "challenger_feature_columns": [*PROD_COLUMNS, EXPERIMENT_COLUMN, "extra_column"],
    }

    result = _evaluate(_request(), tmp_path)

    assert result.outcome == "comparison_failed"
    assert (
        PairedExperimentReason.UNDECLARED_FEATURE_SCHEMA_DIFFERENCE.value
        in result.reason_codes
    )


def test_dropped_training_column_is_rejected(tmp_path, stubbed) -> None:
    # 컬럼이 빠지는 것은 어떤 실험 선언으로도 설명되지 않는다.
    stubbed["comparison_overrides"] = {
        "baseline_feature_columns": [*PROD_COLUMNS, "removed_column"],
    }

    result = _evaluate(_request(), tmp_path)

    assert result.outcome == "comparison_failed"
    assert (
        PairedExperimentReason.UNDECLARED_FEATURE_SCHEMA_DIFFERENCE.value
        in result.reason_codes
    )


def test_feature_schema_fingerprint_must_match_verified_manifest(
    tmp_path, stubbed
) -> None:
    stubbed["comparison_overrides"] = {"challenger_feature_columns_sha256": "9" * 64}

    result = _evaluate(_request(), tmp_path)

    assert result.outcome == "comparison_failed"
    assert (
        PairedExperimentReason.FEATURE_SCHEMA_FINGERPRINT_MISMATCH.value
        in result.reason_codes
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"baseline_snapshot_sha256": "9" * 64},
        {"baseline_split_manifest_sha256": "9" * 64},
    ],
)
def test_dataset_lineage_must_match_verified_manifest(
    tmp_path, stubbed, overrides: dict[str, str]
) -> None:
    # 결과 payload의 dataset lineage가 실제 run의 것과 다르면 그 자체로 거짓 기록이다.
    stubbed["comparison_overrides"] = overrides

    result = _evaluate(_request(), tmp_path)

    assert result.outcome == "comparison_failed"
    assert PairedExperimentReason.DATASET_LINEAGE_MISMATCH.value in result.reason_codes


def test_experiment_without_extra_features_requires_identical_columns(
    tmp_path, stubbed
) -> None:
    stubbed["comparison_overrides"] = {
        "baseline_feature_columns": list(PROD_COLUMNS),
        "challenger_feature_columns": list(PROD_COLUMNS),
        "challenger_feature_columns_sha256": BASELINE_FINGERPRINT,
    }
    request = _request(
        extra_features=[],
        candidate=_condition(
            "candidate", CANDIDATE_SHA, feature_schema_fingerprint=BASELINE_FINGERPRINT
        ),
    )

    result = _evaluate(request, tmp_path)

    assert result.outcome == "comparison_passed"


def test_comparison_verification_failure_is_fail_closed(
    tmp_path, monkeypatch, stubbed
) -> None:
    def fail_verify(*args: object, **kwargs: object) -> TrainingComparisonManifest:
        raise ComparisonValidationError("snapshot validation failed for run x")

    monkeypatch.setattr(paired_experiment, "verify_training_comparison", fail_verify)

    result = _evaluate(_request(), tmp_path)

    assert result.outcome == "comparison_failed"
    assert PairedExperimentReason.COMPARISON_VERIFICATION_FAILED.value in result.reason_codes
    # 백엔드 예외 원문(자격증명·signed URL 포함 가능)은 결과에 복사하지 않는다.
    assert "snapshot validation failed" not in json.dumps(result.model_dump(mode="json"))
    assert stubbed["evaluate"] == []


def test_passed_result_requires_immutable_model_identifier(tmp_path, stubbed) -> None:
    request = _request(
        candidate={
            key: value
            for key, value in _condition("candidate", CANDIDATE_SHA).items()
            if key != "model_uri"
        }
    )

    result = _evaluate(request, tmp_path)

    assert result.outcome == "comparison_failed"
    assert PairedExperimentReason.MODEL_URI_MISSING.value in result.reason_codes


@pytest.mark.parametrize(
    "model_uri",
    ["models:/ctr-model@champion", "ctr-model/12", "models:/ctr-model", "runs:/abc/model"],
)
def test_model_uri_must_be_an_immutable_version_reference(model_uri: str) -> None:
    # alias는 나중에 다른 버전을 가리킬 수 있어 승격 후보 식별자가 되지 못한다.
    with pytest.raises(ValueError, match="model_uri"):
        _request(candidate=_condition("candidate", CANDIDATE_SHA, model_uri=model_uri))


def test_result_payload_carries_full_lineage(tmp_path, stubbed) -> None:
    result = _evaluate(_request(), tmp_path)

    payload = result.model_dump(mode="json")
    assert payload["contract_version"] == "paired-offline-experiment-result-v1"
    assert payload["issue_number"] == 449
    assert payload["issue_branch"] == "exp/449-example"
    assert payload["experiment_id"] == "primary"
    assert payload["base_dev_sha"] == BASE_SHA
    assert payload["candidate_sha"] == CANDIDATE_SHA
    assert payload["baseline"]["registry_uri"] == _registry_uri("baseline", BASE_SHA)
    assert payload["candidate"]["code_archive_uri"].endswith(f"{CANDIDATE_SHA}.tar.gz")
    assert payload["registry_root"] == REGISTRY_ROOT
    assert payload["dataset_fingerprint"] == DATASET_FINGERPRINT
    assert payload["split_hash"] == SPLIT_HASH
    assert payload["feature_service"] == "ctr_training_v1"
    assert payload["policy_version"] == "promotion-policy-v1"
    assert len(payload["runs"]) == len(POLICY_SEEDS)
    assert payload["runs"][0]["comparison_id"] == f"comparison-{POLICY_SEEDS[0]}"
    assert payload["seeds"] == list(POLICY_SEEDS)


def test_result_runs_are_ordered_by_seed(tmp_path, stubbed) -> None:
    # 비교는 seed 오름차순으로 수행하므로 payload도 같은 순서로 고정한다.
    shuffled = (*POLICY_SEEDS[15:], *POLICY_SEEDS[:15])
    result = _evaluate(_request(runs=_runs(shuffled)), tmp_path)

    assert result.seeds == POLICY_SEEDS
    assert [run.seed for run in result.runs] == list(POLICY_SEEDS)


def test_failed_request_still_reports_lineage(tmp_path, stubbed) -> None:
    request = _request(runs=_runs(POLICY_SEEDS[:-1]))

    result = _evaluate(request, tmp_path)

    assert result.candidate.image_digest == DIGEST
    assert result.base_dev_sha == BASE_SHA
    assert result.runs[0].comparison_id is None


def test_write_result_publishes_atomically(tmp_path, stubbed) -> None:
    result = _evaluate(_request(), tmp_path)
    output = tmp_path / "nested" / "result.json"

    paired_experiment.write_result(result, output)

    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["outcome"] == "comparison_passed"
    assert not list(output.parent.glob(".*.tmp"))
