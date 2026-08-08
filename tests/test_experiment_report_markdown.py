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


def test_renders_primary_metric_with_paired_delta_and_confidence_interval() -> None:
    """신뢰구간이 없으면 delta 부호만 보고 개선이라고 오독한다."""
    result = _result("comparison_passed").model_copy(
        update={
            "metric_name": "roc_auc",
            "primary_baseline": 0.6295,
            "primary_candidate": 0.6872,
            "paired_delta_mean": 0.0577,
            "confidence_interval_lower": 0.0412,
            "confidence_interval_upper": 0.0742,
        }
    )

    document = render_experiment_report(result)

    assert "roc_auc" in document
    assert "0.6295" in document
    assert "0.6872" in document
    assert "+0.0577" in document
    assert "[0.0412, 0.0742]" in document
    assert "판정 지표 없음" not in document


def test_renders_seed_count_and_split_hash() -> None:
    """seed 수가 안 보이면 FAILED를 '개선 없음'과 '검정력 없음'으로 구분할 수 없다."""
    document = render_experiment_report(_result("comparison_failed"))

    assert f"- seed: {len(POLICY_SEEDS)}개" in document
    assert ", ".join(str(seed) for seed in POLICY_SEEDS) in document
    assert "- split hash: `" + "2" * 64 + "`" in document


def test_renders_data_and_code_provenance() -> None:
    """결론을 나중에 감사·재현할 유일한 경로다 — 로컬 실증이 무효가 된 직접 원인이었다."""
    document = render_experiment_report(_result("comparison_passed"))

    assert "gs://artifacts/snapshots/manifest.json" in document
    assert "1" * 64 in document
    assert "3" * 64 in document
    assert "a" * 40 in document
    assert "b" * 40 in document


def test_renders_hypothesis_when_the_caller_supplies_it() -> None:
    """가설이 없으면 '+0.002'가 성공인지 실패인지 판단할 기준이 없다."""
    document = render_experiment_report(
        _result("comparison_passed"),
        hypothesis="num_leaves를 64로 올리면 ROC-AUC가 오른다",
    )

    assert "num_leaves를 64로 올리면 ROC-AUC가 오른다" in document


def test_renders_document_when_the_hypothesis_argument_is_absent() -> None:
    """가설 조달은 호출부 책임이라 없이 호출될 수 있다 — 지표 부재 케이스와 대칭이다."""
    document = render_experiment_report(_result("comparison_failed"))

    assert "가설 없음" in document
    assert "comparison_failed" in document


def test_renders_an_allowlist_and_never_dumps_the_whole_result() -> None:
    """전량 덤프하면 계약에 나중에 추가되는 필드가 검토 없이 리포트로 새 나간다.

    입력에는 서명 URL·내부 좌표가 섞일 수 있으므로, 렌더러는 고른 필드만 낸다.
    `code_archive_uri`는 의도적으로 렌더링하지 않는 필드이며 그 부재로 이 성질을 고정한다.
    """
    document = render_experiment_report(_result("comparison_passed"))

    assert "gs://code/code/" not in document
    assert "code_archive_uri" not in document
