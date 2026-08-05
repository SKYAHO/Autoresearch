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


# 값은 `.github/ISSUE_TEMPLATE/auto_research.yml`의 `label:`과 문자 그대로 같아야 한다.
# 한쪽만 바뀌면 `parse_criteria`가 실제 이슈 본문에서 항상 실패한다(#495). 이 정합성은
# `tests/test_experiment_promotion_gate.py`의 정본 fixture 파싱 테스트가 고정한다.
_LABELS = {
    "primary_name": "주 지표 이름",
    "primary_direction": "주 지표 방향",
    "minimum_delta": "최소 주 지표 개선폭",
    "guardrail_name": "Guardrail 지표 이름",
    "guardrail_direction": "Guardrail 지표 방향",
    "maximum_regression": "최대 Guardrail 악화폭",
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


def _guardrail_failure(
    criteria: PromotionCriteria,
    guardrail_candidate: float | None,
    guardrail_baseline: float | None,
) -> GateDecision | None:
    """guardrail 위반이면 거부 판정을, 문제없으면 ``None``을 돌려준다.

    지표 통과 경로와 하드 리밋 경로가 **같은 guardrail 검사**를 쓰게 하려고 뽑았다
    (#472 §4.4). 두 곳에 복제하면 한쪽만 고쳐져 하드 리밋으로 guardrail을 우회하는
    경로가 조용히 생긴다.
    """
    if criteria.guardrail_name is None:
        return None
    if guardrail_candidate is None or guardrail_baseline is None:
        return GateDecision(False, "guardrail_metric_missing")
    regression = (
        guardrail_baseline - guardrail_candidate
        if criteria.guardrail_direction == "higher_is_better"
        else guardrail_candidate - guardrail_baseline
    )
    if regression > criteria.maximum_guardrail_regression:
        return GateDecision(False, "guardrail_regressed")
    return None


def evaluate(
    criteria: PromotionCriteria,
    *,
    primary_candidate: float,
    primary_baseline: float,
    guardrail_candidate: float | None = None,
    guardrail_baseline: float | None = None,
    hard_retrain_limit_days: int | None = None,
    days_since_last_promotion: int | None = None,
) -> GateDecision:
    """기준을 만족하면 ``criteria_met``, 아니면 안정된 거부 사유를 반환한다.

    ``hard_retrain_limit_days``/``days_since_last_promotion``은 "성능과 무관하게 일정
    기간이 지나면 교체한다"는 정책을 배선하기 위한 입력이다(#472 §4). **둘 다 기본값이
    ``None``이며, 하나라도 없으면 하드 리밋 조건을 평가하지 않는다** — 관측되지 않은
    것을 "기한이 지났다"로도 "안 지났다"로도 바꾸지 않는다(`#485` spec §4.1과 같은 결).

    이 모듈은 값이 **어떻게 구해졌는지 모른다.** 측정 실행·값 산출·`temporal_hold`
    확인은 전부 호출부 책임이다(#472 §2·§3.2) — 이 모듈은 `degradation_eval`을
    import하지 않는다(패키지 경계 + 그 모듈이 끌고 오는 lightgbm).

    Args:
        hard_retrain_limit_days: 마지막 승격 이후 이 일수가 지나면 강제 교체 대상.
            호출부가 `temporal_hold`를 먼저 확인해, hold면 ``None``을 넘긴다 —
            근거 없는 곡선에서 나온 값으로 승격을 **늘리지 않는다**.
        days_since_last_promotion: 마지막 승격 이후 경과일.
    """
    primary_delta = (
        primary_candidate - primary_baseline
        if criteria.primary_direction == "higher_is_better"
        else primary_baseline - primary_candidate
    )
    if primary_delta < criteria.minimum_primary_delta:
        limit_reached = (
            hard_retrain_limit_days is not None
            and days_since_last_promotion is not None
            and days_since_last_promotion >= hard_retrain_limit_days
        )
        if not limit_reached:
            # 기존 동작 그대로 — 하드 리밋이 성립하지 않으면 guardrail을 보지 않는다.
            # 여기서 guardrail을 마저 보면 값이 없는 기존 실행의 사유가
            # `guardrail_metric_missing`으로 바뀌어 승격 이력의 의미가 달라진다.
            return GateDecision(False, "primary_metric_below_delta")
        # 하드 리밋이 성립해도 **guardrail은 우회하지 않는다**(§4.4). 취지는 "성능이
        # 정체돼도 교체한다"이지 "망가진 모델도 올린다"가 아니다.
        failure = _guardrail_failure(criteria, guardrail_candidate, guardrail_baseline)
        if failure is not None:
            return failure
        return GateDecision(True, "hard_retrain_limit_reached")
    failure = _guardrail_failure(criteria, guardrail_candidate, guardrail_baseline)
    if failure is not None:
        return failure
    # 지표로 통과했으면 기한 도달 여부와 무관하게 `criteria_met`이다(§4.1). 기한 때문에
    # 통과한 것으로 기록하면 나중에 승격 이력을 읽을 때 모델 품질을 과소평가한다.
    return GateDecision(True, "criteria_met")
