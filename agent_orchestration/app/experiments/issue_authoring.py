"""사전등록 필드를 Auto Research Issue Form 본문으로 옮기는 순수 함수 계층.

[파이프라인]
자율 실험 흐름의 첫 구간에서 호출자가 제출한 사전등록 필드를 `[AR]` 이슈 본문
문자열로 바꾸는 부분을 담당한다. GitHub 발행과 DB 저장은 각각 github_issues·service의
책임이다.

[기능]
호출자가 제출하는 필드(`IssueSubmission`)를 파서와 같은 규칙으로 검증하고, 서버 소유
실행 설정과 결합해 heading 21개짜리 본문을 만든다. 재시도 복구용 experiment-id marker도
여기서 붙인다.

[비책임]
`tools/auto_research_issue_branch.py`의 파싱 계약과
`src/pipeline/experiment_evaluation.py`의 판정 정책은 이 모듈이 소유하지 않는다. 두 곳은
API 이미지에 없어 import할 수 없으므로 값을 복제하며, 동일성은
`tests/test_issue_authoring.py`가 CI에서 고정한다.

지표·guardrail 값을 LLM이 창작하던 경로는 #536에서 제거했다. 예측 모델링 사전등록
표준(arXiv 2311.18807)에서 성공 기준을 실험 전에 연구자가 선언하는 것이 제도의
핵심이므로, 에이전트가 임계값을 정하면 그 성질이 사라진다.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
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

# `docs/specs/2026-07-24-action-log-slice-semantics.md`의 소비 계약 `dt BETWEEN P-30
# AND P-1`을 따른다. 이 값을 바꾸면 발행되는 실험의 학습 구간이 달라진다.
TRAINING_WINDOW_DAYS = 30
# `src/pipeline/config.yaml`의 `data.path`와 같은 값이다. 사람이 읽는 설명에만 쓰이고
# `reproducibility_id` 해시에는 들어가지 않는다.
DATASET_PATH = "data/processed/training_dataset.csv"

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

# `tools/auto_research_issue_branch.py`의 `_SCOPE_LABELS`와 문자 그대로 같아야 한다.
SCOPE_LABELS: dict[str, str] = {
    "prod_model_contract": (
        "prod 모델 계약(`src/features/model_contract.py`) 수정을 허용한다"
    ),
    "feast_definition": "Feast 정의(`feature_repo/`) 수정을 허용한다",
    "promotion": "실험 결과를 champion으로 승격하는 것까지 검토한다",
}

_MARKER_PREFIX = "<!-- experiment-id:"


class IssueSubmission(BaseModel):
    """호출자가 제출하는 사전등록 필드. heading은 포함하지 않는다.

    예측 모델링 사전등록 표준(arXiv 2311.18807)의 Phase A 중 연구자가 선언해야 하는
    항목에 대응한다 — A.1 research question(`hypothesis`), A.3 independent
    variable(`change`), A.7 metrics(주 지표 3필드와 guardrail 3필드). A.4/A.5/A.8과
    Phase B의 학습 설정은 실험 간 비교가 성립하도록 서버가 고정하므로 여기에 없다.
    """

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
        # 깨진다. 사람이 마크다운 소제목을 쓰는 것은 충분히 있을 수 있다.
        for name, value in (
            ("hypothesis", self.hypothesis),
            ("change", self.change),
            ("secondary_metrics", self.secondary_metrics),
            ("related_work", self.related_work),
        ):
            if _HEADING_LINE_PATTERN.search(value):
                raise ValueError(f"{name} must not contain a '### ' heading line")


class ExperimentDefaults(BaseModel):
    """환경마다 달라지는 서버 소유 값.

    기간은 여기 두지 않는다 — 고정 문자열로 두면 첫날부터 낡는다.
    `training_window()`가 발행 시점에 계산한다.
    """

    model_config = ConfigDict(extra="forbid")

    dataset_source: str = Field(min_length=1, max_length=200)
    training_config_ref: str = Field(min_length=1, max_length=256)


def training_window(today: date) -> tuple[date, date]:
    """학습 대상 기간을 KST 기준으로 계산한다.

    오늘 파티션은 아직 채워지는 중이므로 어제까지 본다. 시계를 직접 읽지 않고 인자로
    받는다 — 그래야 테스트가 실행 날짜에 흔들리지 않는다.
    """
    end = today - timedelta(days=1)
    start = end - timedelta(days=TRAINING_WINDOW_DAYS - 1)
    return start, end


def marker_for(experiment_id: uuid.UUID) -> str:
    """재시도 시 GitHub에서 기존 이슈를 찾기 위한 HTML 주석 marker."""
    return f"{_MARKER_PREFIX} {experiment_id} -->"


def build_issue_title(fields: IssueSubmission) -> str:
    """Issue Form 관례를 따라 `[AR] ` prefix를 붙인다."""
    return f"[AR] {fields.title.strip()}"


def build_issue_body(
    experiment_id: uuid.UUID,
    fields: IssueSubmission,
    defaults: ExperimentDefaults,
    allowed_scope: Sequence[str],
    window: tuple[date, date],
) -> str:
    """제출 값과 서버 소유 값을 heading과 결합해 Issue Form 본문을 만든다."""
    unknown = set(allowed_scope) - set(SCOPE_LABELS)
    if unknown:
        raise ValueError("unknown allowed scope: " + ", ".join(sorted(unknown)))

    checked = set(allowed_scope)
    scope_lines = "\n".join(
        f"- [{'x' if key in checked else ' '}] {label}"
        for key, label in SCOPE_LABELS.items()
    )
    seeds = ", ".join(str(seed) for seed in POLICY_SEEDS)
    window_start, window_end = window
    dataset_snapshot = f"{defaults.dataset_source}@{window_start}..{window_end}"
    dataset_window = (
        f"- 데이터셋 / 경로: {DATASET_PATH}\n"
        f"- 기간 (KST YYYY-MM-DD ~ YYYY-MM-DD): {window_start} ~ {window_end}"
    )
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
        ("데이터셋 스냅샷", dataset_snapshot),
        ("랜덤 시드 목록", seeds),
        ("Split 시드", str(SPLIT_SEED)),
        ("Test 비율", TEST_SIZE),
        ("Validation 비율", VALIDATION_SIZE),
        ("학습 설정 참조", defaults.training_config_ref),
        ("대상 데이터 · 기간", dataset_window),
        ("스냅샷 재사용", SNAPSHOT_REUSE),
        ("허용 범위", scope_lines),
    ]
    # 선택 섹션은 비우면 GitHub이 `_No response_`를 넣는 것과 달리 heading 자체를
    # 생략한다(파서는 두 경우 모두 통과한다).
    if fields.secondary_metrics.strip():
        insert_at = next(
            index for index, (name, _) in enumerate(sections) if name == "비교 대상"
        )
        sections.insert(
            insert_at, ("보조 관측 지표", fields.secondary_metrics.strip())
        )
    if fields.related_work.strip():
        insert_at = next(
            index
            for index, (name, _) in enumerate(sections)
            if name == "변경할 피처 · 모델"
        )
        sections.insert(insert_at, ("선행 연구 참조", fields.related_work.strip()))

    rendered = "\n\n".join(f"### {name}\n{value}" for name, value in sections)
    return f"{marker_for(experiment_id)}\n\n{rendered}\n"
