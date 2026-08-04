"""서버가 조립한 Issue Form 본문이 실제 파서를 통과하는지 고정한다.

전체 파이프라인에서 가설이 `[AR]` 이슈 본문으로 변환되는 구간만 검증한다. LLM 호출과
GitHub 발행은 이 모듈의 범위가 아니다 — 조립은 순수 함수라 둘 없이 검증할 수 있다.

런타임은 `tools/`와 `src/`를 import하지 않지만(API 이미지에 없음), 테스트는 저장소
전체를 보므로 파서·정책 상수와의 동일성을 여기서 고정한다.
"""

from __future__ import annotations

from pathlib import Path
import sys
import uuid

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_orchestration.app.experiments.issue_authoring import (  # noqa: E402
    COMPARISON,
    POLICY_SEEDS,
    SNAPSHOT_REUSE,
    ExperimentDefaults,
    LlmIssueFields,
    build_issue_body,
    build_issue_title,
    build_prompt,
    marker_for,
    parse_llm_fields,
)
from src.pipeline.experiment_evaluation import (  # noqa: E402
    POLICY_SEEDS as ENGINE_POLICY_SEEDS,
)
from tools.auto_research_issue_branch import (  # noqa: E402
    _COMPARISONS,
    _HEADING_NAMES,
    _METRIC_DIRECTIONS,
    _SCOPE_LABELS,
    _SNAPSHOT_REUSE,
    parse_issue_input,
)

EXPERIMENT_ID = uuid.UUID("3f2a1c9d-8b7e-4a1f-9c2d-5e6f7a8b9c0d")
DEFAULTS = ExperimentDefaults(
    dataset_snapshot="bq://autoresearch/train@2026-07-31",
    training_config_ref="configs/train/lgbm-v1.yaml@abc1234",
    dataset_window="- 데이터셋 / 경로: data/train.csv\n- 기간 (KST YYYY-MM-DD ~ YYYY-MM-DD): 2026-07-01 ~ 2026-07-31",
)


def _fields(**overrides: object) -> LlmIssueFields:
    payload: dict[str, object] = {
        "title": "views per day ratio feature",
        "hypothesis": "비율 피처가 ROC-AUC를 높인다.",
        "change": "- 추가 피처: views_per_day = views / (days + 1)",
        "primary_metric_name": "roc_auc",
        "primary_metric_direction": "higher_is_better",
        "minimum_primary_delta": "0.002",
        "guardrail_metric_name": "없음",
        "guardrail_metric_direction": "not_applicable",
        "maximum_guardrail_regression": "없음",
        "secondary_metrics": "pr_auc",
    }
    payload.update(overrides)
    return LlmIssueFields.model_validate(payload)


def test_assembled_body_passes_the_real_parser() -> None:
    """조립 결과가 워크플로가 쓰는 파서를 그대로 통과해야 한다."""
    body = build_issue_body(EXPERIMENT_ID, _fields(), DEFAULTS, allowed_scope=())

    parsed = parse_issue_input(1, build_issue_title(_fields()), body)

    assert parsed.primary_metric_name == "roc_auc"
    assert parsed.guardrail_metric_name is None


def test_assembled_body_uses_the_policy_seed_set() -> None:
    """시드가 어긋나면 모든 실험이 comparison_failed로 끝난다(#493 시나리오 2)."""
    body = build_issue_body(EXPERIMENT_ID, _fields(), DEFAULTS, allowed_scope=())

    parsed = parse_issue_input(1, "[AR] seed", body)

    assert parsed.random_seeds == POLICY_SEEDS


def test_local_policy_seeds_match_the_judgement_engine() -> None:
    """런타임 import가 불가능해 복제한 값의 드리프트를 잡는다."""
    assert POLICY_SEEDS == ENGINE_POLICY_SEEDS


def test_guardrail_fields_round_trip_when_declared() -> None:
    """guardrail 세 필드가 함께 채워지는 경로도 파서를 통과한다."""
    body = build_issue_body(
        EXPERIMENT_ID,
        _fields(
            guardrail_metric_name="logloss",
            guardrail_metric_direction="lower_is_better",
            maximum_guardrail_regression="0.001",
        ),
        DEFAULTS,
        allowed_scope=(),
    )

    parsed = parse_issue_input(1, "[AR] guardrail", body)

    assert parsed.guardrail_metric_name == "logloss"


def test_allowed_scope_checkboxes_are_rendered_and_parsed() -> None:
    """체크한 범위만 allowed_scope로 나와야 한다."""
    body = build_issue_body(
        EXPERIMENT_ID,
        _fields(),
        DEFAULTS,
        allowed_scope=("prod_model_contract", "promotion"),
    )

    parsed = parse_issue_input(1, "[AR] scope", body)

    assert set(parsed.allowed_scope) == {"prod_model_contract", "promotion"}


