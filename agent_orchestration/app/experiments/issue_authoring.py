"""사전등록 필드를 Auto Research Issue Form 본문으로 옮기는 순수 함수 계층.

[파이프라인]
자율 실험 흐름의 첫 구간에서 호출자가 제출한 사전등록 필드를 `[AR]` 이슈 본문
문자열로 바꾸는 부분을 담당한다. GitHub 발행과 DB 저장은 각각 github_issues·service의
책임이다.

[기능]
호출자가 제출하는 필드(`IssueSubmission`)를 파서와 같은 규칙으로 검증하고 Issue Form
heading 본문을 만든다. 선언된 항목만 내보내므로 heading은 `연구 가설` 하나부터 여덟
개까지다. 재시도 복구용 experiment-id marker도 여기서 붙인다.

[비책임]
`tools/auto_research_issue_branch.py`의 파싱 계약과
`src/pipeline/experiment_evaluation.py`의 판정 정책은 이 모듈이 소유하지 않는다.

실행 설정과 허용 범위는 #570에서 본문에서 뺐다. 매 이슈 같은 값을 텍스트로 복사하던
것이라, 사본이 코드와 어긋날 위험만 남고 얻는 것이 없었다. 시드처럼 강제가 필요한
값은 이미 `src/pipeline/paired_experiment.py`가 실행 결과를 검사한다.

지표·guardrail 값을 LLM이 창작하던 경로는 #536에서 제거했다. 예측 모델링 사전등록
표준(arXiv 2311.18807)에서 성공 기준을 실험 전에 연구자가 선언하는 것이 제도의
핵심이므로, 에이전트가 임계값을 정하면 그 성질이 사라진다.
"""

from __future__ import annotations



from decimal import Decimal, InvalidOperation
import re
import uuid

from pydantic import BaseModel, ConfigDict, Field


# 실행 설정(시드·split·비율·비교 대상·스냅샷 재사용·데이터셋·학습 설정 참조)은 #570에서
# 본문에서 뺐다. 매 이슈 같은 값을 텍스트로 복사하던 것이고, 실제 강제는 코드가 한다 —
# 예를 들어 시드는 `src/pipeline/paired_experiment.py`가 실행 결과를 검사한다. 사본이
# 코드와 어긋날 수 있는 위험만 남고 얻는 것이 없었다.
_METRIC_DIRECTIONS = ("higher_is_better", "lower_is_better")
_NOT_APPLICABLE = "not_applicable"
_NONE_VALUE = "없음"
# `tools/auto_research_issue_branch.py`의 `_METRIC_NAME_PATTERN`과 같아야 한다.
_METRIC_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
# 값 안의 `### ` 줄은 heading을 하나 더 만들어 본문 구조를 깬다.
_HEADING_LINE_PATTERN = re.compile(r"^### ", re.MULTILINE)
# 제목에 ASCII 영소문자·숫자가 하나도 없으면 `_branch_slug()`가 브랜치 이름을
# `issue-<sha256[:12]>`로 대체한다. 그 이름은 발행과 함께 DB에 굳어 되돌릴 수 없으므로
# 사람이 식별할 수 없는 브랜치가 영구히 남는다. 발행 전에 끊는다.
_TITLE_ASCII_PATTERN = re.compile(r"[a-z0-9]")
# 파서의 `_finite_decimal`/`_validate_decimal_bounds`가 쓰는 경계와 같아야 한다.
# 여기가 더 느슨하면 극단값이 조립을 통과해 이슈가 발행된 뒤에야 실패한다.
MAX_DECIMAL_TEXT_LENGTH = 128
MAX_DECIMAL_DIGITS = 64
MAX_DECIMAL_EXPONENT = 1000


