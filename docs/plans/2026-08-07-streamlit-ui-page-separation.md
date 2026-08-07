# Streamlit 가설 등록·상세 화면 분리 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Streamlit Workbench의 가설 등록 화면과 선택한 가설 상세 화면을 분리하고 사이드바에서 명시적으로 전환합니다.

**Architecture:** `WorkbenchState`에 `CREATE`·`DETAIL` 화면 모드를 추가해 Experiment 선택과 본문 화면을 독립적으로 관리합니다. `app.py`는 사이드바 의도를 상태 전이로 변환하고 현재 모드에 해당하는 본문만 렌더링하며, 기존 API client와 polling 계약은 유지합니다.

**Tech Stack:** Python 3.11/3.12, Streamlit, dataclasses, pytest, Ruff

## Global Constraints

- 최초 접속 화면은 `CREATE`입니다.
- 가설 추가 화면과 상세 화면을 동시에 렌더링하지 않습니다.
- 가설 추가 화면을 열어도 기존 Experiment 선택과 activity 캐시를 유지합니다.
- 가설 등록 성공 시 생성한 Experiment를 선택하고 `DETAIL`로 전환합니다.
- 실시간 polling은 `DETAIL` 화면에서만 수행합니다.
- Experiment API 요청·응답 계약은 변경하지 않습니다.

---

### Task 1: 화면 모드와 순수 상태 전이

**Files:**
- Modify: `agent_orchestration/ui/state.py`
- Modify: `tests/test_agent_orchestration_ui_step.py`

**Interfaces:**
- Consumes: 기존 `WorkbenchState`, `select_experiment(state, experiment_id)`
- Produces: `WorkbenchView` enum, `show_create_view(state) -> None`, `show_experiment(state, experiment_id) -> None`

- [ ] **Step 1: 실패하는 상태 전이 테스트 작성**

```python
from agent_orchestration.ui.state import (
    WorkbenchState,
    WorkbenchView,
    show_create_view,
    show_experiment,
)


def test_workbench_starts_on_create_view() -> None:
    assert WorkbenchState().view is WorkbenchView.CREATE


def test_show_create_view_preserves_selection_and_activity() -> None:
    state = WorkbenchState(selected_id="one")
    state.event_cursor = "event-1"

    show_create_view(state)

    assert state.view is WorkbenchView.CREATE
    assert state.selected_id == "one"
    assert state.event_cursor == "event-1"


def test_show_experiment_selects_experiment_and_opens_detail() -> None:
    state = WorkbenchState()

    show_experiment(state, "two")

    assert state.view is WorkbenchView.DETAIL
    assert state.selected_id == "two"
```

- [ ] **Step 2: 좁은 테스트가 실패하는지 실행**

Run: `uv run python -m pytest tests/test_agent_orchestration_ui_step.py -q`

Expected: `WorkbenchView`, `show_create_view`, `show_experiment` import 실패

- [ ] **Step 3: 화면 상태와 전이 구현**

```python
from enum import StrEnum


class WorkbenchView(StrEnum):
    CREATE = "CREATE"
    DETAIL = "DETAIL"


@dataclass
class WorkbenchState:
    view: WorkbenchView = WorkbenchView.CREATE
    # 기존 필드는 그대로 유지


def show_create_view(state: WorkbenchState) -> None:
    state.view = WorkbenchView.CREATE


def show_experiment(state: WorkbenchState, experiment_id: str) -> None:
    select_experiment(state, experiment_id)
    state.view = WorkbenchView.DETAIL
```

`state.py` 모듈 docstring의 기능 설명에 화면 모드 전이를 추가합니다.

- [ ] **Step 4: 상태 전이 테스트 통과 확인**

Run: `uv run python -m pytest tests/test_agent_orchestration_ui_step.py -q`

Expected: 전체 PASS

- [ ] **Step 5: 상태 전이 변경 커밋**

```bash
git add agent_orchestration/ui/state.py tests/test_agent_orchestration_ui_step.py
git commit -m "feat: Workbench 화면 상태 전이 추가"
```

---

### Task 2: 사이드바 탐색과 본문 화면 분기

**Files:**
- Modify: `agent_orchestration/ui/app.py`
- Modify: `agent_orchestration/ui/views.py`

**Interfaces:**
- Consumes: `WorkbenchView`, `show_create_view(state)`, `show_experiment(state, experiment_id)`
- Produces: `render_add_hypothesis_button() -> bool`, 모드별 단일 본문 렌더링