def test_marker_does_not_change_sealed_identifiers() -> None:
    """marker는 재시도 복구용이며 실험 정의를 바꾸면 안 된다."""
    fields = _fields()
    with_marker = build_issue_body(EXPERIMENT_ID, fields, DEFAULTS, allowed_scope=())
    without_marker = with_marker.replace(marker_for(EXPERIMENT_ID) + "\n\n", "")

    sealed = parse_issue_input(1, "[AR] marker", with_marker)
    bare = parse_issue_input(1, "[AR] marker", without_marker)

    assert sealed.criteria_id == bare.criteria_id
    assert sealed.reproducibility_id == bare.reproducibility_id


def test_title_gets_the_ar_prefix() -> None:
    """제목 prefix는 강제되지 않지만 Issue Form 관례를 따른다."""
    assert build_issue_title(_fields()).startswith("[AR] ")


def test_parse_llm_fields_rejects_non_json() -> None:
    """LLM이 산문으로 답하면 조립 전에 끊어야 한다."""
    with pytest.raises(ValueError):
        parse_llm_fields("죄송합니다. 요청을 이해하지 못했습니다.")


def test_parse_llm_fields_rejects_a_malformed_guardrail_metric_name() -> None:
    """선언된 guardrail 이름도 파서의 정규식을 받는다.

    여기서 막지 못하면 이슈가 발행된 뒤에야 워크플로에서 실패한다.
    """
    with pytest.raises(ValueError):
        _fields(
            guardrail_metric_name="log loss",
            guardrail_metric_direction="lower_is_better",
            maximum_guardrail_regression="0.001",
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("minimum_primary_delta", "약간"),
        ("minimum_primary_delta", "-0.002"),
        ("maximum_guardrail_regression", "조금"),
    ],
)
def test_parse_llm_fields_rejects_non_decimal_thresholds(field: str, value: str) -> None:
    """임계값이 숫자가 아니면 조립 전에 끊는다."""
    overrides: dict[str, object] = {field: value}
    if field == "maximum_guardrail_regression":
        overrides["guardrail_metric_name"] = "logloss"
        overrides["guardrail_metric_direction"] = "lower_is_better"

    with pytest.raises(ValueError):
        _fields(**overrides)


def test_parse_llm_fields_rejects_an_embedded_heading_line() -> None:
    """값 안의 `### ` 줄은 heading을 하나 더 만들어 본문 구조를 깬다."""
    with pytest.raises(ValueError):
        _fields(hypothesis="가설 요약\n### 연구 가설\n(설명)")


def test_parse_llm_fields_rejects_invalid_metric_direction() -> None:
    """파서가 거부할 값을 조립 전에 거부한다."""
    with pytest.raises(ValueError):
        parse_llm_fields('{"title": "t", "hypothesis": "h", "change": "c", '
                         '"primary_metric_name": "roc_auc", '
                         '"primary_metric_direction": "maximize", '
                         '"minimum_primary_delta": "0.002", '
                         '"guardrail_metric_name": "없음", '
                         '"guardrail_metric_direction": "not_applicable", '
                         '"maximum_guardrail_regression": "없음", '
                         '"secondary_metrics": ""}')


def test_prompt_carries_every_llm_owned_exact_string() -> None:
    """LLM 담당 필드의 정확 문자열이 빠지면 거부당할 값을 낸다."""
    prompt = build_prompt("비율 피처가 ROC-AUC를 높인다.")

    for direction in _METRIC_DIRECTIONS:
        assert direction in prompt
    assert "not_applicable" in prompt
    assert "없음" in prompt


def test_prompt_shows_only_the_server_chosen_options() -> None:
    """서버 소유 필드는 고른 값만 참고로 싣는다.

    나머지 옵션까지 실으면 LLM이 그중에서 고를 수 있다고 오해한다. `비교 대상`과
    `스냅샷 재사용`은 §결정 2에서 서버 소유로 정해졌다.
    """
    prompt = build_prompt("비율 피처가 ROC-AUC를 높인다.")

    assert COMPARISON in prompt
    assert SNAPSHOT_REUSE in prompt
    for option in (_COMPARISONS | _SNAPSHOT_REUSE) - {COMPARISON, SNAPSHOT_REUSE}:
        assert option not in prompt


def test_prompt_omits_server_owned_field_names() -> None:
    """LLM이 서버 소유 필드를 채우려 들지 않게 한다."""
    prompt = build_prompt("비율 피처가 ROC-AUC를 높인다.")

    assert "랜덤 시드 목록" not in prompt
    assert "Split 시드" not in prompt


def test_body_covers_every_required_heading() -> None:
    """heading이 빠지면 파서가 fail-closed된다."""
    body = build_issue_body(EXPERIMENT_ID, _fields(), DEFAULTS, allowed_scope=())

    for heading in _HEADING_NAMES:
        if heading == "결과 (에이전트가 채웁니다)":
            continue
        assert f"### {heading}" in body


def test_scope_labels_match_the_parser() -> None:
    """체크박스 label 문자열이 어긋나면 allowed_scope 파싱이 실패한다."""
    body = build_issue_body(EXPERIMENT_ID, _fields(), DEFAULTS, allowed_scope=())

    for label in _SCOPE_LABELS:
        assert f"- [ ] {label}" in body or f"- [x] {label}" in body
