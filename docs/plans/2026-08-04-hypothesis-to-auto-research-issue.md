# 가설 수신부터 `[AR]` 이슈 발행까지 구현 계획 (#516)

> **에이전트 작업자에게:** 이 계획은 task 단위로 구현합니다. 각 step은 체크박스
> (`- [ ]`)로 추적합니다.

**목표:** 사용자가 가설을 보내면 서버가 LLM으로 Issue Form 본문을 만들어 DB에 저장하고,
`gh`로 `[AR]` 이슈를 발행해 `auto-experiment` label로 exp 브랜치 생성을 트리거한다.

**아키텍처:** 추론 경계(Codex Runner)는 본문 값을 JSON으로만 제안하고, 신뢰된 서버
코드가 heading과 결합해 본문을 조립한다. 본문을 DB에 커밋한 뒤 발행하므로 재시도가
결정론적이다. 발행은 `gh` CLI 서브프로세스로 하며 요청 안에서 재시도하지 않는다.

**기술 스택:** FastAPI, SQLAlchemy 2.x, Alembic, pydantic v2, asyncio subprocess,
GitHub CLI(`gh`)

**정본 spec:** `docs/specs/2026-08-04-hypothesis-to-auto-research-issue.md`

## Global Constraints

- 언어는 한국어 격식체. 모듈 docstring은 `.claude/docs/agent-python-reference.md`의
  Module Responsibility 형식(파이프라인 구간 / 기능 / 비책임)을 따른다.
- 모든 함수에 반환 타입을 포함한 타입 힌트를 유지한다.
- **`agent_orchestration`은 `src/`와 `tools/`를 import할 수 없다.** `api.Dockerfile`이
  `agent_orchestration/`만 복사한다(실측 확인). 두 곳의 값이 필요하면 **런타임에는
  복제하고 테스트가 동일성을 고정**한다.
- 시드 집합은 `42..71` 오름차순 30개. `src/pipeline/experiment_evaluation.py:46`의
  `POLICY_SEEDS = tuple(range(42, 72))`와 같아야 한다.
- 새 필수 환경 변수를 도입하므로 **같은 PR에서** `README.md`와
  `.claude/docs/agent-project-reference.md`를 갱신한다(`CLAUDE.md` Core Rules).
- 커밋 메시지는 `<type>: <한국어 설명>`, 제목 50자 이내. 에이전트 서명·트레일러 금지.
- 검증 명령: `uv run --no-sync python -m pytest`,
  `uv run --no-sync ruff check agent_orchestration autoresearch tests tools`

## spec 대비 이 계획에서 확정한 것

spec이 열어 둔 세 가지를 구현 수준에서 확정한다. spec 갱신은 필요 없다 — 어느 것도
spec의 결정을 뒤집지 않는다.

1. **`POLICY_SEEDS` "참조"의 의미.** 런타임 import가 불가능하므로
   `issue_authoring.py`에 값을 복제하고, Task 2의 테스트가
   `src.pipeline.experiment_evaluation.POLICY_SEEDS`와의 동일성을 고정한다.
2. **이슈 제목의 출처.** spec이 정하지 않았다. LLM이 `title`을 함께 반환하고 서버가
   `[AR] ` prefix를 붙인다. 브랜치 slug 가독성을 위해 ASCII 조각을 남기도록 프롬프트에
   지시한다(제목에 ASCII 영소문자·숫자가 없으면 `issue-<sha256 앞 12자>`로 대체된다).
3. **서버 소유 9필드의 값 출처.** 정책값 6개는 모듈 상수, 환경 의존 3개는 설정으로
   나눈다. 근거는 Task 2에 적는다.

## 파일 구조

| 파일 | 책임 |
| --- | --- |
| `agent_orchestration/app/experiments/models.py` (수정) | `issue_body` / `issue_number` / `issue_branch` / `issue_published_at` 컬럼 |
| `agent_orchestration/migrations/versions/0002_experiment_issue_lineage.py` (신규) | 위 세 컬럼과 index |
| `agent_orchestration/app/experiments/issue_authoring.py` (신규) | 순수 함수. 프롬프트 조립, LLM JSON 파싱, 본문 조립 |
| `agent_orchestration/app/experiments/github_issues.py` (신규) | `gh` CLI 경계. 발행·조회·오류 분류 |
| `agent_orchestration/app/config.py` (수정) | `ORCH_GITHUB_*`, `ORCH_EXPERIMENT_*` 설정 |
| `agent_orchestration/app/experiments/service.py` (수정) | 생성→저장→발행 2단계 오케스트레이션 |
| `agent_orchestration/app/experiments/schemas.py` (수정) | 요청·응답 계약 |
| `agent_orchestration/app/experiments/exceptions.py` (수정) | 발행 도메인 오류 |
| `agent_orchestration/app/experiments/router.py` (수정) | `POST /experiments/{id}/issue` |
| `deploy/agent_orchestration/api.Dockerfile` (수정) | `gh` 버전 고정 설치 |

`issue_authoring.py`는 **LLM·GitHub·DB를 모르는 순수 함수만** 담는다. Task 2의 파서
대조 테스트가 성립하려면 이 성질이 유지되어야 한다.

---

## Task 1: 스키마 — lineage 컬럼 3개와 Alembic revision

**Files:**
- Modify: `agent_orchestration/app/experiments/models.py`
- Modify: `agent_orchestration/app/experiments/schemas.py`
- Create: `agent_orchestration/migrations/versions/0002_experiment_issue_lineage.py`
- Test: `tests/test_experiment_service.py`, `tests/test_experiment_issue_migration.py` (신규)

**Interfaces:**
- Produces: `Experiment.issue_body: str | None`, `Experiment.issue_number: int | None`,
  `Experiment.issue_branch: str | None`. `ExperimentResponse.issue_number`,
  `ExperimentResponse.issue_branch`.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_experiment_service.py` 끝에 추가합니다.

```python
def test_experiment_lineage_columns_default_to_none(db_session: Session) -> None:
    """발행 전 실험은 세 lineage 값이 모두 비어 있다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="lineage"))

    assert experiment.issue_body is None
    assert experiment.issue_number is None
    assert experiment.issue_branch is None
    assert experiment.issue_published_at is None


def test_experiment_response_exposes_issue_lineage_without_body(
    db_session: Session,
) -> None:
    """응답은 이슈 좌표만 싣고 본문은 싣지 않는다(목록 응답 비대화 방지)."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="lineage"))
    experiment.issue_body = "### 연구 가설\n본문"
    experiment.issue_number = 520
    experiment.issue_branch = "exp/520-ratio-feature"
    db_session.commit()

    response = ExperimentResponse.model_validate(experiment)

    assert response.issue_number == 520
    assert response.issue_branch == "exp/520-ratio-feature"
    assert not hasattr(response, "issue_body")
```

- [ ] **Step 2: 실패를 확인한다**

Run: `uv run --no-sync python -m pytest tests/test_experiment_service.py -k lineage -v`
Expected: FAIL — `AttributeError: 'Experiment' object has no attribute 'issue_body'`

- [ ] **Step 3: 모델에 컬럼을 추가한다**

`models.py`의 import에 `Integer`를 더합니다.

```python
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
```

`Experiment` 클래스의 `agent_session_id` 아래에 추가합니다.

```python
    # 발행 전에 커밋되는 본문. 재시도가 LLM을 다시 부르지 않고 같은 본문으로 발행하게
    # 해 criteria_id/reproducibility_id가 흔들리지 않도록 한다(#516).
    issue_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issue_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 일일 발행 상한 질의 전용. `updated_at`은 `onupdate=func.now()`라 발행과 무관한
    # UPDATE에도 갱신되어 며칠 전 발행분을 "오늘 발행"으로 잘못 센다.
    issue_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

`Experiment.__table_args__`(130행)의 튜플에 index를 더합니다.

```python
        Index("ix_experiments_issue_number", "issue_number"),
```

- [ ] **Step 4: 응답 스키마에 노출한다**

`schemas.py`의 `ExperimentResponse`에서 `agent_session_id` 아래에 추가합니다.

```python
    issue_number: int | None
    issue_branch: str | None
```

- [ ] **Step 5: 모듈 docstring을 갱신한다**

`models.py` 모듈 docstring의 마지막 문단 뒤에 추가합니다. 이 docstring은 migration과의
동일성을 단언하므로 같은 커밋에서 갱신해야 합니다.

```
`Experiment.issue_body`/`issue_number`/`issue_branch`/`issue_published_at`은
`0002_experiment_issue_lineage`
revision이 nullable로 추가한 발행 lineage다. `issue_body`는 발행 **전**에, 나머지 둘은
발행 성공 후에 채워진다.
```

- [ ] **Step 6: 테스트 통과를 확인한다**

Run: `uv run --no-sync python -m pytest tests/test_experiment_service.py -k lineage -v`
Expected: PASS (2 passed)

- [ ] **Step 7: Alembic revision을 만든다**

`agent_orchestration/migrations/versions/0002_experiment_issue_lineage.py`:

```python
"""Agent Orchestration 실험에 이슈 발행 lineage 컬럼을 추가한다.

전체 파이프라인에서 가설이 GitHub `[AR]` 이슈로 발행된 사실을 실험 행에 남기는 구간을
담당한다. 발행 절차와 HTTP 계약은 각각 service와 router의 책임이다.

`issue_body`(발행 전 커밋), `issue_number`, `issue_branch`, `issue_published_at`을
nullable로 추가하고
`issue_number` 조회 index를 만든 뒤 역순으로 제거한다.

Revision ID: 0002_experiment_issue_lineage
Revises: 0001_experiment_tables
Create Date: 2026-08-04
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_experiment_issue_lineage"
down_revision = "0001_experiment_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """발행 lineage 컬럼 셋과 issue_number index를 추가한다."""
    # 기존 행이 있으므로 셋 모두 nullable이다. issue_number에 unique를 두지 않는 것은
    # 이슈 1건이 실험 N건을 가질 수 있기 때문이다.
    op.add_column("experiments", sa.Column("issue_body", sa.Text(), nullable=True))
    op.add_column("experiments", sa.Column("issue_number", sa.Integer(), nullable=True))
    op.add_column(
        "experiments", sa.Column("issue_branch", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "experiments",
        sa.Column("issue_published_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_experiments_issue_number", "experiments", ["issue_number"])


def downgrade() -> None:
    """upgrade의 역순으로 index와 컬럼을 제거한다."""
    op.drop_index("ix_experiments_issue_number", table_name="experiments")
    op.drop_column("experiments", "issue_published_at")
    op.drop_column("experiments", "issue_branch")
    op.drop_column("experiments", "issue_number")
    op.drop_column("experiments", "issue_body")
```

