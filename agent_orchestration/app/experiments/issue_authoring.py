"""가설을 Auto Research Issue Form 본문으로 옮기는 순수 함수 계층.

[파이프라인]
자율 실험 흐름의 첫 구간에서 자연어 가설을 `[AR]` 이슈 본문 문자열로 바꾸는 부분을
담당한다. LLM 호출, GitHub 발행, DB 저장은 각각 llm·github_issues·service의 책임이다.

[기능]
LLM에 보낼 프롬프트를 조립하고, LLM이 낸 JSON을 검증된 필드로 파싱하며, 서버 소유
실행 설정과 결합해 heading 20개짜리 본문을 만든다. 재시도 복구용 experiment-id marker도
여기서 붙인다.

[비책임]
`tools/auto_research_issue_branch.py`의 파싱 계약과
`src/pipeline/experiment_evaluation.py`의 판정 정책은 이 모듈이 소유하지 않는다. 두 곳은
API 이미지에 없어 import할 수 없으므로 값을 복제하며, 동일성은
`tests/test_issue_authoring.py`가 CI에서 고정한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
import json
import re
import uuid

from pydantic import BaseModel, ConfigDict, Field


# `src/pipeline/experiment_evaluation.py`의 POLICY_SEEDS와 같아야 한다. 어긋나면
# paired_experiment가 MISSING_PAIRED_RUN으로 끊어 모든 실험이 comparison_failed가 된다.
POLICY_SEEDS: tuple[int, ...] = tuple(range(42, 72))

# 정책 상수 — 환경이 아니라 실험 방법론이 정한다.
COMPARISON = "동일 조건 baseline 재학습 (권장)"
# 자동 발행 경로는 사람이 데이터 이상을 판단할 수 없으므로 스냅샷 재사용을 막는다.
SNAPSHOT_REUSE = "불허 (정규 조립 경로 실패 시 중단)"
# 실험 간 동일 분할을 강제해 서로 다른 실험의 지표를 비교 가능하게 한다.
SPLIT_SEED = 20260801
TEST_SIZE = "0.2"
VALIDATION_SIZE = "0.2"

_METRIC_DIRECTIONS = ("higher_is_better", "lower_is_better")
_NOT_APPLICABLE = "not_applicable"
_NONE_VALUE = "없음"
# `tools/auto_research_issue_branch.py`의 `_METRIC_NAME_PATTERN`과 같아야 한다.
_METRIC_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
# 값 안의 `### ` 줄은 heading을 하나 더 만들어 본문 구조를 깬다.
_HEADING_LINE_PATTERN = re.compile(r"^### ", re.MULTILINE)


def _require_non_negative_decimal(value: str, field_name: str) -> None:
    """파서의 `_non_negative_decimal`이 받아들일 값인지 확인한다."""
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{field_name} must be a decimal") from error
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite")
    if parsed < 0:
        raise ValueError(f"{field_name} must be non-negative")

# `tools/auto_research_issue_branch.py`의 `_SCOPE_LABELS`와 문자 그대로 같아야 한다.
SCOPE_LABELS: dict[str, str] = {
    "prod_model_contract": (
        "prod 모델 계약(`src/features/model_contract.py`) 수정을 허용한다"
    ),
    "feast_definition": "Feast 정의(`feature_repo/`) 수정을 허용한다",
    "promotion": "실험 결과를 champion으로 승격하는 것까지 검토한다",
}

_MARKER_PREFIX = "<!-- experiment-id:"


class LlmIssueFields(BaseModel):
    """LLM이 가설에서 유도해 반환하는 값. heading은 포함하지 않는다."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    hypothesis: str = Field(min_length=1, max_length=2000)
    change: str = Field(min_length=1, max_length=2000)
    primary_metric_name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
    primary_metric_direction: str
    minimum_primary_delta: str
    guardrail_metric_name: str
    guardrail_metric_direction: str
    maximum_guardrail_regression: str
    secondary_metrics: str = Field(default="", max_length=2000)

    def model_post_init(self, _context: object) -> None:
        """파서가 거부할 값을 조립 전에 끊는다.

        여기서 막지 못한 값은 이슈가 **발행된 뒤** 워크플로의 파서에서 실패한다.
        그때는 이미 GitHub에 이슈가 열려 있고 브랜치만 생기지 않는다. 그래서 파서가
        검사하는 것과 같은 규칙을 이 지점에서 먼저 적용한다.
        """
        if self.primary_metric_direction not in _METRIC_DIRECTIONS:
            raise ValueError("primary_metric_direction is invalid")
        if self.guardrail_metric_direction not in (
            *_METRIC_DIRECTIONS,
            _NOT_APPLICABLE,
        ):
            raise ValueError("guardrail_metric_direction is invalid")
        declared = self.guardrail_metric_name != _NONE_VALUE
        if declared != (self.guardrail_metric_direction != _NOT_APPLICABLE):
            raise ValueError("guardrail fields must be declared together")
        if declared != (self.maximum_guardrail_regression != _NONE_VALUE):
            raise ValueError("guardrail fields must be declared together")
        # 선언된 guardrail 이름은 주 지표와 같은 규칙을 받는다. pydantic의 `pattern`으로
        # 걸 수 없는 이유는 미선언 시 `없음`이 들어오기 때문이다.
        if declared and not _METRIC_NAME_PATTERN.fullmatch(self.guardrail_metric_name):
            raise ValueError("guardrail_metric_name is invalid")
        _require_non_negative_decimal(
            self.minimum_primary_delta, "minimum_primary_delta"
        )
        if declared:
            _require_non_negative_decimal(
                self.maximum_guardrail_regression, "maximum_guardrail_regression"
            )
        # 값 안에 `### `로 시작하는 줄이 있으면 heading이 하나 더 생겨 본문 구조가
        # 깨진다. LLM이 마크다운 소제목을 쓰는 것은 충분히 있을 수 있다.
        for name, value in (
            ("hypothesis", self.hypothesis),
            ("change", self.change),
            ("secondary_metrics", self.secondary_metrics),
        ):
            if _HEADING_LINE_PATTERN.search(value):
                raise ValueError(f"{name} must not contain a '### ' heading line")


