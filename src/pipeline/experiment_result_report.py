"""paired 실험 판정 결과를 Experiment API 반영용 payload로 변환한다.

[파이프라인]
`compare-paired-experiment`가 게시한 `PairedExperimentResult`와 Experiment API 사이의
변환 경계다. 판정은 이미 끝난 뒤이므로 이 모듈은 판정하지 않는다.

[기능]
outcome→실험 상태 매핑, `metric_snapshot`·`reason`·포인터 로그 본문 조립, 128자 상한을
지키는 idempotency key 생성, 현재 상태에서 목표 터미널까지의 전이 경로 계획을 제공한다.

[비책임]
HTTP 전송(`agent_orchestration.ui.client`), 명령 배선과 종료 코드(`src.cli`), 판정
자체(`src.pipeline.experiment_evaluation`).
"""

from __future__ import annotations

from src.pipeline.paired_experiment import PairedExperimentResult

STATUS_CREATED = "CREATED"
STATUS_RUNNING = "RUNNING"
STATUS_EVALUATING = "EVALUATING"
STATUS_PASSED = "PASSED"
STATUS_FAILED = "FAILED"
STATUS_ERROR = "ERROR"
STATUS_PROMOTED = "PROMOTED"

TERMINAL_STATUSES = frozenset(
    {STATUS_PASSED, STATUS_FAILED, STATUS_ERROR, STATUS_PROMOTED}
)

# 서버 스키마 상한과 같은 값이다. 넘기면 Pydantic 검증에서 거부된다.
MAX_REASON_LENGTH = 8192
MAX_LOG_CONTENT_LENGTH = 8192
MAX_IDEMPOTENCY_KEY_LENGTH = 128

TRUNCATION_MARKER = "…(truncated)"

# comparison_failed는 HOLD 판정과 검증 실패를 겸한다(#454). 둘 다 "기각"이 아니라
# "판정되지 않았다"이므로 FAILED가 아닌 ERROR로 옮긴다.
_OUTCOME_TO_STATUS = {
    "comparison_passed": STATUS_PASSED,
    "comparison_rejected": STATUS_FAILED,
    "comparison_failed": STATUS_ERROR,
}


class ResultReportError(RuntimeError):
    """판정 결과를 Experiment API 상태로 옮길 수 없는 상태."""


def target_status(result: PairedExperimentResult) -> str:
    """판정 결과가 도달해야 할 터미널 상태를 반환한다."""
    try:
        return _OUTCOME_TO_STATUS[result.outcome]
    except KeyError as error:
        raise ResultReportError("알 수 없는 비교 outcome입니다.") from error


def build_metric_snapshot(result: PairedExperimentResult) -> dict[str, object]:
    """#454 결과 계약의 지표 필드를 이름 그대로 옮긴다."""
    return {
        "metric_name": result.metric_name,
        "primary_baseline": result.primary_baseline,
        "primary_candidate": result.primary_candidate,
        "paired_delta_mean": result.paired_delta_mean,
        "confidence_interval_lower": result.confidence_interval_lower,
        "confidence_interval_upper": result.confidence_interval_upper,
        "seeds": list(result.seeds),
        "outcome": result.outcome,
        "reason_codes": list(result.reason_codes),
        # datetime은 json.dumps가 직렬화하지 못한다.
        "evaluated_at": result.evaluated_at.isoformat(),
    }


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def build_reason(result: PairedExperimentResult) -> str:
    """판정 사유와 reason code를 상한 안에서 하나의 문자열로 만든다."""
    codes = ", ".join(result.reason_codes) if result.reason_codes else "none"
    return _truncate(
        f"{result.decision_reason} [reason_codes: {codes}]", MAX_REASON_LENGTH
    )
