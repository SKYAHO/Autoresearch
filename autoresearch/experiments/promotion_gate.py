"""Auto Research 실험 결과를 main 승격 후보로 판정한다.

[파이프라인] 가설 이슈와 dev 실험 결과 사이에서 구조화 metric 기준을 판정한다.
GitHub Actions가 Draft main PR을 만들지만, 모델 alias 변경·prod 배포·Airflow 실행은
이 모듈의 책임이 아니다.

[기능] Issue Form 본문에서 기계 판독 가능한 주 지표·guardrail 기준을 읽고,
baseline/candidate 값을 비교해 승격 후보 통과 여부와 안정된 사유 코드를 반환한다.

[비책임] GitHub API 호출, 브랜치 생성, PR 생성과 GCP/MLflow 접근은 호출자 workflow
또는 외부 오케스트레이터가 소유한다.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


_LABELS = {
    "primary_name": "주 지표 이름",
    "primary_direction": "주 지표 방향",
    "minimum_delta": "baseline 대비 최소 개선폭",
    "guardrail_name": "Guardrail 지표 이름 (선택)",
    "guardrail_direction": "Guardrail 지표 방향",
    "maximum_regression": "Guardrail 허용 최대 비열화 (선택)",
}
_DIRECTIONS = {"higher_is_better", "lower_is_better"}


@dataclass(frozen=True)
class PromotionCriteria:
    primary_name: str
    primary_direction: str
    minimum_primary_delta: float
    guardrail_name: str | None
    guardrail_direction: str | None
    maximum_guardrail_regression: float | None


@dataclass(frozen=True)
class GateDecision:
    passed: bool
    reason: str


def _field(body: str, label: str) -> str:
    match = re.search(rf"^### {re.escape(label)}\s*\n+(.+?)(?=\n### |\Z)", body, re.M | re.S)
    if match is None:
        raise ValueError(f"missing Issue Form field: {label}")
    return match.group(1).strip()


def _non_negative(value: str, name: str) -> float:
    try:
        parsed = float(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a decimal") from error
    if parsed < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def parse_criteria(issue_body: str) -> PromotionCriteria:
    """Issue Form 본문에서 승격 판정 기준을 검증해 반환한다."""
    primary_direction = _field(issue_body, _LABELS["primary_direction"])
    if primary_direction not in _DIRECTIONS:
        raise ValueError("primary_metric_direction is invalid")
    guardrail_name = _field(issue_body, _LABELS["guardrail_name"])
    guardrail_direction = _field(issue_body, _LABELS["guardrail_direction"])
    maximum_regression = _field(issue_body, _LABELS["maximum_regression"])
    if guardrail_name == "없음":
        if guardrail_direction != "not_applicable" or maximum_regression != "없음":
            raise ValueError("guardrail fields must all be not_applicable or 없음")
        resolved_guardrail_name = None
        resolved_guardrail_direction = None
        resolved_maximum = None
    else:
        if guardrail_direction not in _DIRECTIONS or maximum_regression == "없음":
            raise ValueError("guardrail metric requires direction and maximum regression")
        resolved_guardrail_name = guardrail_name
        resolved_guardrail_direction = guardrail_direction
        resolved_maximum = _non_negative(maximum_regression, "maximum_guardrail_regression")
    return PromotionCriteria(
        primary_name=_field(issue_body, _LABELS["primary_name"]),
        primary_direction=primary_direction,
        minimum_primary_delta=_non_negative(
            _field(issue_body, _LABELS["minimum_delta"]), "minimum_primary_delta"
        ),
        guardrail_name=resolved_guardrail_name,
        guardrail_direction=resolved_guardrail_direction,
        maximum_guardrail_regression=resolved_maximum,
    )


def evaluate(
    criteria: PromotionCriteria,
    *,
    primary_candidate: float,
    primary_baseline: float,
    guardrail_candidate: float | None = None,
    guardrail_baseline: float | None = None,
) -> GateDecision:
    """기준을 만족하면 ``criteria_met``, 아니면 안정된 거부 사유를 반환한다."""
    primary_delta = (
        primary_candidate - primary_baseline
        if criteria.primary_direction == "higher_is_better"
        else primary_baseline - primary_candidate
    )
    if primary_delta < criteria.minimum_primary_delta:
        return GateDecision(False, "primary_metric_below_delta")
    if criteria.guardrail_name is None:
        return GateDecision(True, "criteria_met")
    if guardrail_candidate is None or guardrail_baseline is None:
        return GateDecision(False, "guardrail_metric_missing")
    regression = (
        guardrail_baseline - guardrail_candidate
        if criteria.guardrail_direction == "higher_is_better"
        else guardrail_candidate - guardrail_baseline
    )
    if regression > criteria.maximum_guardrail_regression:
        return GateDecision(False, "guardrail_regressed")
    return GateDecision(True, "criteria_met")