class ExperimentDefaults(BaseModel):
    """환경마다 달라지는 서버 소유 값."""

    model_config = ConfigDict(extra="forbid")

    dataset_snapshot: str = Field(min_length=1, max_length=256)
    training_config_ref: str = Field(min_length=1, max_length=256)
    dataset_window: str = Field(min_length=1)


def marker_for(experiment_id: uuid.UUID) -> str:
    """재시도 시 GitHub에서 기존 이슈를 찾기 위한 HTML 주석 marker."""
    return f"{_MARKER_PREFIX} {experiment_id} -->"


def build_issue_title(fields: LlmIssueFields) -> str:
    """Issue Form 관례를 따라 `[AR] ` prefix를 붙인다."""
    return f"[AR] {fields.title.strip()}"


def parse_llm_fields(text: str) -> LlmIssueFields:
    """LLM 응답 텍스트에서 JSON 객체를 뽑아 검증한다."""
    stripped = text.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("LLM response does not contain a JSON object")
    try:
        payload = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as error:
        raise ValueError("LLM response is not valid JSON") from error
    return LlmIssueFields.model_validate(payload)


def build_prompt(hypothesis: str) -> str:
    """LLM이 담당 필드만 JSON으로 내도록 지시하는 프롬프트를 만든다."""
    directions = " 또는 ".join(f"`{value}`" for value in _METRIC_DIRECTIONS)
    comparison_options = "\n".join(
        f"  - {value}" for value in (COMPARISON, SNAPSHOT_REUSE)
    )
    return f"""당신은 CTR 모델 실험 설계를 돕습니다. 아래 가설을 읽고 **JSON 객체 하나만**
출력하십시오. 설명·머리말·코드펜스를 붙이지 마십시오.

## 가설

{hypothesis}

## 출력할 JSON 키

- `title`: 60자 이내 실험 제목. **ASCII 영소문자와 숫자를 반드시 포함**하십시오 —
  브랜치 이름이 이 제목에서 만들어지며, ASCII 조각이 없으면 해시로 대체되어 사람이
  식별할 수 없습니다.
- `hypothesis`: 무엇이 왜 개선되는지 한두 문장.
- `change`: 바꿀 피처나 모델을 코드로 옮길 수 있을 만큼 구체적으로.
- `primary_metric_name`: `^[A-Za-z][A-Za-z0-9._-]{{0,63}}$`. 이 프로젝트의 주 지표는
  `roc_auc`입니다.
- `primary_metric_direction`: {directions}
- `minimum_primary_delta`: 0 이상 십진수 문자열. 예 `"0.002"`.
- `guardrail_metric_name`, `guardrail_metric_direction`,
  `maximum_guardrail_regression`: guardrail을 쓰지 않으면 각각 `"{_NONE_VALUE}"`,
  `"{_NOT_APPLICABLE}"`, `"{_NONE_VALUE}"`로 **세 개를 함께** 채우십시오. 쓰면 세 개를
  모두 실제 값으로 채우십시오. 섞으면 거부됩니다. guardrail을 쓸 때
  `guardrail_metric_name`은 주 지표와 같은 규칙(`^[A-Za-z][A-Za-z0-9._-]{{0,63}}$`)을
  따르고, `maximum_guardrail_regression`은 0 이상 십진수 문자열입니다.
- `secondary_metrics`: 보조 관측 지표. 없으면 빈 문자열.

어떤 값에도 `### `로 시작하는 줄을 넣지 마십시오. 이슈 본문의 heading 구조가 깨집니다.

## 참고 — 서버가 채우는 값 (당신은 출력하지 않습니다)

{comparison_options}

## 예시 출력

{{"title": "views per day ratio feature",
  "hypothesis": "비율 피처가 ROC-AUC를 높인다.",
  "change": "- 추가 피처: views_per_day = views / (days + 1)",
  "primary_metric_name": "roc_auc",
  "primary_metric_direction": "higher_is_better",
  "minimum_primary_delta": "0.002",
  "guardrail_metric_name": "{_NONE_VALUE}",
  "guardrail_metric_direction": "{_NOT_APPLICABLE}",
  "maximum_guardrail_regression": "{_NONE_VALUE}",
  "secondary_metrics": "pr_auc"}}
"""


