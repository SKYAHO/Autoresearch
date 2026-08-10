# 실험 리포트 HTML 워크벤치 렌더 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 실험의 최종 산출물인 `report.md`를 Streamlit 워크벤치 결과 탭에서 우리가 소유하는 고정 HTML 페이지로 렌더한다.

**Architecture:** executor가 완주 보고(`POST /internal/executor/experiments/{id}/result`)에 리포트 본문을 nullable 필드로 함께 싣고, API가 그것을 `experiments.report_markdown`(deferred 컬럼)에 **지표 커밋과 다른 트랜잭션으로** 쓴다. UI는 별도 조회 endpoint로 본문을 받아 `markdown-it-py`(`html=False`)로 HTML로 바꾸고 `st.iframe`에 넣는다. 전 계층에서 **리포트는 지표에 종속된다** — 리포트 실패가 지표를 잃게 하는 경로를 어디에도 남기지 않는다.

**Tech Stack:** Python 3.11+, SQLAlchemy 2.0.51, Alembic, FastAPI, Pydantic v2, Streamlit 1.60.0, markdown-it-py 4.2.0, pytest, uv

**정본 spec:** `docs/specs/2026-08-10-experiment-report-html-workbench.md` (#647)

## Global Constraints

- 모든 주석·docstring·커밋 메시지는 **한국어**다. 저장소 관례를 따른다.
- 새 모듈은 최상단 docstring에 `[파이프라인]` / `[기능]` / `[비책임]`을 쓴다. 기존 모듈은 기능을 바꾸는 같은 커밋에서 docstring을 갱신한다 (`CLAUDE.md`).
- `MAX_REPORT_MARKDOWN_BYTES = 65536` — executor와 API 양쪽에 같은 값으로 둔다. `executor`는 `app` 패키지를 import하지 않으므로 상수를 공유하지 않고, **두 값이 같은지 확인하는 테스트**로 드리프트를 막는다.
- `MAX_REPORT_MARKDOWN_CHARS = 262144` — 요청 본문 폭주만 막는 성긴 **문자 수** 상한이다. 바이트 상한과 단위가 다르다.
- **`report_markdown` 때문에 요청을 거절하는 경로를 만들지 않는다.** 상한 위반은 422가 아니라 절단이다. `metric_snapshot` validator는 지금처럼 거절한다 — 건드리지 않는다.
- `orchestration-ui` 의존성 그룹: `markdown-it-py`, `streamlit>=1.60,<2`.
- 검증 명령은 항상 워크트리 루트에서 돈다:
  - `uv run python -m pytest`
  - `uv run --no-sync ruff check agent_orchestration autoresearch tests tools`
- **테스트는 SQLite in-memory로 돈다** (`tests/test_experiment_candidate_api.py`의 `sqlite_engine` fixture). PostgreSQL 고유 동작(NUL 거부, `UndefinedColumn`)은 실측 재현되지 않으므로 **주입한 예외로** 검증한다.
- 이 저장소는 Windows에서 개발된다. 리눅스였으면 통과할 기존 실패는 손대지 않고, 회귀는 **작업 전 baseline 대비 증감**으로 판단한다.

---

## File Structure

**신규**

| 파일 | 책임 |
| --- | --- |
| `agent_orchestration/migrations/versions/0006_experiment_report_markdown.py` | `experiments.report_markdown` 컬럼 추가 |
| `agent_orchestration/ui/report.py` | md → HTML 순수 변환과 고정 템플릿 조립. Streamlit을 import하지 않는다 |
| `tests/test_experiment_report_api.py` | 보고 적재·정규화·트랜잭션 독립·조회 endpoint 계약 |
| `tests/test_agent_orchestration_ui_report.py` | `ui/report.py` 순수 함수와 결과 탭 렌더 |

**수정**

| 파일 | 변경 |
| --- | --- |
| `agent_orchestration/app/experiments/models.py` | `Experiment.report_markdown` (deferred) |
| `agent_orchestration/app/experiments/repository.py` | `find_experiment_report` (undefer) |
| `agent_orchestration/app/experiments/schemas.py` | 상한 상수 2개, `ExecutorResultReportRequest.report_markdown`, `ExperimentReportResponse` |
| `agent_orchestration/app/experiments/service.py` | logger, `normalize_report_markdown`, `_store_report_markdown`, `get_experiment_report` |
| `agent_orchestration/app/experiments/router.py` | `GET /{experiment_id}/report` |
| `agent_orchestration/executor/report.py` | `MAX_REPORT_MARKDOWN_BYTES`, `truncate_report_markdown`, `read_report_markdown` |
| `agent_orchestration/executor/api_client.py` | `report_result(report_markdown=...)` |
| `agent_orchestration/executor/phase2.py` | `_ResultPayload`로 반환 확장, 보고에 본문 적재 |
| `agent_orchestration/ui/models.py` | `REPORT_STATUSES` |
| `agent_orchestration/ui/client.py` | `fetch_report` |
| `agent_orchestration/ui/state.py` | 리포트 3필드, `record_report`, `record_report_error` |
| `agent_orchestration/ui/app.py` | `refresh_report` |
| `agent_orchestration/ui/views.py` | 결과 탭을 `_render_results`로 교체 |
| `pyproject.toml` / `uv.lock` | `orchestration-ui` 그룹 |

---

## Task 1: DB 컬럼과 모델

**Files:**
- Create: `agent_orchestration/migrations/versions/0006_experiment_report_markdown.py`
- Modify: `agent_orchestration/app/experiments/models.py:170` (`candidate_sha` 바로 아래)
- Modify: `agent_orchestration/app/experiments/repository.py:25-35`
- Test: `tests/test_experiment_report_api.py`

**Interfaces:**
- Consumes: 없음 (첫 작업)
- Produces:
  - `Experiment.report_markdown: Mapped[str | None]` — deferred 컬럼
  - `find_experiment_report(session: Session, experiment_id: uuid.UUID, *, for_update: bool = False) -> Experiment | None`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_experiment_report_api.py`를 새로 만든다. fixture는 `tests/test_experiment_candidate_api.py:53-` 의 `sqlite_engine` / `db_session`을 같은 형태로 복제한다 (그 파일이 이미 그렇게 자립해 있다).

```python
"""실험 리포트 본문의 적재·정규화·조회 계약을 검증한다.

전체 파이프라인에서 executor가 완주 보고에 실은 `report.md` 본문이 DB에 적재되고
워크벤치가 그것을 별도 endpoint로 읽어 가는 구간의 service·HTTP 경계를 검증한다.
markdown → HTML 변환과 화면 렌더링은 `tests/test_agent_orchestration_ui_report.py`가
담당한다.
"""

from __future__ import annotations

from collections.abc import Iterator
import uuid

import pytest
from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from agent_orchestration.app.database import Base
from agent_orchestration.app.experiments.models import Experiment
from agent_orchestration.app.experiments.repository import find_experiment_report


@pytest.fixture
def sqlite_engine() -> Iterator[Engine]:
    """PostgreSQL UUID 함수를 재현하는 in-memory SQLAlchemy engine을 제공한다."""
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def register_uuid_function(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function(
            "gen_random_uuid", 0, lambda: str(uuid.uuid4())
        )

    Base.metadata.create_all(engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def db_session(sqlite_engine: Engine) -> Iterator[Session]:
    """요청 단위 Session을 제공한다."""
    factory = sessionmaker(bind=sqlite_engine, autoflush=False, expire_on_commit=False)
    with factory() as session:
        yield session


def test_report_markdown_is_deferred_from_the_default_select(db_session: Session) -> None:
    """목록 질의가 리포트 본문을 끌어오지 않는다.

    `find_experiments`는 `select(Experiment)`로 전체 컬럼을 읽으므로, 평범한 컬럼으로
    두면 목록 한 번이 최대 100행 × 64KB를 전송한다. deferred가 그것을 막는 유일한
    장치라 계약으로 고정한다.
    """
    experiment = Experiment(hypothesis="가설", report_markdown="# 리포트")
    db_session.add(experiment)
    db_session.commit()
    db_session.expunge_all()

    loaded = db_session.get(Experiment, experiment.id)
    assert "report_markdown" in inspect(loaded).unloaded


def test_find_experiment_report_loads_the_body(db_session: Session) -> None:
    """조회 경로는 `undefer`로 본문을 함께 싣는다."""
    experiment = Experiment(hypothesis="가설", report_markdown="# 리포트")
    db_session.add(experiment)
    db_session.commit()
    db_session.expunge_all()

    loaded = find_experiment_report(db_session, experiment.id)
    assert loaded is not None
    assert "report_markdown" not in inspect(loaded).unloaded
    assert loaded.report_markdown == "# 리포트"


def test_find_experiment_report_returns_none_for_a_missing_experiment(
    db_session: Session,
) -> None:
    """없는 실험은 None이다 — 예외를 올리는 것은 service의 몫이다."""
    assert find_experiment_report(db_session, uuid.uuid4()) is None
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `uv run python -m pytest tests/test_experiment_report_api.py -v`
Expected: FAIL — `ImportError: cannot import name 'find_experiment_report'`

- [ ] **Step 3: 모델에 deferred 컬럼을 추가한다**

`agent_orchestration/app/experiments/models.py`의 `candidate_sha` 줄 바로 아래에 넣는다.

```python
    # 실험을 수행한 에이전트가 쓴 `report.md` 본문이다. **deferred**로 둔다 —
    # `find_experiments`가 `select(Experiment)`로 전체 컬럼을 읽으므로(`repository.py`),
    # 평범한 컬럼이면 목록 한 번이 최대 100행 × 64KB를 끌어온다. 응답 스키마에서
    # 감추는 것과 질의가 읽지 않는 것은 다르다. 본문이 필요한 조회만 `undefer`로
    # 명시한다(`find_experiment_report`). 계약 정본은
    # `docs/specs/2026-08-10-experiment-report-html-workbench.md` 결정 1이다.
    report_markdown: Mapped[str | None] = mapped_column(
        Text, nullable=True, deferred=True
    )
```

같은 커밋에서 모듈 docstring 끝에 한 문단을 덧붙인다.

```
`report_markdown`은 `0006_experiment_report_markdown` revision이 nullable로 추가한 실험의
최종 산출물 본문이며, 이 모듈에서 유일하게 `deferred=True`인 컬럼이다. 이유는 컬럼 옆
주석에 있다. `ExperimentResponse`에 노출하지 않으며 전용 endpoint가 조회한다.
```

- [ ] **Step 4: repository에 undefer 조회를 추가한다**

`agent_orchestration/app/experiments/repository.py`의 import에 `undefer`를 더하고 `find_experiment` 아래에 넣는다.

```python
from sqlalchemy.orm import Session, undefer
```

```python
def find_experiment_report(
    session: Session,
    experiment_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Experiment | None:
    """리포트 본문을 함께 로드해 실험을 조회한다.

    `Experiment.report_markdown`은 deferred라 `find_experiment`로 읽으면 접근 시점에
    별도 SELECT가 나간다. 본문이 목적인 조회는 그것을 한 번에 싣는다.
    """
    statement = (
        select(Experiment)
        .where(Experiment.id == experiment_id)
        .options(undefer(Experiment.report_markdown))
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalar(statement)
```

- [ ] **Step 5: migration을 작성한다**

`agent_orchestration/migrations/versions/0006_experiment_report_markdown.py`

```python
"""Experiment에 에이전트가 쓴 리포트 본문을 추가한다.

전체 파이프라인에서 executor가 완주 보고에 실은 `report.md` 본문을 실험 행에 한 번
적재하는 schema 구간을 담당한다. 적재 시점의 트랜잭션 분리와 정규화는 application
service의 책임이다.

Revision ID: 0006_experiment_report_markdown
Revises: 0005_experiment_candidate_sha
Create Date: 2026-08-10
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_experiment_report_markdown"
down_revision = "0005_experiment_candidate_sha"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """nullable 리포트 본문 컬럼을 추가한다.

    형식 제약을 두지 않는다 — 본문은 에이전트가 쓴 자유 서술이고, 크기 상한은
    거절이 아니라 절단으로 처리한다(spec 결정 3).
    """
    op.add_column(
        "experiments", sa.Column("report_markdown", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    """리포트 본문 컬럼을 제거한다."""
    op.drop_column("experiments", "report_markdown")
```

- [ ] **Step 6: 테스트가 통과하는지 확인한다**

Run: `uv run python -m pytest tests/test_experiment_report_api.py -v`
Expected: PASS (3 passed)

- [ ] **Step 7: lint를 돌린다**

Run: `uv run --no-sync ruff check agent_orchestration tests`
Expected: `All checks passed!`

- [ ] **Step 8: 커밋한다**

```bash
git add agent_orchestration/app/experiments/models.py \
        agent_orchestration/app/experiments/repository.py \
        agent_orchestration/migrations/versions/0006_experiment_report_markdown.py \
        tests/test_experiment_report_api.py
git commit -m "feat: 실험에 리포트 본문 컬럼을 deferred로 추가한다

목록 질의가 `select(Experiment)`로 전체 컬럼을 읽으므로 평범한 컬럼이면 한 번에
최대 100행 × 64KB를 끌어온다. deferred로 두고 본문이 목적인 조회만 undefer한다.

Refs #647"
```

---

## Task 2: 보고 스키마와 적재 (트랜잭션 분리)

**Files:**
- Modify: `agent_orchestration/app/experiments/schemas.py:34` (상수), `:106-135` (`ExecutorResultReportRequest`)
- Modify: `agent_orchestration/app/experiments/service.py` (logger, 정규화, `_store_report_markdown`, `record_experiment_result`)
- Test: `tests/test_experiment_report_api.py`

**Interfaces:**
- Consumes: `find_experiment_report` (Task 1)
- Produces:
  - `schemas.MAX_REPORT_MARKDOWN_BYTES: int = 65536`
  - `schemas.MAX_REPORT_MARKDOWN_CHARS: int = 262144`
  - `ExecutorResultReportRequest.report_markdown: str | None`
  - `service.normalize_report_markdown(text: str) -> str`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_experiment_report_api.py`에 이어 붙인다. `create_experiment` / `record_candidate` / `record_experiment_result`와 `ISSUE_NUMBER` 등 상수는 `tests/test_experiment_candidate_api.py`와 같은 값을 쓴다.

```python
from agent_orchestration.app.experiments.models import ExperimentStatus
from agent_orchestration.app.experiments.schemas import (
    MAX_REPORT_MARKDOWN_BYTES,
    CandidateReportRequest,
    ExecutorResultReportRequest,
    ExperimentCreate,
)
from agent_orchestration.app.experiments.service import (
    create_experiment,
    normalize_report_markdown,
    record_candidate,
    record_experiment_result,
)

ISSUE_NUMBER = 647
ISSUE_BRANCH = "exp/647"
BASE_DEV_SHA = "a" * 40
CANDIDATE_SHA = "b" * 40
SNAPSHOT = {"contract_version": "experiment-metric-snapshot-v1", "primary_metric": "roc_auc"}


def _evaluating_experiment(session: Session) -> uuid.UUID:
    """candidate까지 보고된 EVALUATING 실험 하나를 만든다."""
    experiment = create_experiment(session, ExperimentCreate(hypothesis="가설"))
    record_candidate(
        session,
        experiment.id,
        CandidateReportRequest(
            idempotency_key=f"executor-candidate:{experiment.id}",
            issue_number=ISSUE_NUMBER,
            issue_branch=ISSUE_BRANCH,
            base_dev_sha=BASE_DEV_SHA,
            candidate_sha=CANDIDATE_SHA,
        ),
    )
    return experiment.id


def _result_request(experiment_id: uuid.UUID, **overrides: object) -> ExecutorResultReportRequest:
    """완주 보고 요청을 만든다."""
    values: dict[str, object] = {
        "idempotency_key": f"executor-result:{experiment_id}",
        "candidate_sha": CANDIDATE_SHA,
        "metric_snapshot": SNAPSHOT,
    }
    values.update(overrides)
    return ExecutorResultReportRequest.model_validate(values)


def test_result_report_without_a_report_still_passes(db_session: Session) -> None:
    """리포트 없는 기존 보고 경로가 그대로 성립한다 (회귀)."""
    experiment_id = _evaluating_experiment(db_session)
    record_experiment_result(db_session, experiment_id, _result_request(experiment_id))

    stored = find_experiment_report(db_session, experiment_id)
    assert stored is not None
    assert stored.status == ExperimentStatus.PASSED.value
    assert stored.report_markdown is None


def test_result_report_stores_the_report_body(db_session: Session) -> None:
    """리포트를 실으면 본문이 적재되고 지표 전이도 그대로 일어난다."""
    experiment_id = _evaluating_experiment(db_session)
    record_experiment_result(
        db_session,
        experiment_id,
        _result_request(experiment_id, report_markdown="# 결론\n\n올랐다."),
    )

    stored = find_experiment_report(db_session, experiment_id)
    assert stored is not None
    assert stored.status == ExperimentStatus.PASSED.value
    assert stored.report_markdown == "# 결론\n\n올랐다."


def test_report_write_failure_leaves_the_metric_commit_in_place(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """리포트 적재가 터져도 지표 커밋은 남는다.

    PostgreSQL의 NUL 거부와 배포 순서 어긋남(`UndefinedColumn`)은 SQLite에서 재현되지
    않으므로 같은 자리에 예외를 주입해 성질만 고정한다 — 검증 대상은 실패의 종류가
    아니라 **지표가 살아남는가**다.
    """
    import agent_orchestration.app.experiments.service as service_module

    experiment_id = _evaluating_experiment(db_session)

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(service_module, "find_experiment_report", explode)
    record_experiment_result(
        db_session,
        experiment_id,
        _result_request(experiment_id, report_markdown="# 결론"),
    )

    db_session.expunge_all()
    stored = find_experiment_report(db_session, experiment_id)
    assert stored is not None
    assert stored.status == ExperimentStatus.PASSED.value
    assert stored.report_markdown is None


def test_retry_with_a_different_report_keeps_the_first_and_warns(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """재시도가 다른 본문을 실어도 첫 보고가 정본이고, 조용히 버리지 않는다."""
    experiment_id = _evaluating_experiment(db_session)
    record_experiment_result(
        db_session, experiment_id, _result_request(experiment_id, report_markdown="첫 번째")
    )
    with caplog.at_level("WARNING"):
        record_experiment_result(
            db_session,
            experiment_id,
            _result_request(experiment_id, report_markdown="두 번째"),
        )

    stored = find_experiment_report(db_session, experiment_id)
    assert stored is not None
    assert stored.report_markdown == "첫 번째"
    assert "already set, mismatch on retry" in caplog.text
    assert "두 번째" not in caplog.text


def test_normalize_strips_nul_and_truncates_without_rejecting() -> None:
    """정규화는 거절하지 않는다 — NUL을 지우고 상한을 넘으면 자른다."""
    assert normalize_report_markdown("가\x00나") == "가나"

    oversized = "가" * MAX_REPORT_MARKDOWN_BYTES
    normalized = normalize_report_markdown(oversized)
    assert len(normalized.encode("utf-8")) <= MAX_REPORT_MARKDOWN_BYTES
    assert normalized.endswith("\n")
    assert "잘렸습니다" in normalized


def test_normalize_keeps_a_body_within_the_limit_untouched() -> None:
    """상한 안이면 그대로 둔다 — 문구가 붙지 않는다."""
    assert normalize_report_markdown("# 결론") == "# 결론"
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `uv run python -m pytest tests/test_experiment_report_api.py -v`
Expected: FAIL — `ImportError: cannot import name 'normalize_report_markdown'`

- [ ] **Step 3: 스키마에 상수와 필드를 더한다**

`agent_orchestration/app/experiments/schemas.py`의 `MAX_METRIC_SNAPSHOT_BYTES` 아래에 넣는다.

```python
# 리포트 본문의 저장 상한(UTF-8 바이트). executor가 먼저 자르고
# (`executor/report.py`) service가 한 번 더 자른다 — **둘 다 거절이 아니라 절단이다.**
# 거절 경로를 남기면 리포트 내용이 지표 보고를 죽이는 결합이 되살아난다(spec 결정 3).
MAX_REPORT_MARKDOWN_BYTES = 65536
# 요청 본문 폭주만 막는 성긴 상한이다. **문자 수**라 위 바이트 상한과 단위가 다르며,
# DB에 들어갈 크기를 정하는 것은 service의 절단이다.
MAX_REPORT_MARKDOWN_CHARS = 262144
```

`ExecutorResultReportRequest`의 `metric_snapshot` 아래에 필드를 더한다. **validator를 붙이지 않는다.**

```python
    # 에이전트가 쓴 `report.md` 본문. 없이 보고해도 성립한다 — 리포트 실패가 지표
    # 게시를 막지 않는다는 성질이 여기서 유지된다. 크기·내용 검증을 여기서 하지 않는
    # 이유는 spec 결정 3에 있다: 이 필드로 요청을 거절하면 리포트가 지표를 죽인다.
    report_markdown: str | None = Field(default=None, max_length=MAX_REPORT_MARKDOWN_CHARS)
```

- [ ] **Step 4: service에 logger와 정규화를 더한다**

`agent_orchestration/app/experiments/service.py` import에 `logging`을 더하고, 모듈 상수 근처에 넣는다.

```python
logger = logging.getLogger(__name__)

# service가 상한을 넘겨 자를 때 본문 끝에 남기는 고정 문구다. executor의 문구와 문안을
# 다르게 두어 **어느 계층이 잘랐는지**가 화면에서 구분되게 한다.
_REPORT_TRUNCATION_NOTE = "\n\n[하네스] 리포트가 상한을 넘어 API에서 잘렸습니다.\n"


def normalize_report_markdown(text: str) -> str:
    """DB에 저장할 수 있는 형태로 리포트 본문을 정규화한다.

    **거절하지 않는다.** 이 함수가 예외를 올리면 그 예외가 완주 보고를 죽이고, 그것은
    "리포트는 지표에 종속된다"는 계약과 정반대다(spec 결정 3).

    NUL을 지우는 이유는 PostgreSQL이 text 값에 `U+0000`을 저장하지 못하기 때문이다.
    `report.md`는 `read_text(errors="replace")`로 읽히는데 그 옵션은 잘못된 UTF-8만
    바꿀 뿐 정상 디코드되는 0x00은 그대로 통과시킨다.
    """
    cleaned = text.replace("\x00", "")
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= MAX_REPORT_MARKDOWN_BYTES:
        return cleaned
    budget = MAX_REPORT_MARKDOWN_BYTES - len(_REPORT_TRUNCATION_NOTE.encode("utf-8"))
    return encoded[:budget].decode("utf-8", errors="ignore") + _REPORT_TRUNCATION_NOTE
```

import에 `find_experiment_report`, `MAX_REPORT_MARKDOWN_BYTES`를 더한다.

- [ ] **Step 5: 별도 트랜잭션 적재를 구현한다**

`record_experiment_result` 아래에 넣는다.

```python
def _store_report_markdown(
    session: Session, experiment_id: uuid.UUID, raw_markdown: str
) -> None:
    """리포트 본문을 지표 커밋과 **다른 트랜잭션**에 쓴다.

    같은 트랜잭션에 두면 리포트 쓰기 실패가 지표까지 롤백시킨다. 그 실패는 가상이
    아니다 — PostgreSQL의 NUL 거부와, migration `0006` 이전에 코드가 뜬 배포 순서
    어긋남(deferred 컬럼이라 SELECT는 통과하고 UPDATE에서 터진다) 두 경로가 있다.

    **어떤 예외도 위로 올리지 않는다.** 여기서 예외가 나가면 이미 커밋된 지표 보고가
    200에서 500으로 바뀌고, executor는 그것을 실패로 읽어 Job이 ERROR로 회수된다.
    `with session.begin()`의 `__exit__`가 이미 rollback하므로 명시적 rollback은 넣지
    않는다(`_transition_experiment` 호출부의 주석과 같은 근거).

    **write-once.** 이미 값이 있으면 덮어쓰지 않는다. 지표는 다르면 409지만 리포트는
    그렇게 하지 않는다 — 재시도가 리포트 때문에 실패하면 지표 보고까지 잃는다. 대신
    다른 본문이 왔다는 사실은 로그에 남긴다. 본문 자체는 싣지 않는다(최대 64KB의 LLM
    산출물이다).
    """
    try:
        normalized = normalize_report_markdown(raw_markdown)
        if normalized != raw_markdown:
            logger.warning(
                "report_markdown normalized experiment_id=%s", experiment_id
            )
        with session.begin():
            experiment = find_experiment_report(session, experiment_id, for_update=True)
            if experiment is None:
                return
            if experiment.report_markdown is None:
                experiment.report_markdown = normalized
            elif experiment.report_markdown != normalized:
                logger.warning(
                    "report_markdown ignored: already set, mismatch on retry "
                    "experiment_id=%s",
                    experiment_id,
                )
    except Exception as error:  # noqa: BLE001 - 리포트 실패가 지표 커밋을 되돌리면 안 된다
        logger.error(
            "report_markdown write failed experiment_id=%s error_type=%s",
            experiment_id,
            type(error).__name__,
        )
```

`record_experiment_result`의 `with session.begin():` 블록 **뒤**, `return experiment` **앞**에 두 줄을 넣는다. 기존 블록 내부는 손대지 않는다.

```python
    # 지표는 위에서 이미 커밋됐다. 리포트는 여기서부터 독립이다 — 이 아래에서 무엇이
    # 터져도 완주 보고는 성립한다.
    if request.report_markdown is not None:
        _store_report_markdown(session, experiment_id, request.report_markdown)
    return experiment
```

`record_experiment_result`의 docstring 끝에 한 문단을 덧붙인다.

```
`report_markdown`은 **다른 트랜잭션**에 쓴다. 리포트 쓰기 실패가 지표 커밋을 되돌리면
안 되기 때문이며, 그 근거는 `_store_report_markdown`에 있다.
```

- [ ] **Step 6: 테스트가 통과하는지 확인한다**

Run: `uv run python -m pytest tests/test_experiment_report_api.py -v`
Expected: PASS (9 passed)

- [ ] **Step 7: 기존 executor API 계약이 깨지지 않았는지 확인한다**

Run: `uv run python -m pytest tests/test_experiment_candidate_api.py -v`
Expected: PASS — 실패가 있으면 Task 시작 전 baseline과 대조한다.

- [ ] **Step 8: 커밋한다**

```bash
git add agent_orchestration/app/experiments/schemas.py \
        agent_orchestration/app/experiments/service.py \
        tests/test_experiment_report_api.py
git commit -m "feat: 완주 보고에 리포트 본문을 실어 별도 트랜잭션으로 적재한다

리포트 쓰기를 지표 커밋과 같은 트랜잭션에 두면 NUL 바이트나 배포 순서 어긋남이
지표까지 롤백시킨다. 정규화도 validator가 아니라 service에 둬 요청을 거절하는
경로를 남기지 않는다.

Refs #647"
```

---

## Task 3: 리포트 조회 endpoint

**Files:**
- Modify: `agent_orchestration/app/experiments/schemas.py` (`ExperimentReportResponse`)
- Modify: `agent_orchestration/app/experiments/service.py` (`get_experiment_report`)
- Modify: `agent_orchestration/app/experiments/router.py:269-280` 아래
- Test: `tests/test_experiment_report_api.py`

**Interfaces:**
- Consumes: `find_experiment_report` (Task 1)
- Produces:
  - `ExperimentReportResponse{experiment_id: uuid.UUID, report_markdown: str | None}`
  - `service.get_experiment_report(session, experiment_id) -> str | None`
  - `GET /experiments/{experiment_id}/report`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_experiment_report_api.py`에 이어 붙인다. TestClient fixture는 `tests/test_experiment_candidate_api.py`의 `client` fixture와 같은 형태로 만든다 (`ServiceSettings`에 `orchestration_api_token=API_TOKEN`, app state에 session factory 주입).

```python
def test_report_endpoint_returns_the_body(client: TestClient, db_session: Session) -> None:
    """리포트가 있으면 본문을 돌려준다."""
    experiment_id = _evaluating_experiment(db_session)
    record_experiment_result(
        db_session, experiment_id, _result_request(experiment_id, report_markdown="# 결론")
    )

    response = client.get(f"/experiments/{experiment_id}/report", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["report_markdown"] == "# 결론"
    assert response.json()["experiment_id"] == str(experiment_id)


def test_report_endpoint_returns_null_when_there_is_no_report(
    client: TestClient, db_session: Session
) -> None:
    """실험은 있고 리포트가 없으면 404가 아니라 200 + null이다.

    404로 만들면 UI가 "실험이 사라졌다"와 "아직 리포트가 없다"를 구별할 수 없다.
    후자는 오류가 아니라 정상 상태다.
    """
    experiment_id = _evaluating_experiment(db_session)

    response = client.get(f"/experiments/{experiment_id}/report", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["report_markdown"] is None


def test_report_endpoint_404s_for_a_missing_experiment(client: TestClient) -> None:
    """없는 실험은 404다."""
    response = client.get(f"/experiments/{uuid.uuid4()}/report", headers=AUTH_HEADERS)
    assert response.status_code == 404


def test_report_endpoint_requires_the_api_token(client: TestClient, db_session: Session) -> None:
    """토큰 없이 리포트를 읽을 수 없다."""
    experiment_id = _evaluating_experiment(db_session)
    assert client.get(f"/experiments/{experiment_id}/report").status_code == 401
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `uv run python -m pytest tests/test_experiment_report_api.py -k report_endpoint -v`
Expected: FAIL — 404 (라우트 없음)

- [ ] **Step 3: 응답 스키마를 더한다**

`agent_orchestration/app/experiments/schemas.py`의 `ExperimentResponse` 아래에 넣는다.

```python
class ExperimentReportResponse(BaseModel):
    """실험 리포트 본문 응답.

    `ExperimentResponse`와 분리한 이유는 그것이 5초 polling으로 반복 조회되고 목록
    화면에도 실리기 때문이다. 수십 KB 본문을 거기 실으면 목록까지 느려진다.
    """

    model_config = ConfigDict(from_attributes=True)

    experiment_id: uuid.UUID
    # 리포트가 아직 없으면 `None`이다. 실험이 없는 것과 구별되며, 그 경우는 404다.
    report_markdown: str | None
```

- [ ] **Step 4: service 조회를 더한다**

`get_experiment_metadata` 아래에 넣는다.

```python
def get_experiment_report(
    session: Session,
    experiment_id: uuid.UUID,
) -> str | None:
    """존재하는 실험의 리포트 본문을 반환한다.

    실험이 없으면 `ExperimentNotFoundError`다. 실험은 있고 리포트가 없으면 `None`이며
    그것은 오류가 아니다 — 완주 전 실험, 리포트를 끄고 돌린 배포, Codex가 실패한
    실행이 모두 여기 해당한다.
    """
    experiment = find_experiment_report(session, experiment_id)
    if experiment is None:
        raise ExperimentNotFoundError(experiment_id)
    return experiment.report_markdown
```

- [ ] **Step 5: 라우트를 더한다**

`agent_orchestration/app/experiments/router.py`의 metadata 라우트 아래에 넣는다. import에 `ExperimentReportResponse`와 `get_experiment_report`를 더한다.

```python
@router.get(
    "/{experiment_id}/report",
    response_model=ExperimentReportResponse,
    responses={**_UNAUTHORIZED_RESPONSE, **_NOT_FOUND_RESPONSE},
)
def get_experiment_report_by_id(
    experiment_id: uuid.UUID,
    session: SessionDependency,
) -> ExperimentReportResponse:
    """실험 리포트 본문을 조회한다. 리포트가 없으면 본문이 null이다."""
    return ExperimentReportResponse(
        experiment_id=experiment_id,
        report_markdown=get_experiment_report(session, experiment_id),
    )
```

- [ ] **Step 6: 테스트가 통과하는지 확인한다**

Run: `uv run python -m pytest tests/test_experiment_report_api.py -v`
Expected: PASS (13 passed)

- [ ] **Step 7: 커밋한다**

```bash
git add agent_orchestration/app/experiments/schemas.py \
        agent_orchestration/app/experiments/service.py \
        agent_orchestration/app/experiments/router.py \
        tests/test_experiment_report_api.py
git commit -m "feat: 실험 리포트 조회 endpoint를 낸다

ExperimentResponse는 5초 polling으로 반복 조회되므로 본문을 싣지 않고 분리한다.
리포트가 없는 것은 정상 상태라 404가 아니라 200 + null이다.

Refs #647"
```

---

## Task 4: executor 잘림과 보고 배선

**Files:**
- Modify: `agent_orchestration/executor/report.py` (상수 2개, 함수 2개)
- Modify: `agent_orchestration/executor/api_client.py:136-164`
- Modify: `agent_orchestration/executor/phase2.py:594-610`, `:630-650`
- Test: `tests/test_experiment_report_api.py` (상한 일치), 신규 executor 테스트는 기존 executor 테스트 파일 관례를 따른다

**Interfaces:**
- Consumes: `schemas.MAX_REPORT_MARKDOWN_BYTES` (Task 2, 대조 테스트에서만)
- Produces:
  - `executor.report.MAX_REPORT_MARKDOWN_BYTES: int = 65536`
  - `executor.report.truncate_report_markdown(text: str) -> str`
  - `executor.report.read_report_markdown(path: Path) -> str | None`
  - `api_client.report_result(..., report_markdown: str | None = None) -> None`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_experiment_report_api.py`에 상한 일치 테스트를 더한다.

```python
def test_executor_and_api_share_the_same_report_size_limit() -> None:
    """executor와 API의 상한이 갈리면 지표가 죽는다 — 두 값을 고정한다.

    `executor`는 `app` 패키지를 import하지 않으므로 상수를 공유할 수 없다. 드리프트를
    막는 것은 이 테스트뿐이다.
    """
    from agent_orchestration.executor.report import (
        MAX_REPORT_MARKDOWN_BYTES as EXECUTOR_LIMIT,
    )

    assert EXECUTOR_LIMIT == MAX_REPORT_MARKDOWN_BYTES
```

executor 순수 함수 테스트를 같은 파일에 더한다.

```python
def test_truncate_keeps_a_body_within_the_limit_untouched() -> None:
    """상한 안이면 그대로다."""
    from agent_orchestration.executor.report import truncate_report_markdown

    assert truncate_report_markdown("# 결론") == "# 결론"


def test_truncate_cuts_on_a_character_boundary() -> None:
    """멀티바이트 문자가 상한에 걸쳐도 깨진 문자를 남기지 않는다."""
    from agent_orchestration.executor.report import (
        MAX_REPORT_MARKDOWN_BYTES as LIMIT,
        truncate_report_markdown,
    )

    truncated = truncate_report_markdown("가" * LIMIT)

    assert len(truncated.encode("utf-8")) <= LIMIT
    assert "�" not in truncated
    assert truncated.endswith("\n")
    assert "executor에서 잘렸습니다" in truncated


def test_read_report_markdown_absorbs_a_missing_file(tmp_path) -> None:
    """본문 읽기 실패는 None이다 — 지표 보고를 막지 않는다."""
    from agent_orchestration.executor.report import read_report_markdown

    assert read_report_markdown(tmp_path / "없음.md") is None


def test_read_report_markdown_treats_a_blank_report_as_absent(tmp_path) -> None:
    """공백뿐인 리포트는 없는 것으로 본다."""
    from agent_orchestration.executor.report import read_report_markdown

    path = tmp_path / "report.md"
    path.write_text("   \n\n", encoding="utf-8")
    assert read_report_markdown(path) is None
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `uv run python -m pytest tests/test_experiment_report_api.py -k "executor or truncate or read_report" -v`
Expected: FAIL — `ImportError: cannot import name 'MAX_REPORT_MARKDOWN_BYTES'`

- [ ] **Step 3: executor/report.py에 잘림과 읽기를 더한다**

`DIFF_FILENAME` 상수 근처에 넣는다.

```python
# API에 보고할 리포트 본문의 상한(UTF-8 바이트). `app/experiments/schemas.py`의 같은
# 이름 상수와 **반드시 같은 값**이어야 한다 — executor는 app 패키지를 import하지 않아
# 상수를 공유할 수 없고, 두 값이 갈리면 API가 잘라야 할 것을 executor가 안 잘라 보낸다.
# 일치는 `tests/test_experiment_report_api.py`가 고정한다.
MAX_REPORT_MARKDOWN_BYTES: Final = 65536

# 상한을 넘겨 잘랐을 때 본문 끝에 남기는 고정 문구. API 쪽 문구와 문안을 다르게 두어
# 어느 계층이 잘랐는지가 화면에서 구분되게 한다.
_REPORT_TRUNCATION_NOTE: Final = (
    "\n\n[하네스] 리포트가 상한을 넘어 executor에서 잘렸습니다.\n"
)
```

`missing_report_sections` 아래에 넣는다.

```python
def truncate_report_markdown(text: str) -> str:
    """API로 보낼 리포트 본문을 상한 안으로 줄인다.

    문구의 바이트를 예산에서 먼저 빼고 남은 만큼만 자른다. `errors="ignore"`로 디코드해
    멀티바이트 문자가 상한에 걸쳐도 깨진 문자를 남기지 않는다 —
    `capture_candidate_diff`가 같은 이유로 쓰는 방식이다.

    Returns:
        상한 안이면 원문 그대로, 넘으면 앞부분과 잘림 문구.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_REPORT_MARKDOWN_BYTES:
        return text
    budget = MAX_REPORT_MARKDOWN_BYTES - len(_REPORT_TRUNCATION_NOTE.encode("utf-8"))
    return encoded[:budget].decode("utf-8", errors="ignore") + _REPORT_TRUNCATION_NOTE


def read_report_markdown(path: Path) -> str | None:
    """게시한 리포트를 API 보고용 본문으로 읽는다.

    **어떤 실패도 위로 올리지 않는다.** 여기서 예외가 나가면 그것이 완주 보고를 막고,
    측정한 숫자마저 사라진다 — 리포트는 숫자보다 뒤에 온다는 이 모듈의 계약과 같다.

    Returns:
        보고할 본문. 파일이 없거나 읽지 못하거나 비어 있으면 `None`이다.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.strip():
        return None
    return truncate_report_markdown(text)
```

모듈 docstring `[기능]`에 "보고용 본문을 상한 안에서 읽어 낸다"를 덧붙인다.

- [ ] **Step 4: api_client에 본문을 실는다**

`report_result`의 시그니처와 payload를 고친다.

```python
def report_result(
    *,
    api_url: str,
    token_file: Path,
    experiment_id: uuid.UUID,
    candidate_sha: str,
    metric_snapshot: dict[str, object],
    report_markdown: str | None = None,
) -> None:
    """채점이 끝난 실험 지표를 보고하고 응답이 완주 상태인지 확인한다.

    응답 상태를 확인하는 이유는 candidate 보고에서 SHA를 되받아 확인하는 이유와
    같다 — 200을 받았다는 것과 상태가 실제로 옮겨갔다는 것은 다르다. 여기서 넘어가면
    지표가 어디에도 없는데 실행만 성공으로 끝난다.

    `report_markdown`이 `None`이면 key 자체를 싣지 않는다. 리포트 없이 보내는 것이
    정상 경로이고, API도 그렇게 받도록 돼 있다.
    """
    token = _read_token(token_file)
    payload: dict[str, object] = {
        "idempotency_key": f"executor-result:{experiment_id}",
        "candidate_sha": candidate_sha,
        "metric_snapshot": metric_snapshot,
    }
    if report_markdown is not None:
        payload["report_markdown"] = report_markdown
    response_payload = _post_json(
        _endpoint(api_url, experiment_id, "result"),
        payload,
        token,
        failure="result_api_failed",
        conflict="result_api_conflict",
    )
    if response_payload.get("status") != _COMPLETED_STATUS:
        raise CandidateApiError("result_api_status_unexpected")
```

- [ ] **Step 5: phase2가 본문을 꺼내 보내게 한다**

`_measure_and_publish_if_enabled`는 지금 `report_path`를 GCS 게시에만 쓰고 버린다. 반환을 넓힌다. 모듈 상단 근처에 dataclass를 더하고 import에 `read_report_markdown`을 추가한다.

```python
@dataclass(frozen=True)
class _ResultPayload:
    """API에 보고할 지표 요약과 리포트 본문이다.

    둘을 함께 돌려주는 이유는 리포트가 이 함수 안에서만 만들어지고 보고는 바깥에서
    일어나기 때문이다. 본문이 `None`이면 리포트 없이 보고한다.
    """

    snapshot: dict[str, object]
    report_markdown: str | None
```

`_measure_and_publish_if_enabled`의 마지막 부분을 고친다.

```python
    report_path = _write_report_if_enabled(
        workspace,
        metrics_path=metrics_path,
        issue_body=issue_body,
        base_dev_sha=base_dev_sha,
        candidate_sha=candidate_sha,
    )
    report_markdown = None
    if report_path is not None:
        _publish_report(
            results_root,
            report_path,
            experiment_id=experiment_id,
            issue_number=issue_number,
        )
        # 읽기 실패는 `None`으로 흡수된다. 게시는 이미 끝났고 워크벤치 표시를 위해
        # 지표 보고를 잃을 이유가 없다.
        report_markdown = read_report_markdown(report_path)
        if report_markdown is None:
            _LOGGER.warning("experiment report body was not readable for the API report")
    return _ResultPayload(
        snapshot=build_metric_snapshot(
            payload, results_uri=published[_METRICS_FILENAME].uri
        ),
        report_markdown=report_markdown,
    )
```

반환 타입 주석을 `_ResultPayload | None`로 바꾸고, `candidate_finalizer_main`의 호출부를 고친다.

```python
    result = _measure_and_publish_if_enabled(
        state.repository,
        seeds=seeds,
        experiment_id=experiment_id,
        issue_number=issue_number,
        issue_body=state.issue_body,
        base_dev_sha=base_dev_sha,
        candidate_sha=candidate_sha,
    )
    if result is not None:
        # 채점했으면 반드시 보고한다. 여기서 실패하면 stage가 실패해 Job이 Failed로
        # 끝나고 launcher가 실험을 ERROR로 회수한다 — GCS에는 결과가 있는데 실험은
        # 완주로 표시되지 않는 상태가 되지만, **없는 결과를 완주로 표시하는 것보다
        # 낫다.** 조용히 넘어가면 `metric_summary=null`이 다시 나온다.
        report_result(
            api_url=_required("ORCH_EXECUTOR_API_URL"),
            token_file=Path(_required("ORCH_EXECUTOR_API_TOKEN_FILE")),
            experiment_id=experiment_id,
            candidate_sha=candidate_sha,
            metric_snapshot=result.snapshot,
            report_markdown=result.report_markdown,
        )
```

모듈 docstring `[기능]`의 "요약을 Experiment API에 보고해"를 "요약과 리포트 본문을 Experiment API에 보고해"로 고친다.

- [ ] **Step 6: 테스트가 통과하는지 확인한다**

Run: `uv run python -m pytest tests/test_experiment_report_api.py -v`
Expected: PASS (18 passed)

- [ ] **Step 7: executor 기존 테스트가 깨지지 않았는지 확인한다**

Run: `uv run python -m pytest tests/ -k "executor or phase2 or report" -v`
Expected: PASS — 실패가 있으면 baseline과 대조한다.

- [ ] **Step 8: 커밋한다**

```bash
git add agent_orchestration/executor/report.py \
        agent_orchestration/executor/api_client.py \
        agent_orchestration/executor/phase2.py \
        tests/test_experiment_report_api.py
git commit -m "feat: executor가 완주 보고에 리포트 본문을 싣는다

_measure_and_publish_if_enabled가 report_path를 GCS 게시에만 쓰고 버려서 본문이
보고까지 닿는 통로가 없었다. 반환을 넓히되 본문 읽기 실패는 위로 올리지 않는다.

Refs #647"
```

---

## Task 5: 의존성 선언

**Files:**
- Modify: `pyproject.toml` (`orchestration-ui` 그룹)
- Modify: `uv.lock`

**Interfaces:**
- Consumes: 없음
- Produces: `markdown-it-py`, `streamlit>=1.60,<2`가 `orchestration-ui` 그룹에 선언됨

- [ ] **Step 1: 그룹을 고친다**

`pyproject.toml`의 `orchestration-ui`를 교체한다.

```toml
orchestration-ui = [
    # `st.iframe`이 1.60에 들어왔다. 리포트를 srcdoc iframe에 넣는 데 쓰며, 그 이전
    # 버전에는 HTML 문자열을 받는 대체 API가 deprecated된 것뿐이다(#647).
    "streamlit>=1.60,<2",
    # 리포트 md를 HTML로 바꾼다. `html=False`가 raw HTML을 escape하는 것이 이 경로의
    # **유일한 방어**다 — iframe은 격리 경계가 아니다
    # (`docs/specs/2026-08-10-experiment-report-html-workbench.md` 결정 5).
    "markdown-it-py>=4,<5",
]
```

- [ ] **Step 2: lock을 갱신한다**

Run: `uv lock`
Expected: 두 패키지가 이미 lock에 있으므로(streamlit 1.60.0, markdown-it-py 4.2.0) 그룹 선언만 반영된다. 다른 패키지 버전이 움직이면 **멈추고 보고한다** — 이 이슈의 범위가 아니다.

- [ ] **Step 3: diff를 확인한다**

Run: `git diff --stat uv.lock`
Expected: 변경이 `orchestration-ui` 그룹 선언 주변으로 국한된다.

- [ ] **Step 4: 설치가 성립하는지 확인한다**

Run: `uv sync`
Expected: 성공. 이어서 `uv run python -c "import markdown_it, streamlit; print(markdown_it.__version__, streamlit.__version__)"`가 `4.2.0 1.60.0`을 찍는다.

- [ ] **Step 5: 커밋한다**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: orchestration-ui에 markdown-it-py를 더하고 streamlit 하한을 올린다

리포트 렌더가 st.iframe(1.60)과 markdown-it-py에 직접 의존한다. 둘 다 이미 lock에
있어 설치 변동은 없고 선언만 명시한다.

Refs #647"
```

---

## Task 6: md → HTML 순수 변환

**Files:**
- Create: `agent_orchestration/ui/report.py`
- Test: `tests/test_agent_orchestration_ui_report.py`

**Interfaces:**
- Consumes: `markdown-it-py` (Task 5)
- Produces:
  - `render_report_html(markdown_text: str) -> str`
  - `build_report_document(body_html: str) -> str`
  - `report_document(markdown_text: str) -> str`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
"""워크벤치 리포트의 md → HTML 변환과 결과 탭 렌더 계약을 검증한다.

전체 파이프라인에서 API가 돌려준 리포트 본문이 화면에 그려지기까지의 UI 구간을
검증한다. 본문의 적재와 조회 endpoint는 `tests/test_experiment_report_api.py`가
담당한다.

**이 파일이 지키는 것은 escape 하나다.** iframe은 격리 경계가 아니므로
(Streamlit sandbox가 `allow-same-origin`과 `allow-scripts`를 둘 다 포함한다),
`html=False`가 뚫리면 방어가 남지 않는다.
"""

from __future__ import annotations

import pytest

pytest.importorskip("markdown_it", reason="orchestration-ui 그룹이 설치돼야 한다")

from agent_orchestration.ui.report import (  # noqa: E402
    build_report_document,
    render_report_html,
    report_document,
)


def test_inline_raw_html_is_escaped() -> None:
    """에이전트가 쓴 인라인 HTML이 태그가 아니라 텍스트가 된다."""
    rendered = render_report_html("raw <script>alert(1)</script> 끝")

    assert "&lt;script&gt;" in rendered
    assert "<script>" not in rendered


def test_block_raw_html_is_escaped() -> None:
    """블록 HTML도 escape된다 — 인라인만 막으면 뚫린다."""
    rendered = render_report_html('<div onclick="x">블록</div>')

    assert "&lt;div" in rendered
    assert "<div" not in rendered
    assert "onclick" not in rendered or "&quot;" in rendered


def test_javascript_links_are_neutralized() -> None:
    """`javascript:` 링크가 앵커로 만들어지지 않는다."""
    rendered = render_report_html("[누르지 마시오](javascript:alert(1))")

    assert "javascript:" not in rendered.replace("javascript:alert(1)", "")
    assert "<a href=\"javascript:" not in rendered


def test_data_text_html_links_are_neutralized() -> None:
    """`data:text/html` 링크도 앵커가 되지 않는다."""
    rendered = render_report_html("[문서](data:text/html,<b>x</b>)")

    assert '<a href="data:text/html' not in rendered


def test_ordinary_links_survive() -> None:
    """정상 링크는 그대로 앵커가 된다."""
    assert '<a href="https://example.com"' in render_report_html("[예](https://example.com)")


def test_empty_report_renders_without_error() -> None:
    """빈 본문도 빈 문자열로 변환된다 — 호출부가 분기하지 않아도 된다."""
    assert render_report_html("") == ""


def test_document_carries_no_script() -> None:
    """우리 템플릿이 스크립트 실행 표면을 만들지 않는다.

    격리가 없는 곳에 우리 손으로 스크립트를 넣을 이유가 없다(spec 결정 5).
    """
    document = build_report_document("<p>본문</p>")

    assert "<script" not in document.lower()
    assert "onload" not in document.lower()


def test_document_is_a_complete_html_page() -> None:
    """srcdoc에 넣을 완결된 문서다."""
    document = build_report_document("<p>본문</p>")

    assert document.startswith("<!doctype html>")
    assert "<p>본문</p>" in document
    assert 'lang="ko"' in document


def test_report_document_composes_both_steps() -> None:
    """호출부가 두 단계를 따로 부르지 않아도 된다."""
    document = report_document("# 제목")

    assert document.startswith("<!doctype html>")
    assert "<h1>제목</h1>" in document
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `uv run python -m pytest tests/test_agent_orchestration_ui_report.py -v`
Expected: FAIL — `ModuleNotFoundError: agent_orchestration.ui.report`

- [ ] **Step 3: 모듈을 만든다**

```python
"""실험 리포트 markdown을 워크벤치에 넣을 HTML 페이지로 바꾸는 순수 변환 경계.

[파이프라인]
Experiment API가 돌려준 `report_markdown`을 받은 뒤부터, `views.py`가 그것을 iframe에
넣기 전까지의 구간을 담당한다. 조회(`ui/client.py`)와 화면 배치(`ui/views.py`) 사이에
있으며 Streamlit을 import하지 않는다.

[기능]
markdown을 raw HTML 없이 HTML 조각으로 변환하고, 우리가 소유하는 고정 스타일 문서로
조립한다.

[비책임]
HTTP 조회(`ui/client.py`), session state(`ui/state.py`), 화면 배치와 iframe 삽입
(`ui/views.py`), 리포트 본문의 생성(`executor/report.py`)은 담당하지 않는다.

[중요] **iframe은 격리 경계가 아니다.** Streamlit의 iframe sandbox는 고정 목록이고
`allow-same-origin`과 `allow-scripts`를 둘 다 포함하며 본문은 srcdoc으로 들어간다 —
부모와 같은 origin이다. 따라서 이 모듈의 `html=False`가 **유일한 방어**다. 리포트를
쓰는 Codex의 입력에는 외부 사용자가 쓴 GitHub 이슈 본문 원문이 들어간다
(`executor/prompt.py`). 이 설정을 켜거나 여기서 만든 문서에 스크립트를 넣으면 그
경계가 사라진다. 계약 정본은
`docs/specs/2026-08-10-experiment-report-html-workbench.md` 결정 4·5다.
"""

from __future__ import annotations

from typing import Final

from markdown_it import MarkdownIt


# `html=False`가 인라인·블록 raw HTML을 모두 escape하고, 기본 `validateLink`가
# `javascript:`·`vbscript:`·`file:`·`data:`(이미지 제외) 링크를 앵커로 만들지 않는다.
# **이 설정을 바꾸지 않는다** — 모듈 docstring의 [중요]가 이유다.
_RENDERER: Final = MarkdownIt("commonmark", {"html": False})

# 리포트 문서의 고정 스타일. 우리가 소유하므로 여기를 고치면 과거 실험의 리포트도
# 전부 같이 바뀐다 — 변환을 UI에 둔 이유가 그것이다(spec 결정 4).
_STYLES: Final = """
  :root { color-scheme: light dark; }
  body {
    margin: 0;
    padding: 1.25rem 1.5rem 2rem;
    font-family: -apple-system, "Segoe UI", "Noto Sans KR", sans-serif;
    font-size: 0.95rem;
    line-height: 1.7;
    word-break: break-word;
  }
  h1 { font-size: 1.45rem; margin: 0 0 1rem; }
  h2 { font-size: 1.2rem; margin: 1.8rem 0 0.7rem; }
  h3 { font-size: 1.05rem; margin: 1.4rem 0 0.5rem; }
  p, li { margin: 0.5rem 0; }
  code { font-size: 0.88em; padding: 0.1em 0.35em; border-radius: 4px; }
  pre { padding: 0.9rem 1rem; border-radius: 8px; overflow-x: auto; }
  pre code { padding: 0; }
  table { border-collapse: collapse; width: 100%; margin: 1rem 0; display: block; overflow-x: auto; }
  th, td { border: 1px solid rgba(128, 128, 128, 0.45); padding: 0.4rem 0.6rem; text-align: left; }
  blockquote { margin: 1rem 0; padding: 0.1rem 1rem; border-left: 3px solid rgba(128, 128, 128, 0.5); }
  img { max-width: 100%; height: auto; }
"""


def render_report_html(markdown_text: str) -> str:
    """리포트 markdown을 raw HTML 없이 HTML 조각으로 바꾼다.

    Args:
        markdown_text: 에이전트가 쓴 `report.md` 본문.

    Returns:
        `<p>`·`<h2>` 등으로 이루어진 HTML 조각. 본문이 비면 빈 문자열이다.
    """
    return _RENDERER.render(markdown_text)


def build_report_document(body_html: str) -> str:
    """HTML 조각을 iframe srcdoc에 넣을 완결된 문서로 조립한다.

    **스크립트를 넣지 않는다.** 격리가 없는 곳에 실행 표면을 만들 이유가 없다.
    """
    return (
        "<!doctype html>\n"
        '<html lang="ko">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<style>{_STYLES}</style>\n"
        "</head>\n"
        f"<body>\n{body_html}\n</body>\n"
        "</html>\n"
    )


def report_document(markdown_text: str) -> str:
    """리포트 markdown 하나를 화면에 넣을 HTML 문서로 바꾼다."""
    return build_report_document(render_report_html(markdown_text))
```

- [ ] **Step 4: 테스트가 통과하는지 확인한다**

Run: `uv run python -m pytest tests/test_agent_orchestration_ui_report.py -v`
Expected: PASS (9 passed)

- [ ] **Step 5: 커밋한다**

```bash
git add agent_orchestration/ui/report.py tests/test_agent_orchestration_ui_report.py
git commit -m "feat: 리포트 md를 우리 템플릿의 HTML 페이지로 변환한다

iframe이 격리 경계가 아니므로 html=False escape가 유일한 방어다. 템플릿에 스크립트를
넣지 않는다.

Refs #647"
```

---

## Task 7: 조회 client와 session state

**Files:**
- Modify: `agent_orchestration/ui/client.py` (`fetch_report`)
- Modify: `agent_orchestration/ui/models.py` (`REPORT_STATUSES`)
- Modify: `agent_orchestration/ui/state.py` (3필드, `select_experiment` 초기화, recorder 2개)
- Test: `tests/test_agent_orchestration_ui_report.py`

**Interfaces:**
- Consumes: `GET /experiments/{id}/report` (Task 3)
- Produces:
  - `ExperimentClient.fetch_report(experiment_id: str) -> str | None`
  - `models.REPORT_STATUSES: frozenset[str]`
  - `WorkbenchState.report_markdown / report_error / report_loaded_for`
  - `state.record_report(state, experiment_id: str, markdown_text: str | None) -> None`
  - `state.record_report_error(state, message: str) -> None`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
from agent_orchestration.ui.models import REPORT_STATUSES  # noqa: E402
from agent_orchestration.ui.state import (  # noqa: E402
    WorkbenchState,
    record_report,
    record_report_error,
    select_experiment,
)


def test_report_statuses_are_exactly_the_states_that_can_hold_a_report() -> None:
    """리포트를 가진 실험은 PASSED 아니면 PROMOTED다.

    `record_experiment_result`가 유일한 기록자이고 PASSED를 하드코딩하며,
    `ALLOWED_TRANSITIONS[PASSED] = {PROMOTED}`라 PASSED에서 FAILED로 가는 간선이 없다.
    전이가 늘면 이 집합도 함께 넓혀야 한다.
    """
    assert REPORT_STATUSES == frozenset({"PASSED", "PROMOTED"})


def test_record_report_marks_the_experiment_as_loaded() -> None:
    """성공하면 본문과 함께 조회 완료 표식을 세운다."""
    state = WorkbenchState(selected_id="exp-1")
    record_report(state, "exp-1", "# 결론")

    assert state.report_markdown == "# 결론"
    assert state.report_loaded_for == "exp-1"
    assert state.report_error is None


def test_record_report_marks_loaded_even_when_there_is_no_report() -> None:
    """리포트가 없다는 사실도 조회 결과다 — 다시 묻지 않는다."""
    state = WorkbenchState(selected_id="exp-1")
    record_report(state, "exp-1", None)

    assert state.report_markdown is None
    assert state.report_loaded_for == "exp-1"


def test_record_report_error_does_not_mark_loaded() -> None:
    """실패에는 표식을 세우지 않는다 — 일시적 오류가 리포트를 영구히 가리면 안 된다."""
    state = WorkbenchState(selected_id="exp-1")
    record_report_error(state, "일시적 오류")

    assert state.report_error == "일시적 오류"
    assert state.report_loaded_for is None


def test_selecting_another_experiment_clears_the_report() -> None:
    """실험을 바꾸면 이전 리포트가 남지 않는다."""
    state = WorkbenchState(selected_id="exp-1")
    record_report(state, "exp-1", "# 결론")
    record_report_error(state, "오류")

    select_experiment(state, "exp-2")

    assert state.report_markdown is None
    assert state.report_error is None
    assert state.report_loaded_for is None
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `uv run python -m pytest tests/test_agent_orchestration_ui_report.py -k "report_statuses or record_report or selecting" -v`
Expected: FAIL — `ImportError: cannot import name 'REPORT_STATUSES'`

- [ ] **Step 3: models.py에 상태 집합을 더한다**

`POLLING_STATUSES` 아래에 넣는다.

```python
# 리포트 본문을 가질 수 있는 상태다. `record_experiment_result`가 `report_markdown`의
# 유일한 기록자이고 도달 상태로 `PASSED`를 하드코딩하며,
# `ALLOWED_TRANSITIONS[PASSED] = {PROMOTED}`라 PASSED에서 FAILED로 가는 간선이 없다.
# 그 두 사실이 이 집합의 근거이므로, 전이 그래프가 바뀌면 여기도 함께 넓힌다.
REPORT_STATUSES = frozenset(
    {ExperimentStatus.PASSED.value, ExperimentStatus.PROMOTED.value}
)
```

- [ ] **Step 4: state.py에 필드와 recorder를 더한다**

`WorkbenchState`에 필드 3개를 더한다.

```python
    # 조회한 리포트 본문. `None`은 "아직 안 받음"과 "받았는데 리포트가 없음" 두 가지로
    # 겹치므로, 둘을 가르는 것은 `report_loaded_for`다.
    report_markdown: str | None = None
    report_error: str | None = None
    # 리포트를 실제로 조회해 본 실험 id. **성공했을 때만** 세운다 — 실패에도 세우면
    # 일시적 오류 한 번에 그 세션 동안 리포트가 영구히 가려진다.
    report_loaded_for: str | None = None
```

`select_experiment`의 초기화 목록에 세 줄을 더한다.

```python
    state.report_markdown = None
    state.report_error = None
    state.report_loaded_for = None
```

`record_detail_error` 근처에 recorder를 더한다.

```python
def record_report(
    state: WorkbenchState,
    experiment_id: str,
    markdown_text: str | None,
) -> None:
    """조회한 리포트 본문과 조회 완료 표식을 기록한다.

    본문이 `None`이어도 표식을 세운다 — "리포트가 없다"는 것도 조회 결과이고, 매
    갱신마다 다시 물을 이유가 없다.
    """
    state.report_markdown = markdown_text
    state.report_error = None
    state.report_loaded_for = experiment_id


def record_report_error(state: WorkbenchState, message: str) -> None:
    """리포트 조회 실패만 기록한다.

    `report_loaded_for`를 **세우지 않아** 다음 갱신에서 다시 시도된다. `detail_error`를
    건드리지 않는 이유는 리포트 실패가 워크벤치 전체를 오류 상태로 만들면 안 되기
    때문이다(spec 결정 7).
    """
    state.report_error = message
```

state.py 모듈 docstring `[기능]`에 "리포트 본문 캐시와 조회 오류의 분리 보존"을 덧붙인다.

- [ ] **Step 5: client에 조회를 더한다**

`get_metadata` 아래에 넣는다.

```python
    def fetch_report(self, experiment_id: str) -> str | None:
        """실험 리포트 본문을 조회한다.

        `ExperimentResponse`가 아니라 전용 endpoint를 쓰는 이유는 본문이 수십 KB이고
        상세 조회는 5초마다 반복되기 때문이다.

        Returns:
            리포트 본문. 실험은 있고 리포트가 없으면 `None`이다 — 서버가 404가 아니라
            200 + null로 답하며, 404는 실험 자체가 없다는 뜻이라 예외로 올라간다.
        """
        payload = self._object(
            self._request_json("GET", f"/experiments/{experiment_id}/report")
        )
        value = payload.get("report_markdown")
        return None if value is None else str(value)
```

client.py 모듈 docstring `[기능]`에 "리포트 본문 조회"를 덧붙인다.

- [ ] **Step 6: 테스트가 통과하는지 확인한다**

Run: `uv run python -m pytest tests/test_agent_orchestration_ui_report.py -v`
Expected: PASS (14 passed)

- [ ] **Step 7: 커밋한다**

```bash
git add agent_orchestration/ui/client.py \
        agent_orchestration/ui/models.py \
        agent_orchestration/ui/state.py \
        tests/test_agent_orchestration_ui_report.py
git commit -m "feat: 워크벤치가 리포트 본문을 실험당 한 번 받아 둔다

report_loaded_for를 성공했을 때만 세워, 일시적 조회 실패가 그 세션 동안 리포트를
영구히 가리지 않게 한다.

Refs #647"
```

---

## Task 8: 결과 탭 렌더

**Files:**
- Modify: `agent_orchestration/ui/app.py` (`refresh_report`, `refresh_selected_experiment` 말미)
- Modify: `agent_orchestration/ui/views.py:319` (결과 탭), 헬퍼 신설
- Test: `tests/test_agent_orchestration_ui_report.py`

**Interfaces:**
- Consumes: `report_document` (Task 6), `fetch_report` / `record_report` / `record_report_error` / `REPORT_STATUSES` (Task 7)
- Produces: `app.refresh_report(client: ExperimentClient, state: WorkbenchState) -> None`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
from agent_orchestration.ui.app import refresh_report  # noqa: E402
from agent_orchestration.ui.client import ApiUnavailableError  # noqa: E402
from agent_orchestration.ui.models import Experiment  # noqa: E402


class _StubClient:
    """`fetch_report`만 답하는 최소 client."""

    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0

    def fetch_report(self, experiment_id: str) -> str | None:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _state_with(status: str) -> WorkbenchState:
    """선택 실험이 주어진 상태인 workbench state를 만든다."""
    state = WorkbenchState(selected_id="exp-1")
    state.experiment = Experiment(
        id="exp-1",
        hypothesis="가설",
        status=status,
        metric_summary=None,
        agent_session_id=None,
        created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    return state


def test_refresh_report_does_not_query_before_the_experiment_completes() -> None:
    """PASSED 이전에는 리포트가 반드시 없으므로 묻지 않는다."""
    client = _StubClient("# 결론")
    state = _state_with("EVALUATING")

    refresh_report(client, state)

    assert client.calls == 0
    assert state.report_loaded_for is None


def test_refresh_report_fetches_once_and_stops() -> None:
    """성공하면 한 번으로 그친다 — 5초 polling에 태우지 않는다."""
    client = _StubClient("# 결론")
    state = _state_with("PASSED")

    refresh_report(client, state)
    refresh_report(client, state)

    assert client.calls == 1
    assert state.report_markdown == "# 결론"


def test_refresh_report_retries_after_a_failure() -> None:
    """실패는 다음 갱신에서 다시 시도된다."""
    client = _StubClient(ApiUnavailableError("일시적 오류"))
    state = _state_with("PASSED")

    refresh_report(client, state)
    refresh_report(client, state)

    assert client.calls == 2
    assert state.report_error is not None


def test_refresh_report_failure_does_not_touch_the_detail_error() -> None:
    """리포트 실패가 워크벤치 전체를 오류 상태로 만들지 않는다."""
    client = _StubClient(ApiUnavailableError("일시적 오류"))
    state = _state_with("PASSED")

    refresh_report(client, state)

    assert state.detail_error is None
```

`AppTest` 다섯 조합 테스트를 더한다. `tests/test_ui_submission_app.py`의 `_StubHandler` 패턴을 그대로 복제해 `/experiments/{id}/report` 라우트를 추가하고, 각 조합에서 `app.exception`이 비어 있는지 본다.

```python
@pytest.mark.parametrize(
    ("metric_summary", "report_body", "report_status"),
    [
        pytest.param(SNAPSHOT_FIXTURE, None, 200, id="지표만"),
        pytest.param(None, "# 결론", 200, id="리포트만"),
        pytest.param(SNAPSHOT_FIXTURE, "# 결론", 200, id="둘_다"),
        pytest.param(None, None, 200, id="둘_다_없음"),
        pytest.param(SNAPSHOT_FIXTURE, None, 503, id="fetch_실패"),
    ],
)
def test_results_tab_survives_every_combination(
    metric_summary: dict | None, report_body: str | None, report_status: int
) -> None:
    """다섯 조합 어디서도 결과 탭이 죽지 않는다."""
    app = _rendered_workbench(
        metric_summary=metric_summary,
        report_body=report_body,
        report_status=report_status,
    )

    assert not app.exception
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `uv run python -m pytest tests/test_agent_orchestration_ui_report.py -k refresh_report -v`
Expected: FAIL — `ImportError: cannot import name 'refresh_report'`

- [ ] **Step 3: app.py에 조회 배선을 더한다**

import에 `REPORT_STATUSES`, `record_report`, `record_report_error`를 더하고 `refresh_selected_experiment` 아래에 넣는다.

```python
def refresh_report(client: ExperimentClient, state: WorkbenchState) -> None:
    """완주한 실험의 리포트 본문을 한 번만 받아 온다.

    **실패를 `report_error`에만 담는다.** `detail_error`를 세우지 않고,
    `remove_selected_experiment`를 부르지 않고, 갱신을 중단시키지 않는다 — metadata는
    실패 시 갱신 전체를 접지만 리포트는 그러면 안 된다. 그러지 않으면 리포트 조회
    하나가 5초마다 워크벤치 전체를 오류 상태로 만든다(spec 결정 7).

    `ApiNotFoundError`도 여기서는 실험 제거로 올리지 않는다 — 실험이 정말 없다면 바로
    앞의 `get_experiment`가 이미 그렇게 처리한 뒤다.
    """
    experiment = state.experiment
    if experiment is None or state.selected_id is None:
        return
    if experiment.status not in REPORT_STATUSES:
        return
    if state.report_loaded_for == state.selected_id:
        return
    try:
        record_report(state, state.selected_id, client.fetch_report(state.selected_id))
    except ExperimentApiError as error:
        record_report_error(state, str(error))
```

`refresh_selected_experiment`의 말미를 고친다. **맨 끝**이어야 한다.

```python
    record_terminal_refresh(state)
    state.detail_error = None
    state.last_updated_at = datetime.now(timezone.utc)
    # 맨 끝이다. 여기서 무엇이 나도 위의 갱신 결과와 반환값을 바꾸지 않는다.
    refresh_report(client, state)
    return False
```

- [ ] **Step 4: views.py 결과 탭을 교체한다**

import에 `report_document`, `REPORT_STATUSES`를 더한다. `_render_tabs`의 결과 탭 줄을 바꾼다.

```python
    with results_tab:
        _render_results(state)
```

`_render_metrics` **위**에 헬퍼를 더한다. `_render_metrics` 자체는 오른쪽 "실행 요약" 패널(`views.py:255`)이 계속 쓰므로 **그대로 둔다.**

```python
# 결과 탭이 카드로 그리는 지표와 화면 이름. 낮을수록 좋은 지표는 delta 색을 뒤집는다.
_METRIC_CARDS: tuple[tuple[str, str, bool], ...] = (
    ("roc_auc", "ROC-AUC", False),
    ("log_loss", "LogLoss", True),
    ("brier", "Brier", True),
)

# 리포트 iframe의 고정 높이(px). 자동 리사이즈를 넣지 않는다 — 실패하면 리포트가 안
# 보이는 실패 모드만 늘어난다. Streamlit이 srcdoc iframe의 스크롤을 항상 켜므로 긴
# 리포트는 내부에서 스크롤된다.
_REPORT_HEIGHT_PX = 620


def _render_results(state: WorkbenchState) -> None:
    """결과 탭에 경고·지표 카드·리포트 본문을 그린다.

    **지표와 리포트의 결손은 독립이다** — 리포트가 없어도 카드는 나오고, 지표가 없어도
    본문은 나온다. 지표를 iframe 밖 Streamlit 위젯으로 두는 이유가 그것이다.
    """
    metrics = state.experiment.metric_summary if state.experiment else None
    _render_split_warning(metrics)
    _render_metric_cards(metrics)
    _render_report(state)


def _render_split_warning(metrics: dict[str, object] | None) -> None:
    """두 조건의 테스트셋이 갈렸으면 지표보다 **위**에 경고한다.

    숫자는 멀쩡해 보이므로 경고가 지표 아래에 있으면 읽는 사람이 delta를 먼저 믿는다.
    """
    if not metrics or metrics.get("split_matches") is not False:
        return
    st.warning(
        "두 조건의 테스트셋이 다릅니다 — 이 delta는 변경의 효과로 읽을 수 없습니다."
    )


def _render_metric_cards(metrics: dict[str, object] | None) -> None:
    """조건별 평균과 짝지은 delta를 카드로 그린다.

    숫자는 전부 `metric_summary`에서 온다 — **에이전트가 쓴 텍스트를 파싱하지 않는다.**
    seed별 delta는 요약에 없고 전문(GCS)에만 있으므로 그리지 않는다.
    """
    if not metrics:
        st.caption("아직 평가 전입니다.")
        return
    conditions = metrics.get("conditions")
    paired = metrics.get("paired")
    if not isinstance(conditions, dict) or not isinstance(paired, dict):
        st.caption("지표 요약 형식을 읽을 수 없습니다.")
        return
    candidate = conditions.get("candidate")
    baseline = conditions.get("baseline")
    columns = st.columns(len(_METRIC_CARDS))
    for column, (name, label, lower_is_better) in zip(columns, _METRIC_CARDS):
        summary = paired.get(name)
        mean = summary.get("mean") if isinstance(summary, dict) else None
        error = summary.get("standard_error") if isinstance(summary, dict) else None
        value = candidate.get(name) if isinstance(candidate, dict) else None
        with column:
            st.metric(
                label,
                f"{float(value):.4f}" if isinstance(value, (int, float)) else "—",
                delta=f"{float(mean):+.4f}" if isinstance(mean, (int, float)) else None,
                delta_color="inverse" if lower_is_better else "normal",
            )
            # seed가 하나면 표본 표준편차가 정의되지 않아 `None`이다. 0으로 보이면
            # "변동이 없다"로 읽히므로 표기 자체를 생략한다.
            if isinstance(error, (int, float)):
                st.caption(f"표준오차 ±{float(error):.4f}")
            if isinstance(baseline, dict) and isinstance(baseline.get(name), (int, float)):
                st.caption(f"baseline {float(baseline[name]):.4f}")


def _render_report(state: WorkbenchState) -> None:
    """리포트 본문을 고정 높이 iframe에 그린다.

    네 갈래를 구분한다. `report_error`가 최우선인 이유는, 실패했는데 "리포트가
    없습니다"로 보이면 **없는 것과 못 받은 것이 구별되지 않기** 때문이다.
    """
    if state.report_error is not None:
        st.warning(f"리포트를 불러오지 못했습니다 — {state.report_error}")
        return
    if state.experiment is None or state.experiment.status not in REPORT_STATUSES:
        return
    if state.report_loaded_for != state.selected_id:
        st.caption("리포트를 불러오는 중입니다.")
        return
    if not state.report_markdown:
        st.caption("아직 리포트가 없습니다.")
        return
    st.iframe(report_document(state.report_markdown), height=_REPORT_HEIGHT_PX)
```

views.py 모듈 docstring `[기능]`에 "결과 탭의 지표 카드와 리포트 HTML 렌더"를 덧붙인다.

- [ ] **Step 5: 테스트가 통과하는지 확인한다**

Run: `uv run python -m pytest tests/test_agent_orchestration_ui_report.py -v`
Expected: PASS (23 passed)

- [ ] **Step 6: 전체 테스트와 lint를 돌린다**

Run: `uv run python -m pytest`
Expected: Task 시작 전 baseline 대비 실패가 늘지 않았다. 늘었으면 멈추고 원인을 본다.

Run: `uv run --no-sync ruff check agent_orchestration autoresearch tests tools`
Expected: `All checks passed!`

- [ ] **Step 7: 커밋한다**

```bash
git add agent_orchestration/ui/app.py \
        agent_orchestration/ui/views.py \
        tests/test_agent_orchestration_ui_report.py
git commit -m "feat: 결과 탭에 지표 카드와 리포트 HTML을 그린다

경고를 지표 위에 두고, 리포트 본문만 고정 높이 iframe에 넣는다. 리포트 조회 실패는
report_error에만 담겨 워크벤치 갱신을 중단시키지 않는다.

Refs #647"
```

---

## Task 9: 문서 마무리

**Files:**
- Modify: `docs/specs/2026-08-10-experiment-report-html-workbench.md` (상태 갱신)
- Modify: `docs/plans/2026-08-10-experiment-report-html-workbench.md` (검증 결과)

- [ ] **Step 1: spec 상태를 갱신한다**

머리말의 `상태: 초안`을 `상태: 구현 완료 (#647)`로 바꾸고, `## 검증` 절 아래에 실측 결과를 적는다 — 전체 테스트 수, baseline 대비 실패 증감, 실제로 확인한 화면.

- [ ] **Step 2: 계획의 미완 항목을 확인한다**

이 문서의 모든 체크박스가 채워졌는지 본다. 남은 것이 있으면 그 이유를 spec 「범위 밖」에 적는다.

- [ ] **Step 3: 커밋한다**

```bash
git add docs/specs/2026-08-10-experiment-report-html-workbench.md \
        docs/plans/2026-08-10-experiment-report-html-workbench.md
git commit -m "docs: 리포트 렌더 구현 결과를 spec과 plan에 반영한다

Refs #647"
```

- [ ] **Step 4: 이슈 정정 코멘트 — 사용자 확인 후에만 한다**

`#647` 본문의 네 항목(seed별 delta, `components.html`의 origin 격리, deferred 누락, 상한 검증의 거절 여부)이 코드 실측과 어긋난다. 저장소 관례는 원문을 지우지 않고 `[정정 — #647, 2026-08-10]` 태그를 단 코멘트를 덧붙이는 것이다.

**이것은 바깥으로 나가는 동작이므로 실행자가 임의로 하지 않는다.** 사용자에게 코멘트 문안을 보여 주고 승인받은 뒤에만 올린다.

---

## Self-Review

**Spec 커버리지**

| spec | task |
| --- | --- |
| 결정 1 — 보고/조회 분리, deferred, 404 vs 200+null | 1, 2, 3 |
| 결정 2 — 별도 트랜잭션, write-once + WARNING | 2 |
| 결정 3 — 거절 경로 없음, service 정규화, executor 잘림, 상수 일치 | 2, 4 |
| 결정 4 — UI 변환, 우리 템플릿, escape 실측표 | 6 |
| 결정 5 — iframe은 격리 아님, `st.iframe`, 의존성 | 5, 6, 8 |
| 결정 6 — 지표 카드, 경고 위치, seed별 delta 제외, inspector 유지 | 8 |
| 결정 7 — 3필드, 4갈래 판정, 조회 시점, 잡는 위치 | 7, 8 |
| 검증 목록 전 항목 | 1~8의 테스트 단계 |

**타입 일관성** — `find_experiment_report`(Task 1)의 시그니처가 Task 2·3의 호출과 맞는다. `report_document`(Task 6)를 Task 8이 그 이름으로 부른다. `REPORT_STATUSES`(Task 7)를 Task 8이 `app.py`·`views.py` 양쪽에서 같은 이름으로 쓴다. `_ResultPayload.snapshot` / `.report_markdown`(Task 4)이 같은 Task 안에서만 쓰인다.

**남은 가정** — `_rendered_workbench` 헬퍼(Task 8 Step 1)는 `tests/test_ui_submission_app.py`의 `_StubHandler`·`_rendered_app` 패턴을 그대로 복제해 만든다. 그 파일이 이미 HTTP 스텁 서버로 워크벤치 전체를 띄우고 있으므로 새 패턴을 만들지 않는다.
