"""Auto Research 이슈 폼과 실험 브랜치 workflow 계약을 검증한다."""

from __future__ import annotations

from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ISSUE_FORM = REPOSITORY_ROOT / ".github" / "ISSUE_TEMPLATE" / "auto_research.yml"
BRANCH_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "auto-research-experiment-branch.yml"
)


def _issue_fields() -> dict[str, dict[str, object]]:
    form = yaml.safe_load(ISSUE_FORM.read_text(encoding="utf-8"))
    return {
        field["id"]: field
        for field in form["body"]
        if field.get("type") in {"input", "dropdown"}
    }


def test_auto_research_form_requires_machine_readable_promotion_criteria() -> None:
    fields = _issue_fields()

    assert "success_criteria" not in fields
    assert fields["experiment_id"]["validations"]["required"] is True
    assert fields["primary_metric_name"]["validations"]["required"] is True
    assert fields["primary_metric_direction"]["attributes"]["options"] == [
        "higher_is_better",
        "lower_is_better",
    ]
    assert fields["minimum_primary_delta"]["validations"]["required"] is True
    assert fields["guardrail_metric_name"]["attributes"]["placeholder"] == "없음"
    assert fields["maximum_guardrail_regression"]["attributes"]["placeholder"] == "없음"


def test_experiment_branch_workflow_creates_immutable_dev_based_ref() -> None:
    workflow = BRANCH_WORKFLOW.read_text(encoding="utf-8")

    assert "issues:" in workflow
    assert "opened" in workflow and "labeled" in workflow
    assert "contents: write" in workflow
    assert "issues: write" in workflow
    assert "auto-research" in workflow and "experiment" in workflow
    assert "ref: 'heads/dev'" in workflow
    assert "exp/${issue.number}-${experimentId}" in workflow
    assert "registryKey" in workflow
    assert "registry.db" in workflow
    assert "github.paginate(github.rest.issues.listComments" in workflow
    assert "autoresearch-branch:" in workflow
    assert "github.rest.git.createRef" in workflow
    assert "github.rest.git.updateRef" not in workflow