- [ ] **Step 8: migration과 모델의 일치를 고정하는 테스트를 쓴다**

`tests/test_experiment_issue_migration.py`:

```python
"""0002 revision이 모델과 같은 lineage 컬럼을 만드는지 고정한다.

전체 파이프라인에서 실험 영속화 schema의 migration-모델 정합성만 검증한다. 발행 절차와
HTTP 계약은 이 모듈의 범위가 아니다.
"""

from __future__ import annotations

from pathlib import Path
import re

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REVISION = (
    PROJECT_ROOT
    / "agent_orchestration"
    / "migrations"
    / "versions"
    / "0002_experiment_issue_lineage.py"
)
MODELS = (
    PROJECT_ROOT / "agent_orchestration" / "app" / "experiments" / "models.py"
)

LINEAGE_COLUMNS = (
    "issue_body",
    "issue_number",
    "issue_branch",
    "issue_published_at",
)


def test_revision_chains_to_the_initial_revision() -> None:
    """revision 체인이 끊기면 배포 시 마이그레이션이 적용되지 않는다."""
    text = REVISION.read_text(encoding="utf-8")

    assert 'revision = "0002_experiment_issue_lineage"' in text
    assert 'down_revision = "0001_experiment_tables"' in text


def test_upgrade_and_downgrade_are_symmetric() -> None:
    """downgrade가 upgrade가 만든 것을 모두 되돌린다."""
    text = REVISION.read_text(encoding="utf-8")
    added = set(re.findall(r'op\.add_column\(\s*"experiments",\s*sa\.Column\(\s*"(\w+)"', text))
    dropped = set(re.findall(r'op\.drop_column\(\s*"experiments",\s*"(\w+)"', text))

    assert added == set(LINEAGE_COLUMNS)
    assert added == dropped
    assert 'op.create_index("ix_experiments_issue_number"' in text
    assert 'op.drop_index("ix_experiments_issue_number"' in text


def test_model_declares_the_same_lineage_columns() -> None:
    """migration만 바뀌고 모델이 남는 드리프트를 잡는다."""
    text = MODELS.read_text(encoding="utf-8")

    for column in LINEAGE_COLUMNS:
        assert f"{column}: Mapped[" in text
    assert 'Index("ix_experiments_issue_number", "issue_number")' in text
```

- [ ] **Step 9: 전체 확인**

Run: `uv run --no-sync python -m pytest tests/test_experiment_issue_migration.py tests/test_experiment_service.py -q`
Expected: PASS

Run: `uv run --no-sync ruff check agent_orchestration tests`
Expected: `All checks passed!`

- [ ] **Step 10: 커밋**

```bash
git add agent_orchestration/app/experiments/models.py \
        agent_orchestration/app/experiments/schemas.py \
        agent_orchestration/migrations/versions/0002_experiment_issue_lineage.py \
        tests/test_experiment_issue_migration.py \
        tests/test_experiment_service.py
git commit -m "feat: 실험에 이슈 발행 lineage 컬럼 추가"
```

---

## Task 2: 본문 조립 — `issue_authoring.py`

**Files:**
- Create: `agent_orchestration/app/experiments/issue_authoring.py`
- Test: `tests/test_issue_authoring.py` (신규)

**Interfaces:**
- Consumes: 없음 (순수 함수 모듈)
- Produces:
  - `POLICY_SEEDS: tuple[int, ...]`
  - `class LlmIssueFields(BaseModel)` — `title`, `hypothesis`, `change`,
    `primary_metric_name`, `primary_metric_direction`, `minimum_primary_delta`,
    `guardrail_metric_name`, `guardrail_metric_direction`,
    `maximum_guardrail_regression`, `secondary_metrics`
  - `class ExperimentDefaults(BaseModel)` — `dataset_snapshot`, `training_config_ref`,
    `dataset_window`
  - `build_prompt(hypothesis: str) -> str`
  - `parse_llm_fields(text: str) -> LlmIssueFields`
  - `build_issue_body(experiment_id: uuid.UUID, fields: LlmIssueFields, defaults: ExperimentDefaults, allowed_scope: Sequence[str]) -> str`
  - `build_issue_title(fields: LlmIssueFields) -> str`
  - `marker_for(experiment_id: uuid.UUID) -> str`

**서버 소유 9필드의 값 출처 (이 task에서 확정)**

