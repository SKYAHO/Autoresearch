"""paired 실험 요청·결과 계약의 테스트 픽스처를 제공한다.

여러 테스트 모듈이 `paired-offline-experiment-v1` 요청과 그 결과 double을 필요로 한다.
테스트 모듈끼리 서로 import하면 무거운 부수효과(`sys.path` 조작, 최상위 import)까지
딸려오고 비공개 헬퍼 이름에 의존하게 되므로, 공용 픽스처는 여기에 둔다.

**테스트 모듈이 아니다** — 여기에 `test_` 함수를 추가하지 않는다.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.pipeline.paired_experiment import PairedExperimentResult
from src.pipeline.promotion_evidence import (
    ExperimentPlanReceipt,
    GcsObjectReceipt,
    create_experiment_plan,
)


def paired_result(request, *, outcome: str) -> PairedExperimentResult:
    """CLI 배선만 보는 테스트용 결과 double(판정 자체는 paired_experiment 테스트가 본다)."""
    return PairedExperimentResult(
        outcome=outcome,
        decision_reason=(
            "criteria_met" if outcome == "comparison_passed" else "missing_paired_run"
        ),
        reason_codes=() if outcome == "comparison_passed" else ("missing_paired_run",),
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
        registry_root=request.registry_root,
        plan_id=request.plan_receipt.plan.plan_id,
        seeds=tuple(run.seed for run in request.runs),
        runs=(),
        model_uri=(
            request.candidate.model_uri if outcome == "comparison_passed" else None
        ),
        evaluated_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
    )


def paired_request_payload(seeds: tuple[int, ...]) -> dict[str, object]:
    """compare-paired-experiment CLI가 읽을 최소 유효 요청을 만든다."""
    plan = create_experiment_plan(
        hypothesis_id="issue-449",
        control_id="a" * 40,
        candidate_ids=("b" * 40,),
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    plan_receipt = ExperimentPlanReceipt(
        plan=plan,
        object=GcsObjectReceipt(
            uri=f"gs://evidence/promotion-evidence/plans/{plan.plan_id}.json",
            generation="1",
            metageneration="1",
            time_created=datetime(2026, 8, 1, tzinfo=timezone.utc),
            sha256="a" * 64,
        ),
    )

    def _condition(condition: str, source_sha: str) -> dict[str, object]:
        return {
            "source_sha": source_sha,
            "image_digest": "sha256:" + "c" * 64,
            "code_archive_sha": source_sha,
            "code_archive_uri": f"gs://code/code/{source_sha}.tar.gz",
            "registry_uri": (
                f"gs://registry/experiments/449/primary/{condition}/{source_sha}/registry.db"
            ),
            "feature_schema_fingerprint": "d" * 64,
        }

    candidate = _condition("candidate", "b" * 40)
    candidate["model_uri"] = "models:/ctr-model/12"
    return {
        "contract_version": "paired-offline-experiment-v1",
        "issue_number": 449,
        "issue_branch": "exp/449-example",
        "experiment_id": "primary",
        "base_dev_sha": "a" * 40,
        "candidate_sha": "b" * 40,
        "feature_service": "ctr_training_v1",
        "extra_features": [],
        "registry_root": "gs://registry",
        "dataset_snapshot_uri": "gs://artifacts/snapshots/manifest.json",
        "dataset_fingerprint": "1" * 64,
        "split_hash": "2" * 64,
        "training_config_fingerprint": "3" * 64,
        "plan_receipt": plan_receipt.model_dump(mode="json"),
        "baseline": _condition("baseline", "a" * 40),
        "candidate": candidate,
        "runs": [
            {
                "seed": seed,
                "run_id": f"seed-{seed}",
                "baseline_mlflow_run_id": f"baseline-{seed}",
                "candidate_mlflow_run_id": f"candidate-{seed}",
                "artifact_uri": f"gs://artifacts/449/primary/candidate/{'b' * 40}/seed-{seed}/",
                "log_uri": f"gs://artifacts/449/primary/candidate/{'b' * 40}/seed-{seed}/log.txt",
            }
            for seed in seeds
        ],
    }