def _require_non_negative_decimal(value: str, field_name: str) -> None:
    """파서의 `_non_negative_decimal`이 받아들일 값인지 확인한다.

    파서(`_finite_decimal` → `_validate_decimal_bounds`)와 **같은 순서로 같은 경계**를
    본다. 한 축이라도 느슨하면 그 축의 극단값이 발행 후에야 거부된다.
    """
    if len(value) > MAX_DECIMAL_TEXT_LENGTH:
        raise ValueError(f"{field_name} exceeds the decimal text length limit")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{field_name} must be a finite decimal") from error
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal")
    decimal_tuple = parsed.as_tuple()
    if len(decimal_tuple.digits) > MAX_DECIMAL_DIGITS:
        raise ValueError(f"{field_name} exceeds the decimal digit limit")
    if abs(decimal_tuple.exponent) > MAX_DECIMAL_EXPONENT:
        raise ValueError(f"{field_name} exceeds the decimal exponent limit")
    if parsed < 0:
        raise ValueError(f"{field_name} must be non-negative")

_MARKER_PREFIX = "<!-- experiment-id:"


class IssueSubmission(BaseModel):
    """호출자가 제출하는 사전등록 필드. heading은 포함하지 않는다.

    예측 모델링 사전등록 표준(arXiv 2311.18807)의 Phase A 중 연구자가 선언해야 하는
    항목에 대응한다 — A.1 research question(`hypothesis`), A.3 independent
    variable(`change`), A.7 metrics(주 지표 3필드와 guardrail 3필드). A.4/A.5/A.8과
    Phase B의 학습 설정은 실험 간 비교가 성립하도록 서버가 고정하므로 여기에 없다.

    `title`과 `hypothesis`만 필수다(#570). 표준이 요구하는 A.3·A.7을 빈 채로 발행하면
    그 실험은 사전등록의 성질을 갖지 못하고 판정 대상에서 빠진다 — 지표를 어디서
    받을지가 정해지기 전까지의 과도기 상태이며, 정해지면 다시 필수가 된다.
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    hypothesis: str = Field(min_length=1, max_length=8000)
    # 사전등록 표준의 A.3(independent variable)은 `hypothesis` 자유 서술 안에 함께
    # 적는다. 별도 칸을 강제하면 그 틀 밖의 내용을 넣을 방법이 없다(#570).
    change: str = Field(default="", max_length=2000)
    # 주 지표는 비워 발행할 수 있다. 비면 `criteria_id`가 값 없는 상태로 봉인되고 그
    # 실험은 판정 대상이 아니다 — 값의 출처를 정하는 것은 #570의 후속 과제다.
    primary_metric_name: str = Field(default="", max_length=64)
    primary_metric_direction: str = ""
    minimum_primary_delta: str = ""
    # Guardrail은 새 기본값을 만들지 않고 계약에 이미 있는 미선언 표현을 쓴다.
    guardrail_metric_name: str = _NONE_VALUE
    guardrail_metric_direction: str = _NOT_APPLICABLE
    maximum_guardrail_regression: str = _NONE_VALUE
    secondary_metrics: str = Field(default="", max_length=2000)
    # 표준이 요구하지만 Issue Form에 없던 항목이다. 선택 섹션이므로 `criteria_id`·
    # `reproducibility_id` 계산에 들어가지 않아 기존 실험의 봉인값을 바꾸지 않는다.
    related_work: str = Field(default="", max_length=2000)

    def model_post_init(self, _context: object) -> None:
        """파서가 거부할 값을 조립 전에 끊는다.

        여기서 막지 못한 값은 이슈가 **발행된 뒤** 워크플로의 파서에서 실패한다.
        그때는 이미 GitHub에 이슈가 열려 있고 브랜치만 생기지 않는다. 그래서 파서가
        검사하는 것과 같은 규칙을 이 지점에서 먼저 적용한다. 이 모델은 요청 본문이므로
        위반은 FastAPI가 422로 돌려주며, 이슈는 아직 열리지 않은 상태다.
        """
        if not _TITLE_ASCII_PATTERN.search(self.title.lower()):
            raise ValueError(
                "title must contain at least one ASCII letter or digit "
                "so the experiment branch name stays identifiable"
            )
        # 주 지표는 세 값이 함께 선언되거나 함께 비어야 한다. 하나만 채우면 파서가
        # 부분 선언을 어느 쪽으로도 읽을 수 없다.
        primary_declared = self.primary_metric_name != ""
        if primary_declared != (self.primary_metric_direction != ""):
            raise ValueError("primary metric fields must be declared together")
        if primary_declared != (self.minimum_primary_delta != ""):
            raise ValueError("primary metric fields must be declared together")
        if primary_declared:
            if not _METRIC_NAME_PATTERN.fullmatch(self.primary_metric_name):
                raise ValueError("primary_metric_name is invalid")
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
        if primary_declared:
            _require_non_negative_decimal(
                self.minimum_primary_delta, "minimum_primary_delta"
            )
        if declared:
            _require_non_negative_decimal(
                self.maximum_guardrail_regression, "maximum_guardrail_regression"
            )
        # 값 안에 `### `로 시작하는 줄이 있으면 heading이 하나 더 생겨 본문 구조가
        # 깨진다. 사람이 마크다운 소제목을 쓰는 것은 충분히 있을 수 있다.
        for name, value in (
            ("hypothesis", self.hypothesis),
            ("change", self.change),
            ("secondary_metrics", self.secondary_metrics),
            ("related_work", self.related_work),
        ):
            if _HEADING_LINE_PATTERN.search(value):
                raise ValueError(f"{name} must not contain a '### ' heading line")


def marker_for(experiment_id: uuid.UUID) -> str:
    """재시도 시 GitHub에서 기존 이슈를 찾기 위한 HTML 주석 marker."""
    return f"{_MARKER_PREFIX} {experiment_id} -->"


def build_issue_title(fields: IssueSubmission) -> str:
    """Issue Form 관례를 따라 `[AR] ` prefix를 붙인다."""
    return f"[AR] {fields.title.strip()}"


def build_issue_body(
    experiment_id: uuid.UUID,
    fields: IssueSubmission,
) -> str:
    """제출 값을 Issue Form heading 본문으로 만든다.

    선언된 항목만 내보낸다. `연구 가설`을 뺀 나머지는 전부 선택이며, 값이 없으면
    heading 자체를 생략한다 — GitHub이 빈 칸에 `_No response_`를 넣는 것과 다르지만
    파서는 두 경우 모두 미선언으로 읽는다.

    실행 설정(시드·split·비율·비교 대상·스냅샷 재사용·데이터셋·학습 설정 참조)과 허용
    범위는 #570에서 뺐다. 매 이슈 같은 값을 텍스트로 복사하던 것이고, 실제 강제는
    코드가 한다.
    """
    sections: list[tuple[str, str]] = [("연구 가설", fields.hypothesis)]

    def append(name: str, value: str) -> None:
        if value.strip():
            sections.append((name, value.strip()))

    append("선행 연구 참조", fields.related_work)
    append("변경할 피처 · 모델", fields.change)
    if fields.primary_metric_name:
        sections.extend(
            (
                ("주 지표 이름", fields.primary_metric_name),
                ("주 지표 방향", fields.primary_metric_direction),
                ("최소 주 지표 개선폭", fields.minimum_primary_delta),
            )
        )
    if fields.guardrail_metric_name != _NONE_VALUE:
        sections.extend(
            (
                ("Guardrail 지표 이름", fields.guardrail_metric_name),
                ("Guardrail 지표 방향", fields.guardrail_metric_direction),
                ("최대 Guardrail 악화폭", fields.maximum_guardrail_regression),
            )
        )
    append("보조 관측 지표", fields.secondary_metrics)

    rendered = "\n\n".join(f"### {name}\n{value}" for name, value in sections)
    return f"{marker_for(experiment_id)}\n\n{rendered}\n"
