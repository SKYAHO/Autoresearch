"""paired 판정 결과를 Experiment API payload로 바꾸는 계약을 검증한다.

전체 파이프라인 중 `compare-paired-experiment`의 결과와 Experiment API 사이의 변환
구간만 본다. HTTP 전송(`agent_orchestration.ui.client`)과 명령 배선(`src.cli`)은
담당하지 않는다.
"""

from __future__ import annotations

import json

import pytest

from src.pipeline.paired_experiment import PairedExperimentRequest
from src.pipeline.experiment_result_report import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_PASSED,
    build_metric_snapshot,
    build_reason,
    target_status,
)
from tests.test_cli import _paired_request_payload, _paired_result


def _result(outcome: str):
    request = PairedExperimentRequest.model_validate(_paired_request_payload((42, 43, 44)))
    return _paired_result(request, outcome=outcome)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("comparison_passed", STATUS_PASSED),
        ("comparison_rejected", STATUS_FAILED),
        ("comparison_failed", STATUS_ERROR),
    ],
)
def test_outcome_maps_to_experiment_status(outcome: str, expected: str) -> None:
    """comparison_failed는 판정 불가이므로 FAILED가 아니라 ERROR로 옮긴다."""
    assert target_status(_result(outcome)) == expected


@pytest.mark.parametrize(
    ("origin", "reason_codes"),
    [
        # HOLD 판정 — EvaluationReasonCode 출신
        ("hold", ("primary_roc_auc_inconclusive",)),
        # 판정 엔진을 부르지도 못한 검증 실패 — PairedExperimentReason 출신
        ("validation_failure", ("missing_paired_run",)),
    ],
)
def test_comparison_failed_maps_to_error_regardless_of_origin(
    origin: str, reason_codes: tuple[str, ...]
) -> None:
    """comparison_failed의 두 원인 모두 ERROR로 간다.

    두 원인은 reason_code 문자열만으로 기계적으로 갈라낼 수 없다(`seed_policy_mismatch`가
    두 Enum 모두에 존재). 리포터가 다시 갈라내려는 변경이 들어오면 이 테스트가 깨진다.
    """
    result = _result("comparison_failed").model_copy(update={"reason_codes": reason_codes})

    assert target_status(result) == STATUS_ERROR
    # 구분 정보는 잃지 않고 스냅샷에 원본 그대로 실린다.
    assert build_metric_snapshot(result)["reason_codes"] == list(reason_codes)


def test_metric_snapshot_uses_contract_field_names() -> None:
    """#454 계약의 필드명을 그대로 옮긴다 — 이름을 새로 지으면 계약 변경이 조용히 통과한다."""
    snapshot = build_metric_snapshot(_result("comparison_passed"))

    assert set(snapshot) == {
        "metric_name",
        "primary_baseline",
        "primary_candidate",
        "paired_delta_mean",
        "confidence_interval_lower",
        "confidence_interval_upper",
        "seeds",
        "outcome",
        "reason_codes",
        "evaluated_at",
    }
    assert snapshot["seeds"] == [42, 43, 44]
    assert isinstance(snapshot["evaluated_at"], str)


def test_metric_snapshot_is_json_serializable() -> None:
    """client가 payload를 `json.dumps`로 보내므로(client.py:236) raw datetime이면 죽는다.

    `evaluated_at`은 `PairedExperimentResult`에서 `datetime`이고 표준 encoder는 이를
    직렬화하지 못한다. isoformat 변환이 빠지면 `PATCH /status`가 매번 TypeError로
    실패하므로, 실패 모드 자체를 여기서 친다.
    """
    snapshot = build_metric_snapshot(_result("comparison_passed"))

    encoded = json.dumps(snapshot)

    assert json.loads(encoded)["evaluated_at"] == "2026-08-03T00:00:00+00:00"


def test_reason_is_truncated_with_marker() -> None:
    """8192자 상한을 넘으면 잘리되, 잘렸다는 사실을 문자열에 남긴다."""
    result = _result("comparison_rejected").model_copy(
        update={"decision_reason": "x" * 9000}
    )

    reason = build_reason(result)

    assert len(reason) == 8192
    assert reason.endswith("…(truncated)")