def build_issue_body(
    experiment_id: uuid.UUID,
    fields: LlmIssueFields,
    defaults: ExperimentDefaults,
    allowed_scope: Sequence[str],
) -> str:
    """LLM 값과 서버 소유 값을 heading과 결합해 Issue Form 본문을 만든다."""
    unknown = set(allowed_scope) - set(SCOPE_LABELS)
    if unknown:
        raise ValueError("unknown allowed scope: " + ", ".join(sorted(unknown)))

    checked = set(allowed_scope)
    scope_lines = "\n".join(
        f"- [{'x' if key in checked else ' '}] {label}"
        for key, label in SCOPE_LABELS.items()
    )
    seeds = ", ".join(str(seed) for seed in POLICY_SEEDS)
    sections: list[tuple[str, str]] = [
        ("연구 가설", fields.hypothesis),
        ("변경할 피처 · 모델", fields.change),
        ("주 지표 이름", fields.primary_metric_name),
        ("주 지표 방향", fields.primary_metric_direction),
        ("최소 주 지표 개선폭", fields.minimum_primary_delta),
        ("Guardrail 지표 이름", fields.guardrail_metric_name),
        ("Guardrail 지표 방향", fields.guardrail_metric_direction),
        ("최대 Guardrail 악화폭", fields.maximum_guardrail_regression),
        ("비교 대상", COMPARISON),
        ("데이터셋 스냅샷", defaults.dataset_snapshot),
        ("랜덤 시드 목록", seeds),
        ("Split 시드", str(SPLIT_SEED)),
        ("Test 비율", TEST_SIZE),
        ("Validation 비율", VALIDATION_SIZE),
        ("학습 설정 참조", defaults.training_config_ref),
        ("대상 데이터 · 기간", defaults.dataset_window),
        ("스냅샷 재사용", SNAPSHOT_REUSE),
        ("허용 범위", scope_lines),
    ]
    # `보조 관측 지표`는 선택이며, 비우면 GitHub이 `_No response_`를 넣는 것과 달리
    # 여기서는 heading 자체를 생략한다(파서는 두 경우 모두 통과한다).
    if fields.secondary_metrics.strip():
        insert_at = next(
            index for index, (name, _) in enumerate(sections) if name == "비교 대상"
        )
        sections.insert(
            insert_at, ("보조 관측 지표", fields.secondary_metrics.strip())
        )

    rendered = "\n\n".join(f"### {name}\n{value}" for name, value in sections)
    return f"{marker_for(experiment_id)}\n\n{rendered}\n"
