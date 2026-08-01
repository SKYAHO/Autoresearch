"""실험 metric 결과의 Draft PR 승격 게이트를 검증한다."""

from __future__ import annotations

from pathlib import Path

from autoresearch.experiments.promotion_gate import evaluate, parse_criteria


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/auto-research-promotion.yml"


def _issue_body(**values: str) -> str:
    defaults = {
        "주 지표 이름": "val_roc_auc",
        "주 지표 방향": "higher_is_better",
        "baseline 대비 최소 개선폭": "0.002",
        "Guardrail 지표 이름 (선택)": "없음",
        "Guardrail 지표 방향": "not_applicable",
        "Guardrail 허용 최대 비열화 (선택)": "없음",
    }
    defaults.update(values)
    return "\n\n".join(f"### {key}\n\n{value}" for key, value in defaults.items())


def test_gate_passes_primary_metric_above_required_delta() -> None:
    criteria = parse_criteria(_issue_body())

    decision = evaluate(criteria, primary_candidate=0.781, primary_baseline=0.778)

    assert decision.passed is True
    assert decision.reason == "criteria_met"


def test_gate_rejects_guardrail_regression() -> None:
    criteria = parse_criteria(
        _issue_body(
            **{
                "Guardrail 지표 이름 (선택)": "log_loss",
                "Guardrail 지표 방향": "lower_is_better",
                "Guardrail 허용 최대 비열화 (선택)": "0.001",
            }
        )
    )

    decision = evaluate(
        criteria,
        primary_candidate=0.781,
        primary_baseline=0.778,
        guardrail_candidate=0.42,
        guardrail_baseline=0.41,
    )

    assert decision.passed is False
    assert decision.reason == "guardrail_regressed"


def test_gate_rejects_negative_primary_delta() -> None:
    try:
        parse_criteria(_issue_body(**{"baseline 대비 최소 개선폭": "-0.1"}))
    except ValueError as error:
        assert "minimum_primary_delta" in str(error)
    else:
        raise AssertionError("negative delta must be rejected")


def test_promotion_workflow_uses_dispatch_gate_and_draft_pr() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "repository_dispatch:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "issues: write" in workflow
    assert "pull-requests: write" in workflow
    assert "registry_uri" in workflow
    assert "run_id" in workflow
    assert "Validate experiment lineage" in workflow
    assert "lineage_invalid:" in workflow
    assert "steps.lineage.outputs.valid != 'true'" in workflow
    assert "refs/heads/promote/" in workflow
    assert "draft: true" in workflow
    assert "compareCommits" in workflow
    assert "Comment failed or rejected experiment result on source issue" in workflow
    assert "github.rest.issues.createComment" in workflow
    assert "github.paginate(github.rest.issues.listComments" in workflow
    assert "existingRef.object.sha !== candidateSha" in workflow
    assert "github.rest.git.updateRef" not in workflow
