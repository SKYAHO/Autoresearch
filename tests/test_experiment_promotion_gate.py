"""실험 metric 결과의 Draft PR 승격 게이트를 검증한다."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

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


def _workflow_accepts_registry_uri(
    registry_uri: str,
    *,
    issue_number: int = 449,
    experiment_id: str = "primary",
    candidate_sha: str = "b" * 40,
) -> bool:
    """워크플로 소스에서 뽑은 suffix 템플릿으로 게이트 판정을 재현한다.

    JS를 실행하지는 못하므로, 워크플로가 실제로 쓰는 두 템플릿 문자열을 파일에서
    읽어 같은 규칙(gs:// anchoring + suffix 두 개 중 하나)을 파이썬으로 적용한다.
    템플릿이 바뀌면 이 재현도 함께 바뀌므로, 경로 규칙 변경이 테스트에 드러난다.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    suffixes = re.findall(
        r"`(/experiments/\$\{issueNumber\}/\$\{experimentId\}[^`]*registry\.db)`",
        workflow,
    )
    assert suffixes, "워크플로에서 registry suffix 템플릿을 찾지 못했습니다"
    resolved = [
        suffix.replace("${issueNumber}", str(issue_number))
        .replace("${experimentId}", experiment_id)
        .replace("${candidateSha}", candidate_sha)
        for suffix in suffixes
    ]
    if not registry_uri.startswith("gs://"):
        return False
    return any(registry_uri.endswith(suffix) for suffix in resolved)


@pytest.mark.parametrize(
    ("registry_uri", "accepted"),
    [
        # 조건 격리 candidate 경로(#454)
        ("gs://registry/experiments/449/primary/candidate/" + "b" * 40 + "/registry.db", True),
        # 조건 구간이 없는 기존 경로(#450/#461)
        ("gs://registry/experiments/449/primary/" + "b" * 40 + "/registry.db", True),
        # baseline 조건 산출물은 승격 입력이 아니다
        ("gs://registry/experiments/449/primary/baseline/" + "b" * 40 + "/registry.db", False),
        # 다른 이슈·실험 좌표
        ("gs://registry/experiments/999/primary/candidate/" + "b" * 40 + "/registry.db", False),
        ("gs://registry/experiments/449/other/candidate/" + "b" * 40 + "/registry.db", False),
        # 다른 SHA
        ("gs://registry/experiments/449/primary/candidate/" + "c" * 40 + "/registry.db", False),
        # 스킴 위조
        ("https://evil.example/experiments/449/primary/candidate/" + "b" * 40 + "/registry.db", False),
    ],
)
def test_promotion_gate_registry_rule_accepts_only_candidate_coordinates(
    registry_uri: str, accepted: bool
) -> None:
    assert _workflow_accepts_registry_uri(registry_uri) is accepted


def test_promotion_workflow_keeps_registry_rule_structure() -> None:
    """suffix 두 개를 OR로 받아들이는 구조 자체를 고정한다.

    조건을 `&&`에서 `||`로 잘못 바꾸면 두 경로 모두 거부되거나 모두 통과한다.
    문자열 존재만 보는 검사로는 그 변경이 드러나지 않는다.
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert (
        "!registryUri.endsWith(isolatedRegistrySuffix) &&\n"
        "                !registryUri.endsWith(legacyRegistrySuffix)" in workflow
    )
    assert "if (!registryUri.startsWith('gs://'))" in workflow


def test_promotion_workflow_rejects_non_passed_paired_outcome() -> None:
    """paired 비교가 실패·기각인데 승격 PR이 만들어지지 않도록 고정한다."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "outcome: {required: false, type: string}" in workflow
    assert "OUTCOME: ${{ inputs.outcome || github.event.client_payload.outcome }}" in workflow
    assert "if (outcome && outcome !== 'comparison_passed')" in workflow
