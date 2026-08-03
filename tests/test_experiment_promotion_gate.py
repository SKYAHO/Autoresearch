"""실험 metric 결과의 Draft PR 승격 게이트를 검증한다."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from autoresearch.experiments.promotion_gate import _LABELS, evaluate, parse_criteria


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = PROJECT_ROOT / ".github/workflows/auto-research-promotion.yml"
DEV_PROMOTION_WORKFLOW = PROJECT_ROOT / ".github/workflows/auto-research-dev-promotion.yml"
ISSUE_FORM = PROJECT_ROOT / ".github/ISSUE_TEMPLATE/auto_research.yml"
RENDERED_FORM_FIXTURE = PROJECT_ROOT / "tests/fixtures/auto_research_issue_form_rendered.md"


def _issue_body(**values: str) -> str:
    """Issue Form의 실제 label로 본문을 합성한다.

    기본값은 반드시 `.github/ISSUE_TEMPLATE/auto_research.yml`의 label과 일치해야 한다.
    합성 본문이 Form과 어긋나면 `parse_criteria`의 결함을 테스트가 덮어버린다(#495).
    """
    defaults = {
        "주 지표 이름": "val_roc_auc",
        "주 지표 방향": "higher_is_better",
        "최소 주 지표 개선폭": "0.002",
        "Guardrail 지표 이름": "없음",
        "Guardrail 지표 방향": "not_applicable",
        "최대 Guardrail 악화폭": "없음",
    }
    defaults.update(values)
    return "\n\n".join(f"### {key}\n\n{value}" for key, value in defaults.items())


def _issue_form_labels() -> set[str]:
    """Issue Form이 실제로 렌더하는 heading label 집합을 반환한다."""
    parsed = yaml.safe_load(ISSUE_FORM.read_text(encoding="utf-8"))
    return {
        item["attributes"]["label"]
        for item in parsed["body"]
        if item["type"] != "markdown"
    }


def test_parse_criteria_reads_body_rendered_from_actual_form() -> None:
    """정본 fixture를 그대로 파싱한다 — 합성 본문 헬퍼에 의존하지 않는다.

    이 테스트가 있었다면 #461 머지 시점에 즉시 실패했을 것이다. Issue Form과
    `_LABELS` 중 한쪽만 바뀌거나 한쪽만 머지되면 여기서 깨진다.
    """
    criteria = parse_criteria(RENDERED_FORM_FIXTURE.read_text(encoding="utf-8"))

    assert criteria.primary_name == "roc_auc"
    assert criteria.primary_direction == "higher_is_better"
    assert criteria.minimum_primary_delta == pytest.approx(0.002)
    assert criteria.guardrail_name is None
    assert criteria.guardrail_direction is None
    assert criteria.maximum_guardrail_regression is None


def test_promotion_gate_labels_exist_in_issue_form() -> None:
    """`_LABELS`의 모든 값이 Issue Form에 실재하는지 고정한다."""
    missing = sorted(set(_LABELS.values()) - _issue_form_labels())

    assert not missing, f"Issue Form에 없는 label: {missing}"


def test_issue_body_helper_uses_real_form_labels() -> None:
    """합성 본문 헬퍼가 Form에 없는 label을 쓰지 않도록 고정한다."""
    synthesized = {
        line.removeprefix("### ")
        for line in _issue_body().splitlines()
        if line.startswith("### ")
    }
    missing = sorted(synthesized - _issue_form_labels())

    assert not missing, f"헬퍼가 Form에 없는 label을 사용: {missing}"


def test_gate_passes_primary_metric_above_required_delta() -> None:
    criteria = parse_criteria(_issue_body())

    decision = evaluate(criteria, primary_candidate=0.781, primary_baseline=0.778)

    assert decision.passed is True
    assert decision.reason == "criteria_met"


def test_gate_rejects_guardrail_regression() -> None:
    criteria = parse_criteria(
        _issue_body(
            **{
                "Guardrail 지표 이름": "log_loss",
                "Guardrail 지표 방향": "lower_is_better",
                "최대 Guardrail 악화폭": "0.001",
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
        parse_criteria(_issue_body(**{"최소 주 지표 개선폭": "-0.1"}))
    except ValueError as error:
        assert "minimum_primary_delta" in str(error)
    else:
        raise AssertionError("negative delta must be rejected")


def test_workflows_do_not_join_with_escaped_newline() -> None:
    """`].join('\\\\n')`는 리터럴 백슬래시+n을 본문에 남긴다(#495 버그 A).

    `github-script`의 `script: |`는 리터럴 블록 스칼라라 YAML이 이스케이프를 처리하지
    않는다. 따라서 JS가 받는 값이 `'\\\\n'`(백슬래시+n)이 되어 줄바꿈이 사라진다.
    """
    offenders = []
    for workflow in sorted((PROJECT_ROOT / ".github/workflows").glob("*.yml")):
        text = workflow.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if r".join('\\n')" in line or r'.join("\\n")' in line:
                offenders.append(f"{workflow.name}:{number}")

    assert not offenders, f"이스케이프된 개행으로 본문을 조립하는 위치: {offenders}"


def _experiment_id_patterns() -> dict[str, str]:
    """`experiment_id` 정규식이 정의된 모든 지점을 파일에서 추출한다."""
    sources = {
        "promotion_workflow": (
            WORKFLOW,
            r"/(\^\[a-z0-9\]\[[^/]*?\$)/\.test\(experimentId\)|"
            r"requireMatch\(experimentId, /(\^[^/]*?\$)/",
        ),
        "dev_promotion_workflow": (
            DEV_PROMOTION_WORKFLOW,
            r"/(\^\[a-z0-9\]\[[^/]*?\$)/\.test\(rawExperimentId\)|"
            r"requireMatch\(experimentId, /(\^[^/]*?\$)/",
        ),
        "tools_selector": (
            PROJECT_ROOT / "tools/auto_research_issue_branch.py",
            r"_EXPERIMENT_ID_PATTERN = re\.compile\(r\"(\^[^\"]+\$)\"\)",
        ),
        "paired_experiment": (
            PROJECT_ROOT / "src/pipeline/paired_experiment.py",
            r"_EXPERIMENT_ID_PATTERN = r\"(\^[^\"]+\$)\"",
        ),
        "experiment_context": (
            PROJECT_ROOT / "autoresearch/experiments/context.py",
            r"_EXPERIMENT_ID = re\.compile\(r\"(\^[^\"]+\$)\"\)",
        ),
    }
    found: dict[str, str] = {}
    for name, (path, pattern) in sources.items():
        matches = [
            group
            for match in re.finditer(pattern, path.read_text(encoding="utf-8"))
            for group in match.groups()
            if group
        ]
        assert matches, f"{name}에서 experiment_id 정규식을 찾지 못했습니다"
        assert len(set(matches)) == 1, f"{name} 안에서 정규식이 갈라져 있습니다: {set(matches)}"
        found[name] = matches[0]
    return found


def test_experiment_id_pattern_is_identical_everywhere() -> None:
    """정규식이 5개 정의 지점에서 동일해야 한다(#495 버그 B)."""
    patterns = _experiment_id_patterns()

    assert len(set(patterns.values())) == 1, f"정규식이 갈라져 있습니다: {patterns}"


@pytest.mark.parametrize(
    "experiment_id",
    [
        "paired-offline-comparison-2026-08",  # 33자 — 좁은 쪽이 거부
        "feature_dropout",  # 밑줄
        "exp.v2",  # 점
        "ar:449",  # 콜론 — git ref 이름 불허 문자
    ],
)
def test_divergent_experiment_ids_are_judged_identically(experiment_id: str) -> None:
    """한쪽만 통과하던 값들이 모든 지점에서 같은 판정을 받아야 한다."""
    verdicts = {
        name: bool(re.fullmatch(pattern.strip("^$"), experiment_id))
        for name, pattern in _experiment_id_patterns().items()
    }

    assert len(set(verdicts.values())) == 1, f"{experiment_id} 판정 불일치: {verdicts}"


def test_promotion_workflow_uses_dispatch_gate_and_draft_pr() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "repository_dispatch:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "issues: write" in workflow
    assert "pull-requests: write" in workflow
    assert "registry_uri" in workflow
    assert "run_id" in workflow
    assert "Validate experiment lineage" in workflow
    # 사유 코드는 `${kind}:${error.message}`로 조립된다(#495 D-3). kind 기본값이
    # lineage_invalid이며, 나머지 두 갈래는 전용 테스트가 고정한다.
    assert "error.kind || 'lineage_invalid'" in workflow
    assert "steps.lineage.outputs.valid != 'true'" in workflow
    assert "refs/heads/promote/" in workflow
    assert "draft: true" in workflow
    assert "compareCommits" in workflow
    assert "Comment failed or rejected experiment result on source issue" in workflow
    assert "github.rest.issues.createComment" in workflow
    assert "github.paginate(github.rest.issues.listComments" in workflow
    assert "existingRef.object.sha !== candidateSha" in workflow
    assert "github.rest.git.updateRef" not in workflow


def test_promotion_workflow_records_result_when_gate_step_fails() -> None:
    """gate step이 예외로 죽어도 소스 이슈에 기록이 남아야 한다(#495 C·D-1)."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "steps.gate.outcome == 'failure'" in workflow
    assert "gate_step_failed:" in workflow


def test_promotion_workflow_validates_metric_inputs_before_gate() -> None:
    """지표를 lineage에서 검증해 gate의 float() 폭발을 막는다(#495 D-1)."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "isFiniteDecimal" in workflow
    assert "primary_candidate must be a finite decimal" not in workflow  # 템플릿 리터럴로 조립
    assert "must be a finite decimal" in workflow


def test_promotion_workflow_separates_rejection_kinds() -> None:
    """입력 거부·계보 불일치·비교 기각을 다른 사유 코드로 구분한다(#495 D-3)."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "input_invalid" in workflow
    assert "comparison_rejected" in workflow
    assert "lineage_invalid" in workflow
    assert "RejectionError" in workflow


def test_promotion_workflow_failure_comment_has_fallbacks() -> None:
    """실패 코멘트의 모든 항목이 빈 backtick으로 렌더되지 않는다(#495 D-2)."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "PRIMARY_CANDIDATE || '미제공'" in workflow
    assert "PRIMARY_BASELINE || '미제공'" in workflow


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