| 필드 | 출처 | 근거 |
| --- | --- | --- |
| 비교 대상 | 모듈 상수 `"동일 조건 baseline 재학습 (권장)"` | 판정 엔진이 paired 재학습을 전제한다 |
| 스냅샷 재사용 | 모듈 상수 `"불허 (정규 조립 경로 실패 시 중단)"` | 자동 발행은 사람이 데이터 이상을 판단할 수 없으므로 보수적으로 막는다 |
| 랜덤 시드 목록 | 모듈 상수 `POLICY_SEEDS` | 판정 통과 가능한 유일한 집합 |
| Split 시드 | 모듈 상수 `20260801` | 실험 간 동일 분할을 강제해 비교를 의미 있게 만든다 |
| Test 비율 | 모듈 상수 `"0.2"` | fixture와 동일 |
| Validation 비율 | 모듈 상수 `"0.2"` | 합이 1 미만이어야 한다 |
| 데이터셋 스냅샷 | 설정 (Task 3) | 환경마다 다르다 |
| 학습 설정 참조 | 설정 (Task 3) | 환경마다 다르다 |
| 대상 데이터 · 기간 | 설정 (Task 3) | 환경마다 다르다 |

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_issue_authoring.py`:

```python
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
    MAX_DECIMAL_DIGITS,
    MAX_DECIMAL_EXPONENT,
    MAX_DECIMAL_TEXT_LENGTH,
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
    MAX_DECIMAL_DIGITS as PARSER_MAX_DECIMAL_DIGITS,
)
from tools.auto_research_issue_branch import (  # noqa: E402
    MAX_DECIMAL_EXPONENT as PARSER_MAX_DECIMAL_EXPONENT,
)
from tools.auto_research_issue_branch import (  # noqa: E402
    MAX_DECIMAL_TEXT_LENGTH as PARSER_MAX_DECIMAL_TEXT_LENGTH,
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


def test_local_decimal_bounds_match_the_parser() -> None:
    """조립 전 검증이 파서보다 느슨해지는 드리프트를 잡는다.

    한 축이라도 느슨하면 그 축의 극단값이 이슈 발행 후에야 거부된다.
    """
    assert MAX_DECIMAL_TEXT_LENGTH == PARSER_MAX_DECIMAL_TEXT_LENGTH
    assert MAX_DECIMAL_DIGITS == PARSER_MAX_DECIMAL_DIGITS
    assert MAX_DECIMAL_EXPONENT == PARSER_MAX_DECIMAL_EXPONENT


@pytest.mark.parametrize(
    "value",
    [
        "0." + "1" * 200,      # 길이 초과
        "0." + "1" * 70,       # 자릿수 초과
        "1e2000",              # 지수 초과
    ],
)
def test_parse_llm_fields_rejects_out_of_bound_decimals(value: str) -> None:
    """파서의 경계를 넘는 임계값도 조립 전에 끊는다."""
    with pytest.raises(ValueError):
        _fields(minimum_primary_delta=value)


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
```

- [ ] **Step 2: 실패를 확인한다**

Run: `uv run --no-sync python -m pytest tests/test_issue_authoring.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_orchestration.app.experiments.issue_authoring'`

- [ ] **Step 3: 모듈을 구현한다**

`agent_orchestration/app/experiments/issue_authoring.py`:

```python
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
```

- [ ] **Step 4: 테스트 통과를 확인한다**

Run: `uv run --no-sync python -m pytest tests/test_issue_authoring.py -v`
Expected: PASS

`LlmIssueFields`의 검증은 **파서가 거부할 값을 조립 전에 끊는 것**이 목적입니다. 여기서
막지 못한 값은 이슈가 발행된 뒤에야 워크플로에서 실패하고, 그때는 이미 GitHub에 이슈가
열려 있습니다. 그래서 guardrail 이름 정규식, 임계값 십진수, 값 안의 `### ` 줄을 모두
이 지점에서 검사합니다.

- [ ] **Step 5: lint**

Run: `uv run --no-sync ruff check agent_orchestration tests`
Expected: `All checks passed!`

- [ ] **Step 6: 커밋**

```bash
git add agent_orchestration/app/experiments/issue_authoring.py tests/test_issue_authoring.py
git commit -m "feat: 가설을 Issue Form 본문으로 조립하는 순수 함수 추가"
```

---

## Task 3: 설정 — `ORCH_GITHUB_*` / `ORCH_EXPERIMENT_*`

**Files:**
- Modify: `agent_orchestration/app/config.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `.claude/docs/agent-project-reference.md`
- Test: `tests/test_agent_orchestration.py`

**Interfaces:**
- Consumes: `ExperimentDefaults` (Task 2)
- Produces: `ServiceSettings.github_token`, `.github_repository`, `.gh_timeout_sec`,
  `.issue_daily_limit`, `.experiment_defaults`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_agent_orchestration.py` 끝에 추가합니다.

```python
def test_issue_publication_settings_are_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """발행 경로가 요구하는 설정이 누락되면 기동 시점에 드러나야 한다."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ORCH_GITHUB_TOKEN", "x" * 40)
    monkeypatch.setenv("ORCH_GITHUB_REPOSITORY", "SKYAHO/Autoresearch")
    monkeypatch.setenv("ORCH_EXPERIMENT_DATASET_SNAPSHOT", "bq://a/b@2026-07-31")
    monkeypatch.setenv("ORCH_EXPERIMENT_TRAINING_CONFIG_REF", "configs/train/x.yaml@abc")
    monkeypatch.setenv("ORCH_EXPERIMENT_DATASET_WINDOW", "- 데이터셋 / 경로: data/train.csv")

    settings = load_settings()

    assert settings.github_repository == "SKYAHO/Autoresearch"
    assert settings.gh_timeout_sec == 30
    assert settings.issue_daily_limit == 20
    assert settings.experiment_defaults.dataset_snapshot == "bq://a/b@2026-07-31"


def test_github_repository_must_be_owner_slash_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """발행 대상 저장소를 잘못 두면 다른 저장소에 이슈가 열린다."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("ORCH_GITHUB_TOKEN", "x" * 40)
    monkeypatch.setenv("ORCH_GITHUB_REPOSITORY", "Autoresearch")
    monkeypatch.setenv("ORCH_EXPERIMENT_DATASET_SNAPSHOT", "bq://a/b@2026-07-31")
    monkeypatch.setenv("ORCH_EXPERIMENT_TRAINING_CONFIG_REF", "configs/train/x.yaml@abc")
    monkeypatch.setenv("ORCH_EXPERIMENT_DATASET_WINDOW", "- 데이터셋 / 경로: data/train.csv")

    with pytest.raises(ValueError, match="ORCH_GITHUB_REPOSITORY"):
        load_settings()
```

`_set_required_env`가 없으면 이 파일에서 기존 테스트가 필수 환경 변수를 어떻게 세팅하는지
확인해 같은 방식으로 helper를 추가하십시오(`tests/test_agent_orchestration.py:32` 인근의
필수 변수 목록 참조).

- [ ] **Step 2: 실패를 확인한다**

Run: `uv run --no-sync python -m pytest tests/test_agent_orchestration.py -k issue_publication -v`
Expected: FAIL — `AttributeError: 'ServiceSettings' object has no attribute 'github_repository'`

- [ ] **Step 3: 설정을 추가한다**

`config.py`의 `ServiceSettings` dataclass에 필드를 더합니다.

```python
    github_token: str
    github_repository: str
    gh_timeout_sec: int
    issue_daily_limit: int
    experiment_defaults: ExperimentDefaults
```

`load_settings()`의 `return ServiceSettings(` 직전에 추가합니다.

```python
    github_token = _require_env("ORCH_GITHUB_TOKEN", os.getenv("ORCH_GITHUB_TOKEN"))
    github_repository = _require_env(
        "ORCH_GITHUB_REPOSITORY", os.getenv("ORCH_GITHUB_REPOSITORY")
    )
    # `gh issue create`의 결과 URL을 이 값과 대조해 다른 저장소에 열린 이슈를 거부한다.
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", github_repository):
        raise ValueError("ORCH_GITHUB_REPOSITORY must be 'owner/repo'.")
    gh_timeout_sec = _positive_env_int("ORCH_GH_TIMEOUT_SEC", 30)
    issue_daily_limit = _positive_env_int("ORCH_ISSUE_DAILY_LIMIT", 20)
    experiment_defaults = ExperimentDefaults(
        dataset_snapshot=_require_env(
            "ORCH_EXPERIMENT_DATASET_SNAPSHOT",
            os.getenv("ORCH_EXPERIMENT_DATASET_SNAPSHOT"),
        ),
        training_config_ref=_require_env(
            "ORCH_EXPERIMENT_TRAINING_CONFIG_REF",
            os.getenv("ORCH_EXPERIMENT_TRAINING_CONFIG_REF"),
        ),
        dataset_window=_require_env(
            "ORCH_EXPERIMENT_DATASET_WINDOW",
            os.getenv("ORCH_EXPERIMENT_DATASET_WINDOW"),
        ),
    )
```

`return ServiceSettings(...)`에 위 다섯 값을 전달하고, 파일 상단에
`from agent_orchestration.app.experiments.issue_authoring import ExperimentDefaults`를
추가합니다.

- [ ] **Step 4: `.env.example`에 기재한다**

`ORCH_INTERACTIONS_TABLE` 아래에 추가합니다.

```dotenv
# --- Auto Research 이슈 발행 (#516) ---
# `issues: write` 전용 GitHub 자격입니다. repo 스코프 토큰을 넣지 마십시오.
# gh CLI에 GH_TOKEN으로 전달됩니다.
ORCH_GITHUB_TOKEN=
# 발행 대상 저장소. gh가 돌려준 이슈 URL을 이 값과 대조합니다.
ORCH_GITHUB_REPOSITORY=SKYAHO/Autoresearch
# gh 서브프로세스 실행 상한(초).
ORCH_GH_TIMEOUT_SEC=30
# 일일 발행 상한. 초과하면 429를 반환합니다.
ORCH_ISSUE_DAILY_LIMIT=20
# 서버가 Issue Form에 채우는 환경 의존 값 3개입니다. 나머지 실행 설정(비교 대상,
# 시드, split 비율, 스냅샷 재사용)은 정책 상수라 여기서 조정하지 않습니다.
ORCH_EXPERIMENT_DATASET_SNAPSHOT=
ORCH_EXPERIMENT_TRAINING_CONFIG_REF=
ORCH_EXPERIMENT_DATASET_WINDOW=
```

- [ ] **Step 5: 테스트 통과를 확인한다**

Run: `uv run --no-sync python -m pytest tests/test_agent_orchestration.py -q`
Expected: PASS

기존 테스트가 필수 변수 누락으로 깨지면 그 테스트의 환경 설정 helper에 새 변수를
더하십시오.

- [ ] **Step 6: 문서를 갱신한다**

`README.md`와 `.claude/docs/agent-project-reference.md`에서 `agent_orchestration`을
서술한 절에 새 환경 변수 6개와 그 용도를 한 줄씩 추가합니다. `CLAUDE.md` Core Rules가
같은 PR에서의 갱신을 요구합니다.

- [ ] **Step 7: 커밋**

```bash
git add agent_orchestration/app/config.py .env.example README.md \
        .claude/docs/agent-project-reference.md tests/test_agent_orchestration.py
git commit -m "feat: 이슈 발행 경로 설정과 서버 소유 기본값 추가"
```

---

## Task 4: `gh` 경계 — `github_issues.py`

**Files:**
- Create: `agent_orchestration/app/experiments/github_issues.py`
- Modify: `agent_orchestration/app/experiments/exceptions.py`
- Test: `tests/test_github_issues.py` (신규)

**Interfaces:**
- Consumes: `ServiceSettings.github_token`, `.github_repository`, `.gh_timeout_sec` (Task 3)
- Produces:
  - `class IssueRef(BaseModel)` — `number: int`, `url: str`
  - `class GitHubIssueError(RuntimeError)` — `.reason: str`
  - `async def create_issue(settings, *, title, body, labels) -> IssueRef`
  - `async def find_issue_by_marker(settings, *, marker) -> IssueRef | None`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_github_issues.py`:

```python
"""gh CLI 경계의 출력 파싱·저장소 검증·오류 분류를 고정한다.

전체 파이프라인에서 조립된 본문이 GitHub 이슈가 되는 구간만 검증한다. 본문 조립과 DB
저장은 이 모듈의 범위가 아니다. 실제 gh를 실행하지 않고 서브프로세스를 스텁으로 대체한다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from agent_orchestration.app.experiments.github_issues import (
    GitHubIssueError,
    IssueRef,
    create_issue,
    find_issue_by_marker,
)


@dataclass(frozen=True)
class _Settings:
    github_token: str = "x" * 40
    github_repository: str = "SKYAHO/Autoresearch"
    gh_timeout_sec: int = 5


class _FakeProcess:
    def __init__(self, stdout: bytes, stderr: bytes, returncode: int) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.pid = 4242

    async def communicate(self, _stdin: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def _patch_subprocess(monkeypatch: pytest.MonkeyPatch, process: _FakeProcess) -> list:
    calls: list = []

    async def fake_exec(*command: str, **kwargs: object) -> _FakeProcess:
        calls.append(command)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return calls


def test_create_issue_parses_the_issue_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """gh issue create에는 --json이 없어 stdout URL을 파싱해야 한다."""
    _patch_subprocess(
        monkeypatch,
        _FakeProcess(b"https://github.com/SKYAHO/Autoresearch/issues/520\n", b"", 0),
    )

    ref = asyncio.run(
        create_issue(_Settings(), title="[AR] t", body="b", labels=("auto-experiment",))
    )

    assert ref == IssueRef(number=520, url="https://github.com/SKYAHO/Autoresearch/issues/520")


def test_create_issue_rejects_a_url_from_another_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """설정과 다른 저장소에 열린 이슈를 성공으로 기록하면 안 된다."""
    _patch_subprocess(
        monkeypatch,
        _FakeProcess(b"https://github.com/other/repo/issues/1\n", b"", 0),
    )

    with pytest.raises(GitHubIssueError, match="unexpected_repository"):
        asyncio.run(
            create_issue(_Settings(), title="[AR] t", body="b", labels=("auto-experiment",))
        )


def test_create_issue_passes_the_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """label이 빠지면 워크플로가 실패가 아니라 skip되어 흔적이 남지 않는다."""
    calls = _patch_subprocess(
        monkeypatch,
        _FakeProcess(b"https://github.com/SKYAHO/Autoresearch/issues/520\n", b"", 0),
    )

    asyncio.run(
        create_issue(_Settings(), title="[AR] t", body="b", labels=("auto-experiment",))
    )

    assert "--label" in calls[0]
    assert "auto-experiment" in calls[0]


def test_create_issue_classifies_authentication_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """사유를 분류해야 호출자가 무엇을 고쳐야 하는지 알 수 있다."""
    _patch_subprocess(
        monkeypatch, _FakeProcess(b"", b"gh: Bad credentials (HTTP 401)\n", 1)
    )

    with pytest.raises(GitHubIssueError, match="authentication_failed"):
        asyncio.run(
            create_issue(_Settings(), title="[AR] t", body="b", labels=("auto-experiment",))
        )


def test_create_issue_classifies_unknown_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """분류하지 못한 실패도 삼키지 않는다."""
    _patch_subprocess(monkeypatch, _FakeProcess(b"", b"something odd\n", 1))

    with pytest.raises(GitHubIssueError, match="unclassified"):
        asyncio.run(
            create_issue(_Settings(), title="[AR] t", body="b", labels=("auto-experiment",))
        )


def test_create_issue_separates_rate_limit_from_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitHub은 rate limit도 403으로 답한다. 둘을 묶으면 호출자가 오해한다.

    영구적 권한 문제를 `rate_limited`로 알리면 "기다리면 풀린다"로 읽힌다.
    """
    _patch_subprocess(
        monkeypatch,
        _FakeProcess(b"", b"gh: API rate limit exceeded (HTTP 403)\n", 1),
    )
    with pytest.raises(GitHubIssueError, match="rate_limited"):
        asyncio.run(
            create_issue(_Settings(), title="[AR] t", body="b", labels=("auto-experiment",))
        )

    _patch_subprocess(
        monkeypatch,
        _FakeProcess(b"", b"gh: Resource not accessible by integration (HTTP 403)\n", 1),
    )
    with pytest.raises(GitHubIssueError, match="permission_denied"):
        asyncio.run(
            create_issue(_Settings(), title="[AR] t", body="b", labels=("auto-experiment",))
        )


def test_cancellation_reclaims_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """상위 취소 시 `gh` 프로세스를 회수해야 한다.

    회수하지 않으면 shield된 task가 참조 없이 남고, 임시 디렉터리가 실행 중인 `gh`보다
    먼저 지워진다.
    """
    reclaimed: list[object] = []

    class _HangingProcess:
        returncode = None
        pid = 4242

        async def communicate(self, _stdin: bytes | None = None) -> tuple[bytes, bytes]:
            await asyncio.sleep(3600)
            return b"", b""

    process = _HangingProcess()
    _patch_subprocess(monkeypatch, process)
    monkeypatch.setattr(
        "agent_orchestration.app.experiments.github_issues._terminate_process_group",
        reclaimed.append,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            create_issue(
                _Settings(), title="[AR] t", body="b", labels=("auto-experiment",)
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert reclaimed == [process]


def test_find_issue_by_marker_returns_none_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """marker가 없으면 아직 발행되지 않은 것이다."""
    _patch_subprocess(monkeypatch, _FakeProcess(b"[]\n", b"", 0))

    found = asyncio.run(find_issue_by_marker(_Settings(), marker="<!-- experiment-id: x -->"))

    assert found is None


def test_find_issue_by_marker_returns_the_existing_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """발행 후 DB 쓰기가 실패한 경우의 복구 경로다."""
    _patch_subprocess(
        monkeypatch,
        _FakeProcess(
            b'[{"number": 520, "url": "https://github.com/SKYAHO/Autoresearch/issues/520"}]',
            b"",
            0,
        ),
    )

    found = asyncio.run(find_issue_by_marker(_Settings(), marker="<!-- experiment-id: x -->"))

    assert found == IssueRef(number=520, url="https://github.com/SKYAHO/Autoresearch/issues/520")


def test_token_is_not_passed_as_a_command_argument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """토큰이 명령행에 실리면 프로세스 목록에 노출된다."""
    calls = _patch_subprocess(
        monkeypatch,
        _FakeProcess(b"https://github.com/SKYAHO/Autoresearch/issues/520\n", b"", 0),
    )

    asyncio.run(
        create_issue(_Settings(), title="[AR] t", body="b", labels=("auto-experiment",))
    )

    assert not any("x" * 40 in argument for argument in calls[0])
```

- [ ] **Step 2: 실패를 확인한다**

Run: `uv run --no-sync python -m pytest tests/test_github_issues.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: 오류 타입을 추가한다**

`exceptions.py` 끝에 추가합니다.

```python
class IssuePublicationLimitError(RuntimeError):
    """일일 발행 상한을 넘었다."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"Daily issue publication limit {limit} was reached.")
```

- [ ] **Step 4: 모듈을 구현한다**

`agent_orchestration/app/experiments/github_issues.py`:

```python
"""Auto Research 이슈 발행의 GitHub CLI 경계.

[파이프라인]
조립된 Issue Form 본문이 실제 GitHub 이슈가 되는 구간을 담당한다. 본문 조립은
issue_authoring, DB 기록과 멱등성 판단은 service의 책임이다.

[기능]
`gh issue create`/`gh issue list`를 요청별 임시 홈에서 실행하고, 성공 시 stdout의 이슈
URL을 설정된 저장소와 대조해 파싱하며, 실패 사유를 분류해 올린다. 시간 초과 시 프로세스
그룹을 회수한다.

[비책임]
자격 증명의 발급·보관(Autoresearch-infra), 재시도 판단(service), 이슈 본문의 내용.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from tempfile import TemporaryDirectory
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class _Settings(Protocol):
    github_token: str
    github_repository: str
    gh_timeout_sec: int


class IssueRef(BaseModel):
    """발행되었거나 이미 존재하는 이슈의 좌표."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    number: int
    url: str


class GitHubIssueError(RuntimeError):
    """`gh` 호출이 실패했거나 결과를 신뢰할 수 없다."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


# stderr 문자열 기반 분류다. gh 버전을 이미지에 고정해야 조용히 깨지지 않는다.
# 순서가 의미를 갖는다 — GitHub은 rate limit도 HTTP 403으로 응답하므로 rate limit을
# 먼저 본다. 403을 통째로 rate_limited로 묶으면 토큰 스코프 부족·SAML 미인가 같은
# **영구적** 권한 문제를 "기다리면 풀린다"로 잘못 알리게 된다.
_REASON_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"bad credentials|authentication|HTTP 401", "authentication_failed"),
    (r"rate limit|api rate", "rate_limited"),
    (
        r"HTTP 403|forbidden|resource not accessible|saml",
        "permission_denied",
    ),
    (r"could not add label|not found.*label|label.*not found", "label_missing"),
    (r"HTTP 404|could not resolve to a Repository", "repository_not_found"),
    (r"dial tcp|connection refused|timeout|network", "network_error"),
)


def _environment(token: str, home: str) -> dict[str, str]:
    """`gh`가 필요로 하는 최소 환경만 하위 프로세스에 전달한다."""
    # 토큰은 명령행이 아니라 환경으로만 넘긴다 — 명령행은 프로세스 목록에 노출된다.
    return {
        "GH_TOKEN": token,
        "GH_CONFIG_DIR": home,
        "HOME": home,
        "TMPDIR": home,
        "PATH": os.environ.get("PATH", ""),
        "GH_NO_UPDATE_NOTIFIER": "1",
        "GH_PROMPT_DISABLED": "1",
    }


def _classify(stderr: str) -> str:
    for pattern, reason in _REASON_PATTERNS:
        if re.search(pattern, stderr, re.IGNORECASE):
            return reason
    return "unclassified"


def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """`gh`와 같은 세션의 하위 프로세스를 함께 종료한다.

    `agent_orchestration/codex.py`의 같은 이름 함수와 동일한 계약이다. 비-POSIX에는
    프로세스 그룹이 없으므로 `process.kill()`로 떨어진다 — 이 fallback이 없으면
    아래 회수 단계가 무기한 대기해 `gh_timeout_sec`이 무의미해진다.
    """
    if process.returncode is not None:
        return
    if os.name == "posix" and process.pid is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
    process.kill()


async def _terminate_and_wait(
    process: asyncio.subprocess.Process,
    communicate_task: asyncio.Task[tuple[bytes, bytes]],
) -> None:
    """프로세스 그룹 종료 뒤 파이프를 닫고 하위 프로세스를 회수한다."""
    _terminate_process_group(process)
    try:
        await asyncio.wait_for(asyncio.shield(communicate_task), timeout=5)
    except (OSError, TimeoutError):
        communicate_task.cancel()
        await asyncio.gather(communicate_task, return_exceptions=True)


async def _run_gh(settings: _Settings, arguments: tuple[str, ...]) -> str:
    """`gh`를 격리 실행하고 stdout을 반환한다."""
    with TemporaryDirectory(prefix="agent-orchestration-gh-") as home:
        try:
            process = await asyncio.create_subprocess_exec(
                "gh",
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_environment(settings.github_token, home),
                start_new_session=True,
            )
        except OSError as error:
            raise GitHubIssueError("gh_unavailable", str(error)) from error

        communicate_task = asyncio.create_task(process.communicate())
        try:
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communicate_task), timeout=settings.gh_timeout_sec
            )
        except TimeoutError as error:
            await _terminate_and_wait(process, communicate_task)
            raise GitHubIssueError("timeout") from error
        except asyncio.CancelledError:
            # 상위가 취소되면(클라이언트 연결 종료·서비스 종료) shield된 task가 참조
            # 없이 남고, 아래 `with TemporaryDirectory`가 언와인드되며 `gh`가 아직
            # 쓰고 있는 HOME/GH_CONFIG_DIR/TMPDIR을 지워버린다. codex.py와 같이
            # 회수한 뒤 올린다.
            await _terminate_and_wait(process, communicate_task)
            raise

        if process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip()
            raise GitHubIssueError(_classify(message), message)
        return stdout.decode("utf-8", errors="replace").strip()


def _parse_issue_url(url: str, repository: str) -> IssueRef:
    """URL이 설정된 저장소를 가리킬 때만 이슈 번호를 인정한다."""
    match = re.fullmatch(
        r"https://github\.com/([^/]+/[^/]+)/issues/([1-9][0-9]*)", url.strip()
    )
    if match is None:
        raise GitHubIssueError("unparsable_output", url)
    if match.group(1) != repository:
        raise GitHubIssueError("unexpected_repository", url)
    return IssueRef(number=int(match.group(2)), url=url.strip())


async def create_issue(
    settings: _Settings,
    *,
    title: str,
    body: str,
    labels: tuple[str, ...],
) -> IssueRef:
    """본문과 label로 이슈를 발행하고 그 좌표를 반환한다."""
    with TemporaryDirectory(prefix="agent-orchestration-body-") as workdir:
        body_path = os.path.join(workdir, "body.md")
        with open(body_path, "w", encoding="utf-8") as handle:
            handle.write(body)
        arguments = [
            "issue",
            "create",
            "--repo",
            settings.github_repository,
            "--title",
            title,
            "--body-file",
            body_path,
        ]
        for label in labels:
            arguments.extend(["--label", label])
        stdout = await _run_gh(settings, tuple(arguments))
    return _parse_issue_url(stdout.splitlines()[-1] if stdout else "", settings.github_repository)


async def find_issue_by_marker(settings: _Settings, *, marker: str) -> IssueRef | None:
    """본문 marker로 이미 발행된 이슈를 찾는다(발행 후 DB 쓰기 실패 복구용)."""
    stdout = await _run_gh(
        settings,
        (
            "issue",
            "list",
            "--repo",
            settings.github_repository,
            "--state",
            "all",
            "--search",
            marker,
            "--json",
            "number,url",
            "--limit",
            "5",
        ),
    )
    try:
        rows = json.loads(stdout or "[]")
    except json.JSONDecodeError as error:
        raise GitHubIssueError("unparsable_output", stdout) from error
    if not rows:
        return None
    return _parse_issue_url(str(rows[0]["url"]), settings.github_repository)
```

- [ ] **Step 5: 테스트 통과를 확인한다**

Run: `uv run --no-sync python -m pytest tests/test_github_issues.py -v`
Expected: PASS (10 passed)

- [ ] **Step 6: lint 후 커밋**

```bash
uv run --no-sync ruff check agent_orchestration tests
git add agent_orchestration/app/experiments/github_issues.py \
        agent_orchestration/app/experiments/exceptions.py \
        tests/test_github_issues.py
git commit -m "feat: gh CLI 기반 이슈 발행 경계 추가"
```

---

## Task 5: 서비스 — 생성→저장→발행 2단계와 멱등성

**Files:**
- Modify: `agent_orchestration/app/experiments/service.py`
- Modify: `agent_orchestration/app/experiments/schemas.py`
- Test: `tests/test_experiment_issue_publication.py` (신규)

**Interfaces:**
- Consumes: `build_prompt`, `parse_llm_fields`, `build_issue_body`, `build_issue_title`,
  `marker_for` (Task 2); `create_issue`, `find_issue_by_marker`, `IssueRef`,
  `GitHubIssueError` (Task 4); `ServiceSettings` (Task 3)
- Produces:
  - `class IssuePublicationRequest(BaseModel)` — `allowed_scope: tuple[str, ...] = ()`,
    `regenerate: bool = False`
  - `class IssuePublicationResponse(BaseModel)` — `issue_number: int`,
    `issue_url: str`, `issue_branch: str`
  - `async def publish_experiment_issue(session, settings, experiment_id, request, *, generate) -> Experiment`

`generate`는 프롬프트를 받아 LLM 응답 텍스트를 돌려주는 awaitable입니다. 테스트가 이
지점을 스텁으로 대체해 **재호출이 LLM을 다시 부르지 않음**을 검증합니다.

- [ ] **Step 1: 요청·응답 스키마를 추가한다**

`schemas.py` 끝에 추가합니다.

```python
class IssuePublicationRequest(BaseModel):
    """가설을 `[AR]` 이슈로 발행하는 요청."""

    model_config = ConfigDict(extra="forbid")

    allowed_scope: tuple[
        Literal["prod_model_contract", "feast_definition", "promotion"], ...
    ] = ()
    # 저장된 본문이 파서를 통과하지 못해 고착됐을 때만 쓴다. issue_number가 이미 있으면
    # 무시된다 — 발행된 이슈의 본문을 바꾸는 것은 이 endpoint의 책임이 아니다.
    regenerate: bool = False


class IssuePublicationResponse(BaseModel):
    """발행 결과 좌표."""

    model_config = ConfigDict(extra="forbid")

    issue_number: int
    issue_url: str
    issue_branch: str
```

- [ ] **Step 2: 실패하는 테스트를 쓴다**

`tests/test_experiment_issue_publication.py`:

```python
"""가설이 `[AR]` 이슈가 되는 2단계 절차와 멱등성을 고정한다.

전체 파이프라인에서 본문 생성·저장과 발행 사이의 순서·재시도 의미만 검증한다. 본문
형식은 test_issue_authoring, gh 호출은 test_github_issues가 담당한다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
import json
import uuid

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from agent_orchestration.app.database import Base
from agent_orchestration.app.experiments.exceptions import IssuePublicationLimitError
from agent_orchestration.app.experiments.github_issues import GitHubIssueError, IssueRef
from agent_orchestration.app.experiments.issue_authoring import ExperimentDefaults
from agent_orchestration.app.experiments.schemas import (
    ExperimentCreate,
    IssuePublicationRequest,
)
from agent_orchestration.app.experiments.service import (
    create_experiment,
    publish_experiment_issue,
)

LLM_RESPONSE = json.dumps(
    {
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
    },
    ensure_ascii=False,
)


@dataclass(frozen=True)
class _Settings:
    github_token: str = "x" * 40
    github_repository: str = "SKYAHO/Autoresearch"
    gh_timeout_sec: int = 5
    issue_daily_limit: int = 20
    experiment_defaults: ExperimentDefaults = ExperimentDefaults(
        dataset_snapshot="bq://autoresearch/train@2026-07-31",
        training_config_ref="configs/train/lgbm-v1.yaml@abc1234",
        dataset_window="- 데이터셋 / 경로: data/train.csv",
    )


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine: Engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def register_uuid_function(dbapi_connection, _record) -> None:
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


class _Recorder:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return LLM_RESPONSE


def test_publication_stores_body_before_creating_the_issue(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """본문이 발행 전에 커밋되어야 재시도가 결정론적이다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="ratio"))
    recorder = _Recorder()
    seen: list[str] = []

    async def fake_create_issue(_settings, *, title, body, labels):
        seen.append(body)
        return IssueRef(number=520, url="https://github.com/SKYAHO/Autoresearch/issues/520")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", fake_create_issue
    )

    result = asyncio.run(
        publish_experiment_issue(
            db_session,
            _Settings(),
            experiment.id,
            IssuePublicationRequest(),
            generate=recorder.generate,
        )
    )

    assert result.issue_number == 520
    assert result.issue_body == seen[0]
    assert result.issue_branch.startswith("exp/520-")


def test_retry_after_publish_failure_reuses_the_stored_body(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM은 비결정적이라 재생성하면 실험 정의가 바뀐다. 재호출은 같은 본문을 써야 한다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="ratio"))
    recorder = _Recorder()
    attempts: list[str] = []

    async def failing_create_issue(_settings, *, title, body, labels):
        attempts.append(body)
        raise GitHubIssueError("network_error")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", failing_create_issue
    )
    with pytest.raises(GitHubIssueError):
        asyncio.run(
            publish_experiment_issue(
                db_session, _Settings(), experiment.id,
                IssuePublicationRequest(), generate=recorder.generate,
            )
        )

    async def succeeding_create_issue(_settings, *, title, body, labels):
        attempts.append(body)
        return IssueRef(number=521, url="https://github.com/SKYAHO/Autoresearch/issues/521")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", succeeding_create_issue
    )
    asyncio.run(
        publish_experiment_issue(
            db_session, _Settings(), experiment.id,
            IssuePublicationRequest(), generate=recorder.generate,
        )
    )

    assert len(recorder.prompts) == 1, "LLM을 다시 부르면 안 된다"
    assert attempts[0] == attempts[1], "같은 본문으로 재발행해야 한다"


def test_second_call_after_success_does_not_publish_again(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """멱등성 1차 방어 — issue_number가 있으면 발행하지 않는다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="ratio"))
    recorder = _Recorder()
    calls = 0

    async def fake_create_issue(_settings, *, title, body, labels):
        nonlocal calls
        calls += 1
        return IssueRef(number=520, url="https://github.com/SKYAHO/Autoresearch/issues/520")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", fake_create_issue
    )
    for _ in range(2):
        asyncio.run(
            publish_experiment_issue(
                db_session, _Settings(), experiment.id,
                IssuePublicationRequest(), generate=recorder.generate,
            )
        )

    assert calls == 1


def test_regenerate_replaces_the_body_before_publication(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """저장된 본문이 파서를 통과하지 못하면 고착되므로 풀 수단이 필요하다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="ratio"))
    recorder = _Recorder()

    async def failing_create_issue(_settings, *, title, body, labels):
        raise GitHubIssueError("network_error")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", failing_create_issue
    )
    with pytest.raises(GitHubIssueError):
        asyncio.run(
            publish_experiment_issue(
                db_session, _Settings(), experiment.id,
                IssuePublicationRequest(), generate=recorder.generate,
            )
        )

    async def succeeding_create_issue(_settings, *, title, body, labels):
        return IssueRef(number=522, url="https://github.com/SKYAHO/Autoresearch/issues/522")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", succeeding_create_issue
    )
    asyncio.run(
        publish_experiment_issue(
            db_session, _Settings(), experiment.id,
            IssuePublicationRequest(regenerate=True), generate=recorder.generate,
        )
    )

    assert len(recorder.prompts) == 2


def test_regenerate_is_ignored_after_publication(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """발행된 이슈의 본문을 바꾸는 것은 이 endpoint의 책임이 아니다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="ratio"))
    recorder = _Recorder()

    async def fake_create_issue(_settings, *, title, body, labels):
        return IssueRef(number=520, url="https://github.com/SKYAHO/Autoresearch/issues/520")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", fake_create_issue
    )
    asyncio.run(
        publish_experiment_issue(
            db_session, _Settings(), experiment.id,
            IssuePublicationRequest(), generate=recorder.generate,
        )
    )
    asyncio.run(
        publish_experiment_issue(
            db_session, _Settings(), experiment.id,
            IssuePublicationRequest(regenerate=True), generate=recorder.generate,
        )
    )

    assert len(recorder.prompts) == 1


def test_daily_limit_blocks_publication(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """호출 주체가 생겼으므로 폭주 방지를 여기에 둔다(#490 결정)."""
    recorder = _Recorder()

    async def fake_create_issue(_settings, *, title, body, labels):
        return IssueRef(number=520, url="https://github.com/SKYAHO/Autoresearch/issues/520")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", fake_create_issue
    )
    first = create_experiment(db_session, ExperimentCreate(hypothesis="one"))
    asyncio.run(
        publish_experiment_issue(
            db_session, _Settings(issue_daily_limit=1), first.id,
            IssuePublicationRequest(), generate=recorder.generate,
        )
    )

    second = create_experiment(db_session, ExperimentCreate(hypothesis="two"))
    with pytest.raises(IssuePublicationLimitError):
        asyncio.run(
            publish_experiment_issue(
                db_session, _Settings(issue_daily_limit=1), second.id,
                IssuePublicationRequest(), generate=recorder.generate,
            )
        )


def test_marker_lookup_recovers_a_lost_publication(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gh는 성공했는데 응답이 소실된 경우 중복 이슈를 만들면 안 된다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="ratio"))
    recorder = _Recorder()

    async def fake_find(_settings, *, marker):
        return IssueRef(number=530, url="https://github.com/SKYAHO/Autoresearch/issues/530")

    async def unexpected_create(_settings, *, title, body, labels):
        raise AssertionError("이미 발행된 이슈를 다시 만들면 안 된다")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.find_issue_by_marker", fake_find
    )
    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", unexpected_create
    )

    result = asyncio.run(
        publish_experiment_issue(
            db_session, _Settings(), experiment.id,
            IssuePublicationRequest(), generate=recorder.generate,
        )
    )

    assert result.issue_number == 530
```

- [ ] **Step 3: 실패를 확인한다**

Run: `uv run --no-sync python -m pytest tests/test_experiment_issue_publication.py -q`
Expected: FAIL — `ImportError: cannot import name 'publish_experiment_issue'`

- [ ] **Step 4: 서비스를 구현한다**

`service.py`에 import를 추가합니다.

```python
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from agent_orchestration.app.experiments.exceptions import IssuePublicationLimitError
from agent_orchestration.app.experiments.github_issues import (
    create_issue,
    find_issue_by_marker,
)
from agent_orchestration.app.experiments.issue_authoring import (
    build_issue_body,
    build_issue_title,
    build_prompt,
    marker_for,
    parse_llm_fields,
)
from agent_orchestration.app.experiments.schemas import IssuePublicationRequest
```

파일 끝에 추가합니다.

```python
TRIGGER_LABEL = "auto-experiment"


def _branch_name_for(issue_number: int, title: str) -> str:
    """워크플로가 만들 브랜치 이름을 응답에 미리 싣는다.

    `tools/auto_research_issue_branch.py`의 `branch_name_for()`와 같은 규칙이다. 그
    모듈은 API 이미지에 없어 import할 수 없으므로 규칙을 복제한다 — 이 값은 표시용이며
    실제 브랜치는 워크플로가 만든다.
    """
    stripped = re.sub(r"^\s*\[AR\]\s*", "", title, flags=re.IGNORECASE)
    # 정본(`branch_name_for`)은 prefix를 떼고 남은 것이 공백뿐이면 거부한다. 이 가드가
    # 없으면 그럴듯한 브랜치 이름을 만들어 내며 정본과 갈린다.
    if not stripped.strip():
        raise ValueError("issue title must not be empty after the prefix")
    slug = re.sub(r"[^a-z0-9]+", "-", stripped.lower()).strip("-")
    if not slug:
        slug = "issue-" + hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:12]
    return f"exp/{issue_number}-{slug}"


async def publish_experiment_issue(
    session: Session,
    settings: object,
    experiment_id: uuid.UUID,
    request: IssuePublicationRequest,
    *,
    generate: Callable[[str], Awaitable[str]],
) -> Experiment:
    """가설을 `[AR]` 이슈로 발행하고 lineage를 기록한다."""
    experiment = find_experiment(session, experiment_id)
    if experiment is None:
        raise ExperimentNotFoundError(experiment_id)

    # 멱등성 1차 — 이미 발행됐으면 아무것도 하지 않는다. regenerate보다 우선한다.
    if experiment.issue_number is not None:
        return experiment

    # `updated_at`으로 세면 안 된다 — `onupdate=func.now()`라 상태 전이·metric 기록 등
    # 발행과 무관한 UPDATE도 갱신하므로, 며칠 전 발행된 실험이 오늘 수정되면 "오늘
    # 발행"으로 잡혀 새 발행을 부당하게 막는다. 발행 시각 전용 컬럼을 쓴다.
    since = datetime.now(UTC) - timedelta(days=1)
    published_today = session.scalar(
        select(func.count())
        .select_from(Experiment)
        .where(Experiment.issue_published_at >= since)
    )
    if (published_today or 0) >= settings.issue_daily_limit:
        raise IssuePublicationLimitError(settings.issue_daily_limit)

    # ① 본문을 만들고 발행 전에 커밋한다. 이 커밋이 재시도 결정성의 근거다.
    if experiment.issue_body is None or request.regenerate:
        response = await generate(build_prompt(experiment.hypothesis))
        fields = parse_llm_fields(response)
        body = build_issue_body(
            experiment.id,
            fields,
            settings.experiment_defaults,
            allowed_scope=request.allowed_scope,
        )
        title = build_issue_title(fields)
        with session.begin():
            experiment.issue_body = body
            experiment.issue_branch = None
            session.add(experiment)
        session.refresh(experiment)
    else:
        body = experiment.issue_body
        title = _title_from_body(body, experiment.hypothesis)

    # ② 발행. gh 성공 후 응답이 소실된 경우를 위해 marker를 먼저 조회한다.
    existing = await find_issue_by_marker(settings, marker=marker_for(experiment.id))
    reference = existing or await create_issue(
        settings, title=title, body=body, labels=(TRIGGER_LABEL,)
    )

    experiment.issue_number = reference.number
    experiment.issue_branch = _branch_name_for(reference.number, title)
    experiment.issue_published_at = datetime.now(UTC)
    session.add(experiment)
    session.commit()
    return experiment


def _title_from_body(body: str, fallback: str) -> str:
    """저장된 본문으로 재발행할 때 제목을 복원한다."""
    match = re.search(r"^### 연구 가설\n(.+)$", body, re.MULTILINE)
    return f"[AR] {match.group(1).strip() if match else fallback.strip()}"
```

`service.py` 상단에 `import hashlib`, `import re`가 없으면 추가하고, `select`와 `func`가
import돼 있는지 확인하십시오.

- [ ] **Step 5: 브랜치 이름 규칙의 드리프트를 막는 테스트를 더한다**

`_branch_name_for`는 `tools/auto_research_issue_branch.py`의 `branch_name_for()`를
복제한 것입니다. 이 값은 응답 표시용이고 실제 브랜치는 워크플로가 만들지만, 어긋나면
UI가 존재하지 않는 브랜치 링크를 보여줍니다. 런타임은 `tools/`를 import할 수 없어도
테스트는 할 수 있으므로 여기서 고정합니다.

`tests/test_experiment_issue_publication.py`에 추가합니다.

```python
@pytest.mark.parametrize(
    "title",
    [
        "[AR] views per day ratio feature",
        "[AR] 비율 피처 실험",
        "no prefix ascii title",
        "[AR]    공백만    ",
    ],
)
def test_branch_name_matches_the_workflow_rule(title: str) -> None:
    """표시용 브랜치 이름이 워크플로가 만들 이름과 같아야 한다."""
    from agent_orchestration.app.experiments.service import _branch_name_for
    from tools.auto_research_issue_branch import branch_name_for

    assert _branch_name_for(520, title) == branch_name_for(520, title)
```

이 테스트가 실패하면 `_branch_name_for`를 `branch_name_for()`의 구현에 맞추십시오 —
정본은 `tools/` 쪽입니다.

- [ ] **Step 6: 테스트 통과를 확인한다**

Run: `uv run --no-sync python -m pytest tests/test_experiment_issue_publication.py -v`
Expected: PASS (7 + 4 passed)

`_title_from_body`가 만든 제목에 ASCII 조각이 없어 `_branch_name_for`가 해시 slug를
내면 `test_publication_stores_body_before_creating_the_issue`의
`startswith("exp/520-")` 단언은 여전히 통과합니다.

- [ ] **Step 7: lint 후 커밋**

```bash
uv run --no-sync ruff check agent_orchestration tests
git add agent_orchestration/app/experiments/service.py \
        agent_orchestration/app/experiments/schemas.py \
        tests/test_experiment_issue_publication.py
git commit -m "feat: 본문 저장 후 발행하는 2단계 이슈 발행 서비스 추가"
```

---

## Task 6: Endpoint — `POST /experiments/{id}/issue`

**Files:**
- Modify: `agent_orchestration/app/experiments/router.py`
- Modify: `agent_orchestration/app/main.py`
- Test: `tests/test_experiment_issue_endpoint.py` (신규)

**Interfaces:**
- Consumes: `publish_experiment_issue`, `IssuePublicationRequest`,
  `IssuePublicationResponse` (Task 5)
- Produces: `POST /experiments/{experiment_id}/issue` → `IssuePublicationResponse`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_experiment_issue_endpoint.py`:

```python
"""발행 endpoint의 HTTP 계약을 고정한다.

전체 파이프라인에서 발행 요청의 인증·상태 코드·응답 형태만 검증한다. 발행 절차 자체는
test_experiment_issue_publication이 담당한다.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_orchestration.app.experiments.exceptions import IssuePublicationLimitError


def test_publication_requires_the_orchestration_token(client: TestClient) -> None:
    """토큰 없이 이슈를 발행할 수 있으면 안 된다."""
    response = client.post(
        "/experiments/3f2a1c9d-8b7e-4a1f-9c2d-5e6f7a8b9c0d/issue", json={}
    )

    assert response.status_code == 401


def test_publication_returns_the_issue_coordinates(
    client: TestClient, authorized_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """응답에 이슈 번호·URL·브랜치가 실려야 UI가 링크를 만들 수 있다."""
    created = client.post(
        "/experiments", json={"hypothesis": "ratio"}, headers=authorized_headers
    ).json()

    response = client.post(
        f"/experiments/{created['id']}/issue",
        json={"allowed_scope": ["prod_model_contract"]},
        headers=authorized_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["issue_number"] > 0
    assert body["issue_branch"].startswith("exp/")


def test_daily_limit_maps_to_429(
    client: TestClient, authorized_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """상한 초과는 서버 오류가 아니라 호출자가 조절할 신호다."""
    async def raise_limit(*_args: object, **_kwargs: object) -> None:
        raise IssuePublicationLimitError(20)

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.router.publish_experiment_issue", raise_limit
    )
    created = client.post(
        "/experiments", json={"hypothesis": "ratio"}, headers=authorized_headers
    ).json()

    response = client.post(
        f"/experiments/{created['id']}/issue", json={}, headers=authorized_headers
    )

    assert response.status_code == 429
```

`client`와 `authorized_headers` fixture는 기존 `tests/test_experiment_api.py`(또는
동등한 API 테스트 파일)의 방식을 따라 만드십시오. `gh` 호출과 LLM은 그 fixture에서
스텁으로 대체합니다 — 이 테스트는 네트워크를 타면 안 됩니다.

- [ ] **Step 2: 실패를 확인한다**

Run: `uv run --no-sync python -m pytest tests/test_experiment_issue_endpoint.py -q`
Expected: FAIL — 404 (라우트 없음)

- [ ] **Step 3: 라우트를 추가한다**

`router.py` 끝에 추가합니다.

```python
@router.post(
    "/{experiment_id}/issue",
    response_model=IssuePublicationResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        **_UNAUTHORIZED_RESPONSE,
        **_NOT_FOUND_RESPONSE,
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "description": "Daily issue publication limit was reached.",
            "model": ErrorResponse,
        },
        status.HTTP_502_BAD_GATEWAY: {
            "description": "Failed to author or publish the issue.",
            "model": ErrorResponse,
        },
    },
)
async def post_experiment_issue(
    experiment_id: uuid.UUID,
    request: IssuePublicationRequest,
    session: SessionDependency,
    settings: SettingsDependency,
) -> IssuePublicationResponse:
    """가설을 `[AR]` 이슈로 발행하고 그 좌표를 반환한다."""
    experiment = await publish_experiment_issue(
        session,
        settings,
        experiment_id,
        request,
        generate=partial(_generate_text, settings),
    )
    return IssuePublicationResponse(
        issue_number=experiment.issue_number,
        issue_url=(
            f"https://github.com/{settings.github_repository}"
            f"/issues/{experiment.issue_number}"
        ),
        issue_branch=experiment.issue_branch,
    )
```

모듈 상단에 helper와 import를 추가합니다.

```python
from functools import partial

from agent_orchestration.app.llm import generate_response


async def _generate_text(settings: ServiceSettings, prompt: str) -> str:
    """LLM 백엔드 결과에서 텍스트만 꺼낸다.

    service는 LLM 계약을 모르고 `str -> str` awaitable만 받는다. 이 경계 덕분에
    테스트가 LLM 호출 횟수를 셀 수 있다.
    """
    completion = await generate_response(settings, prompt)
    return completion.text
```

`SettingsDependency`가 없으면 `main.py`에서 앱 상태에 설정을 두고 FastAPI 의존성으로
꺼내는 방식을 추가하십시오. 기존 `create_app()`이 `settings`를 클로저로 갖고 있으므로,
`app.state.settings = settings`를 두고 `Request`에서 읽는 것이 가장 작은 변경입니다.

- [ ] **Step 4: 예외를 상태 코드로 변환한다**

`main.py`의 기존 예외 핸들러 옆에 추가합니다.

```python
    @app.exception_handler(IssuePublicationLimitError)
    async def handle_publication_limit(
        _request: Request, error: IssuePublicationLimitError
    ) -> JSONResponse:
        """발행 상한 초과를 429로 변환한다."""
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": str(error)},
        )

    @app.exception_handler(GitHubIssueError)
    async def handle_github_issue_error(
        _request: Request, error: GitHubIssueError
    ) -> JSONResponse:
        """gh 실패를 502로 변환하되 사유만 노출한다."""
        logger.error("Issue publication failed: %s", error)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"detail": f"Failed to publish issue: {error.reason}"},
        )
```

`error`의 전체 메시지에는 `gh`의 stderr가 들어갈 수 있으므로 응답에는 `reason`만
싣습니다.

- [ ] **Step 5: 테스트 통과를 확인한다**

Run: `uv run --no-sync python -m pytest tests/test_experiment_issue_endpoint.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: 전체 회귀와 커밋**

```bash
uv run --no-sync python -m pytest -q
uv run --no-sync ruff check agent_orchestration autoresearch tests tools
git add agent_orchestration/app/experiments/router.py agent_orchestration/app/main.py \
        tests/test_experiment_issue_endpoint.py
git commit -m "feat: 이슈 발행 endpoint 추가"
```

---

## Task 7: 이미지에 `gh` 설치와 로컬 실증

**Files:**
- Modify: `deploy/agent_orchestration/api.Dockerfile`
- Test: 이미지 빌드와 수동 실증

- [ ] **Step 1: Dockerfile에 `gh`를 버전 고정해 설치한다**

`RUN python -m pip install ...` 아래, `COPY agent_orchestration/...` 위에 추가합니다.

```dockerfile
# gh CLI — 이슈 발행에 사용한다(#516). 버전을 고정하지 않으면 stderr 문자열 기반
# 오류 분류(github_issues.py)가 버전 변경으로 조용히 깨진다.
ARG GH_VERSION=2.97.0
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl; \
    arch="$(dpkg --print-architecture)"; \
    curl -fsSL -o /tmp/gh.tar.gz \
      "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz"; \
    tar -xzf /tmp/gh.tar.gz -C /tmp; \
    install -m 0555 "/tmp/gh_${GH_VERSION}_linux_${arch}/bin/gh" /usr/local/bin/gh; \
    rm -rf /tmp/gh.tar.gz "/tmp/gh_${GH_VERSION}_linux_${arch}"; \
    apt-get purge -y curl; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*; \
    gh --version
```

- [ ] **Step 2: 이미지를 빌드해 `gh`를 확인한다**

```bash
docker build -f deploy/agent_orchestration/api.Dockerfile -t autoresearch-orch-api:ci .
docker run --rm autoresearch-orch-api:ci gh --version
docker run --rm autoresearch-orch-api:ci python -c "import agent_orchestration.app.main"
```

Expected: `gh version 2.97.0`이 출력되고 import가 성공합니다.

- [ ] **Step 3: 커밋**

```bash
git add deploy/agent_orchestration/api.Dockerfile
git commit -m "chore: API 이미지에 gh CLI 버전 고정 설치"
```

- [ ] **Step 4: 로컬에서 전 구간을 1회 실증한다**

이 계획의 완료 조건입니다. `.env`에 Task 3의 변수 6개를 채우고 진행합니다.
`ORCH_GITHUB_TOKEN`은 로컬 개인 `gh` 토큰(`gh auth token`)을 씁니다.

```bash
docker compose -f agent_orchestration/docker-compose.yml up -d --wait
uv run --no-sync alembic -c agent_orchestration/alembic.ini upgrade head
uv run uvicorn agent_orchestration.app.main:app --env-file .env --port 8000 &

EXPERIMENT_ID=$(curl -sS -X POST localhost:8000/experiments \
  -H "X-Orch-Token: $ORCH_API_TOKEN" -H 'content-type: application/json' \
  -d '{"hypothesis":"비율 피처가 ROC-AUC를 높인다."}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

curl -sS -X POST "localhost:8000/experiments/$EXPERIMENT_ID/issue" \
  -H "X-Orch-Token: $ORCH_API_TOKEN" -H 'content-type: application/json' \
  -d '{"allowed_scope":["prod_model_contract"]}'
```

- [ ] **Step 5: 결과를 확인한다**

```bash
gh issue list --label auto-experiment --limit 5
gh run list --workflow auto-research-issue-branch.yml --limit 3
git ls-remote --heads origin 'refs/heads/exp/*'
```

확인할 것:

1. `[AR]` 이슈가 열렸고 `auto-experiment` label이 붙어 있다
2. `auto-research-issue-branch.yml` run이 **success**다
3. `exp/<번호>-<slug>` 브랜치가 생겼다
4. 이슈에 marker 코멘트(`<!-- auto-research-issue-branch:v1 -->`)가 하나 있고
   `base_dev_sha`가 `dev` tip과 같다
5. 본문의 `랜덤 시드 목록`이 `42, 43, ..., 71`이다

- [ ] **Step 6: 멱등성을 실증한다**

같은 실험에 발행을 한 번 더 요청합니다.

```bash
curl -sS -X POST "localhost:8000/experiments/$EXPERIMENT_ID/issue" \
  -H "X-Orch-Token: $ORCH_API_TOKEN" -H 'content-type: application/json' -d '{}'
gh issue list --label auto-experiment --limit 5
```

Expected: 같은 `issue_number`가 반환되고 **이슈가 하나만** 있습니다.

- [ ] **Step 7: 검수 발행물을 정리한다**

이슈는 **close하되 exp 브랜치는 남깁니다.** 브랜치만 지우면 fail-closed되고, marker까지
지우면 다른 기준선으로 조용히 재생성되어 원래 `base_dev_sha`를 인용한 곳과 아무 실패
없이 어긋납니다.

```bash
gh issue close <발행된 번호> --comment "#516 검수 발행. exp 브랜치는 계약대로 남깁니다."
```

- [ ] **Step 8: 실증 결과를 이슈에 기록하고 PR을 만든다**

이슈 번호, 워크플로 run URL, 브랜치 이름, marker의 `base_dev_sha`를 #516에 코멘트로
남깁니다. 그다음 PR을 만듭니다.

```bash
gh pr create --base main --title "feat: 가설을 받아 [AR] 이슈를 발행하는 경로 추가" \
  --label feature --assignee @me --body-file pr-body.md
```

PR 본문에는 `Closes #516`, 실증 결과, 그리고 **인접 저장소 의존성**(배포·토큰·마이그레이션
실행이 `Autoresearch-infra` 소유라 배포 환경에서는 아직 동작하지 않음)을 명시합니다.

---

## 자체 검토 결과

**spec 커버리지.** 결정 1~8이 모두 task에 대응합니다 — 1·5는 Task 4·5(발행 주체와 수단),
2는 Task 2·5(필드 소유권과 `allowed_scope`), 3은 Task 2(시드), 4는 Task 2(본문 조립),
6은 Task 2(런타임 파서 미사용 + 테스트 대조), 7은 Task 5·6(2단계와 멱등성), 8은 변경
없음이라 task가 없습니다. spec의 테스트 6항목과 완료 조건 4항목도 각각 Task 2·5·6·7에
있습니다.

**남은 위험 하나.**

`_branch_name_for`와 `branch_name_for()`의 드리프트는 Task 5 Step 5의 대조 테스트가
막습니다 — 런타임은 `tools/`를 import할 수 없지만 테스트는 할 수 있습니다.

1. **`ORCH_GITHUB_TOKEN`이 필수라 기존 로컬 실행이 깨집니다.** 현재
   `agent_orchestration`은 배포돼 있지 않아 실질 영향이 없지만, 로컬에서 이 변수 없이
   `uvicorn`을 띄우던 사람은 기동에 실패합니다. `.env.example` 갱신과 PR 본문 명시로
   대응합니다.

---

## Task 8: 학습 기간을 발행 시점 계산으로 전환

**배경.** `ORCH_EXPERIMENT_DATASET_WINDOW`를 환경변수에 박힌 고정 문자열로 두면 한 번
적은 날짜가 영원히 그대로다. 아무도 갱신하지 않을 것이고, 6개월 뒤 발행되는 이슈도 같은
기간을 주장한다. **첫날부터 낡는 값**이다.

기간은 **발행 시점**에 계산해야 한다. 실행 시점이 아니다 — baseline을 오늘, candidate를
내일 돌리면 서로 다른 데이터를 보게 되어 실험의 전제가 깨진다. 발행 시점에 계산해 본문에
박아 넣으면 `reproducibility_id`에 봉인되고 이후 실행이 그 값을 따른다.

`docs/specs/2026-07-24-action-log-slice-semantics.md`가 소비 계약을 이미 정했다 —
`dt BETWEEN P-30 AND P-1`, 즉 **어제까지 30일**. 오늘 파티션은 아직 채워지는 중이라
포함하지 않는다.

**Files:**
- Modify: `agent_orchestration/app/experiments/issue_authoring.py`
- Modify: `agent_orchestration/app/config.py`
- Modify: `agent_orchestration/app/experiments/service.py`
- Modify: `.env.example`, `README.md`, `.claude/docs/agent-project-reference.md`
- Test: `tests/test_issue_authoring.py`, `tests/test_agent_orchestration.py`,
  `tests/test_experiment_issue_publication.py`

**Interfaces:**
- Produces: `training_window(today: date) -> tuple[date, date]`,
  `ExperimentDefaults(dataset_source, training_config_ref)`,
  `build_issue_body(experiment_id, fields, defaults, allowed_scope, window)`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_issue_authoring.py`에 추가합니다.

```python
def test_training_window_follows_the_slice_consumption_contract() -> None:
    """`dt BETWEEN P-30 AND P-1` — 어제까지 30일.

    오늘 파티션은 아직 채워지는 중이라 포함하지 않는다.
    """
    start, end = training_window(date(2026, 8, 4))

    assert end == date(2026, 8, 3)
    assert start == date(2026, 7, 5)
    assert (end - start).days + 1 == 30


def test_body_renders_the_computed_window_in_both_fields() -> None:
    """`데이터셋 스냅샷`과 `대상 데이터 · 기간`이 같은 기간을 말해야 한다.

    둘이 어긋나면 사람이 읽는 값과 봉인되는 값이 달라진다.
    """
    window = training_window(date(2026, 8, 4))
    body = build_issue_body(EXPERIMENT_ID, _fields(), DEFAULTS, (), window)

    parsed = parse_issue_input(1, "[AR] window", body)

    assert parsed.dataset_snapshot.endswith("@2026-07-05..2026-08-03")
    assert "2026-07-05 ~ 2026-08-03" in parsed.dataset
```

`DEFAULTS`를 새 형태로 바꿉니다.

```python
DEFAULTS = ExperimentDefaults(
    dataset_source="feast://feast_offline_store/ctr_training_v1",
    training_config_ref="src/pipeline/config.yaml@abc1234",
)
```

기존 `build_issue_body(...)` 호출부에 `window` 인자를 더합니다. 고정 날짜를 쓰십시오 —
`date.today()`를 테스트에서 부르면 실행 날짜에 따라 결과가 달라집니다.

- [ ] **Step 2: 실패를 확인한다**

Run: `uv run --no-sync python -m pytest tests/test_issue_authoring.py -k window -v`
Expected: FAIL — `ImportError: cannot import name 'training_window'`

- [ ] **Step 3: `issue_authoring.py`를 고친다**

import에 `from datetime import date, timedelta`를 더하고, 상수와 함수를 추가합니다.

```python
# `docs/specs/2026-07-24-action-log-slice-semantics.md`의 소비 계약 `dt BETWEEN P-30
# AND P-1`을 따른다. 이 값을 바꾸면 발행되는 실험의 학습 구간이 달라진다.
TRAINING_WINDOW_DAYS = 30
# `src/pipeline/config.yaml`의 `data.path`와 같은 값이다. 사람이 읽는 설명에만 쓰이고
# `reproducibility_id` 해시에는 들어가지 않는다.
DATASET_PATH = "data/processed/training_dataset.csv"


def training_window(today: date) -> tuple[date, date]:
    """학습 대상 기간을 KST 기준으로 계산한다.

    오늘 파티션은 아직 채워지는 중이므로 어제까지 본다. 시계를 직접 읽지 않고 인자로
    받는다 — 그래야 테스트가 실행 날짜에 흔들리지 않는다.
    """
    end = today - timedelta(days=1)
    start = end - timedelta(days=TRAINING_WINDOW_DAYS - 1)
    return start, end
```

`ExperimentDefaults`를 바꿉니다.

```python
class ExperimentDefaults(BaseModel):
    """환경마다 달라지는 서버 소유 값.

    기간은 여기 두지 않는다 — 고정 문자열로 두면 첫날부터 낡는다.
    `training_window()`가 발행 시점에 계산한다.
    """

    model_config = ConfigDict(extra="forbid")

    dataset_source: str = Field(min_length=1, max_length=200)
    training_config_ref: str = Field(min_length=1, max_length=256)
```

`build_issue_body`가 `window`를 받아 두 필드를 렌더하게 합니다.

```python
def build_issue_body(
    experiment_id: uuid.UUID,
    fields: LlmIssueFields,
    defaults: ExperimentDefaults,
    allowed_scope: Sequence[str],
    window: tuple[date, date],
) -> str:
```

본문 조립에서 두 섹션을 이렇게 바꿉니다.

```python
    window_start, window_end = window
    dataset_snapshot = f"{defaults.dataset_source}@{window_start}..{window_end}"
    dataset_window = (
        f"- 데이터셋 / 경로: {DATASET_PATH}\n"
        f"- 기간 (KST YYYY-MM-DD ~ YYYY-MM-DD): {window_start} ~ {window_end}"
    )
```

`sections` 목록에서 `("데이터셋 스냅샷", defaults.dataset_snapshot)`을
`("데이터셋 스냅샷", dataset_snapshot)`으로, `("대상 데이터 · 기간",
defaults.dataset_window)`를 `("대상 데이터 · 기간", dataset_window)`로 바꿉니다.

- [ ] **Step 4: 설정을 고친다**

`config.py`에서 `ExperimentDefaults` 생성부를 바꾸고 `ORCH_EXPERIMENT_DATASET_WINDOW`를
제거합니다.

```python
    experiment_defaults = ExperimentDefaults(
        dataset_source=_require_env(
            "ORCH_EXPERIMENT_DATASET_SOURCE",
            os.getenv("ORCH_EXPERIMENT_DATASET_SOURCE"),
        ),
        training_config_ref=_require_env(
            "ORCH_EXPERIMENT_TRAINING_CONFIG_REF",
            os.getenv("ORCH_EXPERIMENT_TRAINING_CONFIG_REF"),
        ),
    )
```

`.env.example`에서 세 줄을 두 줄로 바꿉니다.

```dotenv
# 학습 데이터 출처 좌표입니다. 기간은 발행 시점에 서버가 계산해 붙이므로 여기에
# 날짜를 넣지 마십시오(`dt BETWEEN P-30 AND P-1`, 어제까지 30일).
ORCH_EXPERIMENT_DATASET_SOURCE=feast://feast_offline_store/ctr_training_v1
ORCH_EXPERIMENT_TRAINING_CONFIG_REF=
```

`README.md`와 `.claude/docs/agent-project-reference.md`의 환경 변수 목록도 갱신합니다
(7개 → 6개, `ORCH_EXPERIMENT_DATASET_WINDOW` 삭제, `DATASET_SNAPSHOT` →
`DATASET_SOURCE` 이름 변경).

- [ ] **Step 5: 서비스가 기간을 계산해 넘기게 한다**

`service.py`의 `publish_experiment_issue`에서 `build_issue_body` 호출부를 바꿉니다.

```python
        body = build_issue_body(
            experiment.id,
            fields,
            settings.experiment_defaults,
            request.allowed_scope,
            training_window(datetime.now(_KST).date()),
        )
```

모듈 상단에 추가합니다.

```python
from zoneinfo import ZoneInfo

from agent_orchestration.app.experiments.issue_authoring import training_window

# 학습 기간은 KST 날짜 경계로 계산한다. UTC로 계산하면 한국 시각 오전 9시 이전에
# 발행된 실험이 하루 앞선 구간을 보게 된다.
_KST = ZoneInfo("Asia/Seoul")
```

- [ ] **Step 6: 기존 테스트를 새 형태에 맞춘다**

`ExperimentDefaults`를 생성하는 모든 테스트가 깨집니다. `tests/test_agent_orchestration.py`,
`tests/test_experiment_issue_publication.py`, 그 밖에 `grep -rn "ExperimentDefaults\|
ORCH_EXPERIMENT_" tests/`로 찾은 지점을 모두 새 필드로 바꾸십시오. **단언을 지우거나
느슨하게 하지 마십시오** — 필드 이름만 바꿉니다.

- [ ] **Step 7: 전체 확인**

Run: `uv run --no-sync python -m pytest -q`
Run: `uv run --no-sync ruff check agent_orchestration tests`

- [ ] **Step 8: 커밋**

```bash
git add agent_orchestration/app/experiments/issue_authoring.py \
        agent_orchestration/app/config.py \
        agent_orchestration/app/experiments/service.py \
        .env.example README.md .claude/docs/agent-project-reference.md \
        tests/
git commit -m "feat: 학습 기간을 발행 시점 KST 계산으로 전환"
```