- [ ] **Step 1: 사이드바 추가 버튼 렌더러 구현**

```python
def render_add_hypothesis_button() -> bool:
    """sidebar 최상단의 가설 추가 화면 전환 버튼을 렌더링한다."""
    return st.sidebar.button(
        "+ 가설 추가하기",
        type="primary",
        use_container_width=True,
    )
```

`views.py` 모듈 docstring을 등록 화면과 상세 화면을 분리해 렌더링한다는 책임으로 갱신하고, 빈 상세 안내 문구에서 `상단에서 가설을 등록` 표현을 제거합니다.

- [ ] **Step 2: 앱에서 사이드바 의도를 화면 상태로 연결**

```python
if render_add_hypothesis_button():
    show_create_view(state)
    st.rerun()

selected_id = render_experiment_list(state.experiments, state.selected_id)
if selected_id is not None and (
    selected_id != state.selected_id or state.view is not WorkbenchView.DETAIL
):
    show_experiment(state, selected_id)
    st.rerun()
```

목록 새로고침과 오류 표시는 기존 사이드바 위치와 동작을 유지합니다.

- [ ] **Step 3: 본문을 현재 모드 하나로 분기**

```python
if state.view is WorkbenchView.CREATE:
    submitted_hypothesis = render_hypothesis_composer(state.detail_error)
    if submitted_hypothesis is not None:
        if client is None:
            st.error("Experiment API 연결을 먼저 복구해 주세요.")
        else:
            create_from_hypothesis(client, state, submitted_hypothesis)
            st.rerun()
    return

if state.selected_id is None:
    render_empty_workbench()
    return
```

`create_from_hypothesis`의 성공 경로는 `show_experiment(state, experiment.id)`를 사용해 생성 직후 `DETAIL`로 전환합니다. `live_workbench()` fragment는 위 분기 이후에만 선언·호출하여 `CREATE`에서 polling하지 않습니다. `app.py` 모듈 docstring도 분리된 화면 탐색 책임을 반영합니다.

- [ ] **Step 4: 정적 검사와 관련 회귀 테스트 실행**

Run: `uv run --no-sync ruff check agent_orchestration/ui/app.py agent_orchestration/ui/state.py agent_orchestration/ui/views.py tests/test_agent_orchestration_ui_step.py`

Expected: PASS

Run: `uv run python -m pytest tests/test_agent_orchestration_ui_step.py tests/test_agent_orchestration_ui_time.py -q`

Expected: 전체 PASS

- [ ] **Step 5: UI 화면 분리 변경 커밋**

```bash
git add agent_orchestration/ui/app.py agent_orchestration/ui/views.py
git commit -m "feat: 가설 등록과 상세 화면 분리"
```

---

### Task 3: 문서 수명 정리와 최종 검증

**Files:**
- Move: `docs/specs/2026-08-07-streamlit-ui-page-separation.md` to `docs/archive/specs/2026-08-07-streamlit-ui-page-separation.md`
- Move: `docs/plans/2026-08-07-streamlit-ui-page-separation.md` to `docs/archive/plans/2026-08-07-streamlit-ui-page-separation.md`

**Interfaces:**
- Consumes: Task 1과 Task 2의 완성된 UI 동작
- Produces: 구현 완료 문서의 archive 배치와 PR 전 검증 결과

- [ ] **Step 1: 완료된 spec과 plan을 archive로 이동**

```bash
git mv docs/specs/2026-08-07-streamlit-ui-page-separation.md docs/archive/specs/2026-08-07-streamlit-ui-page-separation.md
git mv docs/plans/2026-08-07-streamlit-ui-page-separation.md docs/archive/plans/2026-08-07-streamlit-ui-page-separation.md
```

- [ ] **Step 2: 최종 좁은 검증 실행**

Run: `uv run --no-sync ruff check agent_orchestration/ui/app.py agent_orchestration/ui/state.py agent_orchestration/ui/views.py tests/test_agent_orchestration_ui_step.py`

Expected: PASS

Run: `uv run python -m pytest tests/test_agent_orchestration_ui_step.py tests/test_agent_orchestration_ui_time.py -q`

Expected: 전체 PASS

Run: `git diff --check origin/main...HEAD`

Expected: 출력 없음

- [ ] **Step 3: 문서 이동 커밋**

```bash
git add docs/archive/specs/2026-08-07-streamlit-ui-page-separation.md docs/archive/plans/2026-08-07-streamlit-ui-page-separation.md
git commit -m "docs: Workbench 화면 분리 문서 보관"
```
