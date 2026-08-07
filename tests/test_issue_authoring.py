"""사전등록 필드가 파서를 통과하는 이슈 본문이 되는지 고정한다.

전체 파이프라인 중 호출자가 제출한 값이 `[AR]` 이슈 본문 문자열이 되는 구간을 검증한다.
GitHub 발행과 DB 기록은 각각 github_issues·service의 책임이라 여기서 보지 않는다.

조립과 파싱은 서로 다른 이미지에 있어 런타임에 import할 수 없다. 그래서 규칙을 복제하며,
그 복제본이 어긋나면 이슈가 **발행된 뒤** 워크플로에서 실패한다 — 그때는 이미 GitHub에
이슈가 열려 있고 브랜치만 생기지 않는다. 드리프트를 여기서 잡는다.
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
    MAX_DECIMAL_DIGITS,
    MAX_DECIMAL_EXPONENT,
    MAX_DECIMAL_TEXT_LENGTH,
    IssueSubmission,
    build_issue_body,
    build_issue_title,
    marker_for,
)
from tools.auto_research_issue_branch import (  # noqa: E402
    MAX_DECIMAL_DIGITS as PARSER_MAX_DECIMAL_DIGITS,
)
from tools.auto_research_issue_branch import (  # noqa: E402
    MAX_DECIMAL_EXPONENT as PARSER_MAX_DECIMAL_EXPONENT,
)
from tools.auto_research_issue_branch import (  # noqa: E402
    MAX_DECIMAL_TEXT_LENGTH as PARSER_MAX_DECIMAL_TEXT_LENGTH,
)
from tools.auto_research_issue_branch import (  # noqa: E402
    _METRIC_DIRECTIONS,
    parse_issue_input,
)

EXPERIMENT_ID = uuid.UUID("3f2a1c9d-8b7e-4a1f-9c2d-5e6f7a8b9c0d")

_MARKDOWN = "# 주제\n\n비율 피처가 ROC-AUC를 높인다.\n\n## 근거\n\n- `views_per_day`"


def _fields(**overrides: object) -> IssueSubmission:
    """UI가 실제로 보내는 최소 payload. 나머지는 서버 기본값이 채운다."""
    payload: dict[str, object] = {
        "title": "views per day ratio feature",
        "hypothesis": _MARKDOWN,
    }
    payload.update(overrides)
    return IssueSubmission.model_validate(payload)


def _body(**overrides: object) -> str:
    return build_issue_body(EXPERIMENT_ID, _fields(**overrides))


# ── 조립 결과가 파서를 통과하는가 ──────────────────────────────


def test_assembled_body_passes_the_real_parser() -> None:
    """조립 결과가 워크플로가 쓰는 파서를 그대로 통과해야 한다."""
    parsed = parse_issue_input(1, build_issue_title(_fields()), _body())

    assert parsed.hypothesis == _MARKDOWN
    assert parsed.primary_metric_name is None
    assert parsed.guardrail_metric_name is None


def test_minimal_body_carries_only_the_hypothesis() -> None:
    """실행 설정을 본문에 복사하지 않는다(#570).

    매 이슈 같은 값을 텍스트로 옮기던 것이고, 강제는 코드가 한다. 사본이 코드와
    어긋날 위험만 남았기에 뺐다.
    """
    body = _body()

    assert body.count("### ") == 1
    assert "### 연구 가설" in body
    for gone in (
        "랜덤 시드 목록",
        "Split 시드",
        "Test 비율",
        "Validation 비율",
        "비교 대상",
        "스냅샷 재사용",
        "데이터셋 스냅샷",
        "학습 설정 참조",
        "대상 데이터 · 기간",
        "허용 범위",
        "Guardrail 지표 이름",
    ):
        assert gone not in body


def test_undeclared_scope_never_widens_executor_permissions() -> None:
    """허용 범위가 없으면 아무 scope도 열리지 않아야 한다.

    부재가 권한을 넓히는 방향으로 읽히면 실행기가 `model_contract.py`와
    `feature_repo/`를 건드릴 수 있게 된다.
    """
    parsed = parse_issue_input(1, "[AR] scope", _body())

    assert parsed.allowed_scope == ()


def test_markdown_hypothesis_survives_assembly() -> None:
    """가설이 본문에서 손상되면 이슈에 다른 글이 실린다."""
    parsed = parse_issue_input(1, "[AR] markdown", _body())

    assert parsed.hypothesis.splitlines()[0] == "# 주제"
    assert "## 근거" in parsed.hypothesis


# ── 선택 항목은 선언했을 때만 나간다 ──────────────────────────


def test_declared_primary_metric_round_trips() -> None:
    """지표를 선언하면 세 heading이 살아나고 파서가 읽어야 한다."""
    body = _body(
        primary_metric_name="roc_auc",
        primary_metric_direction="higher_is_better",
        minimum_primary_delta="0.002",
    )

    parsed = parse_issue_input(1, "[AR] metric", body)

    assert parsed.primary_metric_name == "roc_auc"
    assert str(parsed.minimum_primary_delta) == "0.002"


def test_declared_guardrail_round_trips() -> None:
    """guardrail 세 필드가 함께 채워지는 경로도 파서를 통과한다."""
    body = _body(
        guardrail_metric_name="logloss",
        guardrail_metric_direction="lower_is_better",
        maximum_guardrail_regression="0.001",
    )

    parsed = parse_issue_input(1, "[AR] guardrail", body)

    assert parsed.guardrail_metric_name == "logloss"


def test_related_work_is_rendered_and_parsed_when_given() -> None:
    """선행 연구 링크를 주면 본문에 실리고 파서를 통과한다."""
    body = _body(related_work="https://arxiv.org/abs/2311.18807")

    assert "### 선행 연구 참조" in body
    parse_issue_input(1, "[AR] related work", body)


def test_optional_sections_do_not_change_sealed_identifiers() -> None:
    """선택 섹션이 봉인을 바꾸면 채웠다는 이유만으로 비교가 끊긴다."""
    bare = parse_issue_input(1, "[AR] opt", _body())
    linked = parse_issue_input(
        1, "[AR] opt", _body(related_work="https://arxiv.org/abs/2311.18807")
    )

    assert linked.criteria_id == bare.criteria_id
    assert linked.reproducibility_id == bare.reproducibility_id


def test_marker_does_not_change_sealed_identifiers() -> None:
    """marker는 재시도 복구용이며 실험 정의를 바꾸면 안 된다."""
    with_marker = _body()
    without_marker = with_marker.replace(marker_for(EXPERIMENT_ID) + "\n\n", "")

    sealed = parse_issue_input(1, "[AR] marker", with_marker)
    bare = parse_issue_input(1, "[AR] marker", without_marker)

    assert sealed.criteria_id == bare.criteria_id
    assert sealed.reproducibility_id == bare.reproducibility_id


def test_title_gets_the_ar_prefix() -> None:
    """제목 prefix는 강제되지 않지만 Issue Form 관례를 따른다."""
    assert build_issue_title(_fields()).startswith("[AR] ")


# ── 복제한 규칙이 파서와 어긋나지 않는가 ──────────────────────


def test_local_decimal_bounds_match_the_parser() -> None:
    """조립 전 검증이 파서보다 느슨해지는 드리프트를 잡는다.

    한 축이라도 느슨하면 그 축의 극단값이 이슈 발행 후에야 거부된다.
    """
    assert MAX_DECIMAL_TEXT_LENGTH == PARSER_MAX_DECIMAL_TEXT_LENGTH
    assert MAX_DECIMAL_DIGITS == PARSER_MAX_DECIMAL_DIGITS
    assert MAX_DECIMAL_EXPONENT == PARSER_MAX_DECIMAL_EXPONENT


def test_metric_directions_match_the_parser() -> None:
    """`IssueSubmission`이 받는 방향 값이 파서의 집합과 같아야 한다."""
    for direction in _METRIC_DIRECTIONS:
        _fields(
            primary_metric_name="roc_auc",
            primary_metric_direction=direction,
            minimum_primary_delta="0.002",
        )


# ── 발행 전에 끊어야 하는 값 ──────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "0." + "1" * 200,      # 길이 초과
        "0." + "1" * 70,       # 자릿수 초과
        "1e2000",              # 지수 초과
    ],
)
def test_submission_rejects_out_of_bound_decimals(value: str) -> None:
    """파서의 경계를 넘는 임계값도 조립 전에 끊는다."""
    with pytest.raises(ValueError):
        _fields(
            primary_metric_name="roc_auc",
            primary_metric_direction="higher_is_better",
            minimum_primary_delta=value,
        )


def test_submission_rejects_a_malformed_guardrail_metric_name() -> None:
    """선언된 guardrail 이름도 파서의 정규식을 받는다."""
    with pytest.raises(ValueError):
        _fields(
            guardrail_metric_name="log loss",
            guardrail_metric_direction="lower_is_better",
            maximum_guardrail_regression="0.001",
        )


def test_submission_rejects_invalid_metric_direction() -> None:
    """파서가 거부할 방향 값을 조립 전에 거부한다."""
    with pytest.raises(ValueError):
        _fields(
            primary_metric_name="roc_auc",
            primary_metric_direction="maximize",
            minimum_primary_delta="0.002",
        )


@pytest.mark.parametrize(
    "field", ["primary_metric_direction", "minimum_primary_delta"]
)
def test_submission_rejects_partially_declared_primary_metric(field: str) -> None:
    """하나만 채우면 파서가 부분 선언을 어느 쪽으로도 읽을 수 없다."""
    declared = {
        "primary_metric_name": "roc_auc",
        "primary_metric_direction": "higher_is_better",
        "minimum_primary_delta": "0.002",
    }
    declared[field] = ""

    with pytest.raises(ValueError):
        _fields(**declared)


def test_submission_rejects_server_owned_fields() -> None:
    """호출자가 시드 같은 값을 밀어 넣을 수 없다.

    `extra="forbid"`가 근거다. 실행 설정이 본문에서 빠졌으므로 요청으로도 들어올
    자리가 없어야 한다.
    """
    with pytest.raises(ValueError):
        _fields(random_seeds="1, 2, 3")


@pytest.mark.parametrize(
    "field", ["hypothesis", "change", "secondary_metrics", "related_work"]
)
def test_submission_rejects_an_embedded_heading_line(field: str) -> None:
    """값 안의 `### ` 줄은 heading을 하나 더 만들어 본문 구조를 깬다."""
    with pytest.raises(ValueError):
        _fields(**{field: "요약\n### 연구 가설\n(설명)"})
