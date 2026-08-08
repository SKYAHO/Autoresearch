"""paired 판정 결과 Markdown 렌더러의 동작을 고정한다(#620)."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.pipeline.experiment_evaluation import POLICY_SEEDS  # noqa: E402
from src.pipeline.experiment_report_markdown import (  # noqa: E402
    render_experiment_report,
)
from src.pipeline.paired_experiment import (  # noqa: E402
    PairedExperimentRequest,
    PairedExperimentResult,
)
from tests.paired_experiment_fixtures import (  # noqa: E402
    paired_request_payload,
    paired_result,
)


def _result(outcome: str) -> PairedExperimentResult:
    """CLI 계약을 그대로 따르는 결과 double을 만든다."""
    request = PairedExperimentRequest.model_validate(
        paired_request_payload(POLICY_SEEDS)
    )
    return paired_result(request, outcome=outcome)


def test_renders_verdict_with_outcome_and_decision_reason() -> None:
    document = render_experiment_report(_result("comparison_passed"))

    assert "comparison_passed" in document
    assert "criteria_met" in document


def test_marks_items_absent_from_the_contract_as_unmeasured_with_owning_stage() -> None:
    """계약에 없는 항목을 조용히 생략하면 승격 판단자가 '없음'과 '0'을 구분하지 못한다."""
    document = render_experiment_report(_result("comparison_passed"))

    assert "- 탐색 공간: 미측정 — Stage 3" in document
    assert "- 우승 조합: 미측정 — Stage 3" in document
    assert "- 기여 분해: 미측정 — Stage 4" in document
    assert "- 보조 지표: 미측정 — Stage 4" in document


def test_renders_metrics_section_as_unavailable_when_the_verdict_carries_none() -> None:
    """comparison_failed는 지표가 전부 None일 수 있다 — 빈 값이 아니라 사유로 보여야 한다."""
    document = render_experiment_report(_result("comparison_failed"))

    assert "## 지표" in document
    assert "판정 지표 없음" in document
