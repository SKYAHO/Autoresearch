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
    MAX_IDEMPOTENCY_KEY_LENGTH,
    STATUS_CREATED,
    STATUS_ERROR,
    STATUS_EVALUATING,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_PROMOTED,
    STATUS_RUNNING,
    ResultReportError,
    build_log_content,
    build_log_idempotency_key,
    build_metric_snapshot,
    build_reason,
    plan_transitions,
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


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (STATUS_RUNNING, (STATUS_EVALUATING, STATUS_PASSED)),
        (STATUS_EVALUATING, (STATUS_PASSED,)),
        (STATUS_PASSED, ()),
    ],
)
def test_plan_transitions_resumes_from_current_status(
    current: str, expected: tuple[str, ...]
) -> None:
    """현재 상태부터 남은 전이만 밟는다 — PATCH가 멱등이 아니라 재호출이 event를 늘린다."""
    assert plan_transitions(current, STATUS_PASSED) == expected


def test_plan_transitions_refuses_created_to_avoid_launcher_race() -> None:
    """CREATED는 launcher가 선점할 대기 행이므로 직접 전이하지 않는다.

    #547 병합 전에는 자가 claim이 무해했으나, 이제 `CREATED` + `executor_job_name
    IS NULL`은 `CREATED_CLAIM_STATEMENT`가 집어갈 행이다. 여기서 RUNNING으로 올리면
    launcher와 경합하고, 올라간 행은 두 claim 쿼리 어디에도 걸리지 않는 고아가 된다.
    """
    with pytest.raises(ResultReportError):
        plan_transitions(STATUS_CREATED, STATUS_PASSED)


@pytest.mark.parametrize("current", [STATUS_FAILED, STATUS_ERROR, STATUS_PROMOTED])
def test_plan_transitions_refuses_to_overwrite_other_terminal(current: str) -> None:
    """이미 결론이 난 실험을 다른 결론으로 덮어쓰지 않는다.

    전이를 하나도 밟기 전에 거부하므로, 호출자 쪽 `reached`는 None으로 남는다 —
    ERROR 강등이 일어나지 않아야 한다는 뜻이다(그 경로는 CLI 테스트가 본다).
    """
    with pytest.raises(ResultReportError):
        plan_transitions(current, STATUS_PASSED)


def test_naive_idempotency_key_would_exceed_limit() -> None:
    """접두사를 그대로 이어붙이면 137자가 되어 서버가 거부한다.

    이 테스트는 구현이 아니라 **문제 자체**를 고정한다. 누군가 트리밍을 되돌리면
    아래 상한 테스트가 깨지고, 왜 트리밍이 필요했는지는 이 숫자가 설명한다.
    """
    naive = f"{'0' * 36}:paired-result:{'experiment-evaluation-' + 'a' * 64}"

    assert len(naive) == 137
    assert len(naive) > MAX_IDEMPOTENCY_KEY_LENGTH


@pytest.mark.parametrize(
    ("update", "expected_length"),
    [
        ({"evaluation_id": "experiment-evaluation-" + "a" * 64}, 115),
        (
            {"evaluation_id": None, "evidence_id": "paired-seed-evidence-" + "b" * 64},
            115,
        ),
        ({"evaluation_id": None, "evidence_id": None}, 91),
    ],
    ids=["evaluation_id", "evidence_id", "candidate_sha_fallback"],
)
def test_log_idempotency_key_stays_within_limit(
    update: dict[str, object], expected_length: int
) -> None:
    """상한 근처 실제 길이로 친다 — 짧은 예시로 통과시키면 의미가 없다."""
    result = _result("comparison_passed").model_copy(update=update)

    key = build_log_idempotency_key("0" * 36, result)

    assert len(key) == expected_length
    assert len(key) <= MAX_IDEMPOTENCY_KEY_LENGTH
    assert "experiment-evaluation" not in key
    assert "paired-seed-evidence" not in key


def test_log_content_is_a_pointer_within_limit() -> None:
    """원본 JSON이 아니라 GCS 위치를 가리키는 요약만 남긴다."""
    content = build_log_content(
        _result("comparison_passed"), log_uri="gs://bucket/run.log"
    )

    assert len(content) <= 8192
    assert "gs://bucket/run.log" in content
    assert "outcome=comparison_passed" in content
