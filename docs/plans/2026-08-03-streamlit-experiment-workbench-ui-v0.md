# Streamlit Experiment Workbench UI v0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Streamlit workbench that creates a v0 Experiment from one hypothesis and displays Experiment API state, metrics, events, metadata, and raw logs.

**Architecture:** Streamlit is an Experiment API consumer. A dedicated client owns authenticated HTTP and cursor pagination; pure state functions own selection and polling lifecycle; views only render typed display data. The UI writes only `POST /experiments`; it never writes status, events, logs, promotion, GitHub issues, or `/chat`.

**Tech Stack:** Python 3.12, Streamlit, standard-library HTTP client, FastAPI Experiment API v0, pytest, uv.

## Global Constraints

- `ORCH_UI_API_BASE_URL` defaults to `http://127.0.0.1:8000`; `ORCH_UI_API_TOKEN` is required.
- `X-Orch-Token` is sent only by the Streamlit server. It must never be rendered in the browser.
- `CREATED`, `RUNNING`, `EVALUATING`, and `PASSED` refresh every second. `FAILED`, `ERROR`, and `PROMOTED` fetch Event/Log once more, then stop.
- Event and Log requests use the API's `next_cursor` value as the next `after_id`.
- GitHub `[AR]` issue creation, Experiment lineage, execution orchestration, result reporting, and GKE UI deployment are outside this issue.
- Local integration uses `kubectl -n autoresearch port-forward service/agent-orchestration-api 8000:8000`.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `agent_orchestration/ui/__init__.py` | UI package boundary. |
| `agent_orchestration/ui/models.py` | Immutable API display models and status constants. |
| `agent_orchestration/ui/client.py` | HTTP, API authentication, JSON conversion, and error classification. |
| `agent_orchestration/ui/state.py` | Selected Experiment, Event/Log cursors, cached records, and polling lifecycle. |
| `agent_orchestration/ui/views.py` | Start screen and desktop/mobile workbench rendering. |
| `agent_orchestration/ui/styles.py` | Status colors, labels, and safe log presentation helpers. |
| `agent_orchestration/ui/app.py` | Streamlit entry point and one-second fragment polling. |
| `tests/test_agent_orchestration_ui_client.py` | Client request, parsing, and API error tests. |
| `tests/test_agent_orchestration_ui_state.py` | Selection, cursor, append, and terminal polling tests. |
| `tests/test_agent_orchestration_ui_views.py` | Status copy, metric empty state, and timeline formatting tests. |
| `pyproject.toml`, `uv.lock` | Streamlit dependency. |
| `.env.example`, `agent_orchestration/README.md` | Local configuration and port-forward runbook. |

## Shared Interfaces

```python
@dataclass(frozen=True)
class Experiment:
    id: str
    hypothesis: str
    status: str
    metric_summary: dict[str, object] | None
    agent_session_id: str | None
    created_at: datetime
    updated_at: datetime

@dataclass(frozen=True)
class Event:
    id: str
    experiment_id: str
    from_status: str | None
    to_status: str
    reason: str | None
    metric_snapshot: dict[str, object] | None
    created_at: datetime

@dataclass(frozen=True)
class Log:
    id: str
    experiment_id: str
    log_type: str
    content: str
    created_at: datetime

class ExperimentClient:
    def create_experiment(self, hypothesis: str) -> Experiment: ...
    def list_experiments(self, *, limit: int = 50) -> list[Experiment]: ...
    def get_experiment(self, experiment_id: str) -> Experiment: ...
    def get_events(self, experiment_id: str, after_id: str | None) -> tuple[list[Event], str | None]: ...
    def get_logs(self, experiment_id: str, after_id: str | None) -> tuple[list[Log], str | None]: ...
    def get_metadata(self, experiment_id: str) -> dict[str, str]: ...
```

### Task 1: Add Streamlit configuration and typed API client

**Files:**
- Create: `agent_orchestration/ui/__init__.py`
- Create: `agent_orchestration/ui/models.py`
- Create: `agent_orchestration/ui/client.py`
- Create: `tests/test_agent_orchestration_ui_client.py`
- Modify: `pyproject.toml`, `uv.lock`, `.env.example`, `agent_orchestration/README.md`

**Consumes:** `POST /experiments`, Experiment list/detail, Event/Log pages, metadata endpoint.

**Produces:** Typed `Experiment`, `Event`, `Log`; `ExperimentClient`; `ApiUnauthorizedError`, `ApiNotFoundError`, `ApiValidationError`, `ApiUnavailableError`.

- [ ] Write tests for required token, default API URL, `POST /experiments` payload, `X-Orch-Token` header, Event/Log `after_id`, and `401`/`404`/`422`/`5xx` exception conversion.
- [ ] Implement `ExperimentClient.from_environment()` and reject a missing `ORCH_UI_API_TOKEN` before network access.
- [ ] Use `urllib.request`; parse only expected JSON fields; strip and reject empty hypotheses before `POST /experiments` with `{"hypothesis": hypothesis, "metadata": {}}`.
- [ ] Add Streamlit to the orchestration dependency group, regenerate `uv.lock`, and document blank `ORCH_UI_API_TOKEN` and local `ORCH_UI_API_BASE_URL` in `.env.example`.
- [ ] Document that port-forward is required for local access to the GKE ClusterIP API.

### Task 2: Add pure workbench state and polling reducer

**Files:**
- Create: `agent_orchestration/ui/state.py`
- Create: `tests/test_agent_orchestration_ui_state.py`

**Consumes:** Typed models from Task 1.

**Produces:** `WorkbenchState`, `select_experiment()`, `append_event_page()`, `append_log_page()`, `should_poll()`, `mark_terminal_refresh_complete()`.

- [ ] Write tests proving selection clears Event/Log cursors and cached records, duplicate IDs do not render twice, and a non-null `next_cursor` replaces the cursor.
- [ ] Define `POLLING_STATUSES = {"CREATED", "RUNNING", "EVALUATING", "PASSED"}` and `TERMINAL_STATUSES = {"FAILED", "ERROR", "PROMOTED"}`.
- [ ] Make terminal status poll once until Event/Log have been refreshed, then stop. Keep last successful records if a later refresh raises `ApiUnavailableError`.
- [ ] Record a concise `last_error` string for rendering; do not put raw exception or request headers into session state.

### Task 3: Render the approved start screen and workbench

**Files:**
- Create: `agent_orchestration/ui/views.py`
- Create: `agent_orchestration/ui/styles.py`
- Create: `tests/test_agent_orchestration_ui_views.py`

**Consumes:** `Experiment`, `Event`, `Log`, `WorkbenchState`, and callbacks provided by `app.py`.

**Produces:** `render_start_screen()`, `render_experiment_list()`, `render_workbench()`, `status_display()`, `status_color()`.

- [ ] Write tests that distinguish `FAILED` as a rejected hypothesis from `ERROR` as a system failure, show an explicit empty metric message, and render Event transitions with reason text.
- [ ] Render the start screen with one large hypothesis field, API connection indicator, submit state, and concise error message.
- [ ] Render the desktop workbench with left Experiment list, central hypothesis/progress/results-events-logs tabs, and right status/metrics/metadata summary.
- [ ] Use a warm paper background, deep ink copy, and muted gray/blue/amber/green/red/teal status palette. Render raw logs through Streamlit text components only, never unsafe HTML.
- [ ] On narrow screens, render an Experiment selector above the central area and stack the summary under the hypothesis.

### Task 4: Compose Streamlit routing and incremental refresh

**Files:**
- Create: `agent_orchestration/ui/app.py`
- Modify: `tests/test_agent_orchestration_ui_state.py`
- Modify: `tests/test_agent_orchestration_ui_views.py`

**Consumes:** `ExperimentClient`, `WorkbenchState`, and Task 3 view functions.

**Produces:** Runnable `PYTHONPATH=. uv run streamlit run agent_orchestration/ui/app.py` application.

- [ ] Write tests for `refresh_selected_experiment()` asserting this order: detail, incremental Events, incremental Logs, then metadata only for a newly selected Experiment.
- [ ] On a `404` detail response, clear the selected Experiment but preserve the list and render a user-facing message.
- [ ] Initialize Streamlit session state once, load the recent 50 Experiments, and select the Experiment returned from a successful `create_experiment()` call.
- [ ] Use `@st.fragment(run_every="1s")` only while `should_poll()` is true. Use stored cursors for Event/Log calls and stop after the final terminal refresh.
- [ ] Display API validation, authorization, unavailable, and empty-list states without a traceback.

### Task 5: Finish local runbook and validate the UI boundary

**Files:**
- Modify: `agent_orchestration/README.md`
- Modify: `tests/test_agent_orchestration_ui_client.py`
- Modify: `tests/test_agent_orchestration_ui_state.py`
- Modify: `tests/test_agent_orchestration_ui_views.py`

**Consumes:** Completed Streamlit UI and deployed Experiment API port-forward.

**Produces:** Reproducible local run instructions and regression coverage for failure/terminal paths.

- [ ] Add tests for a terminal Run receiving a final late Log and for a refresh error preserving already rendered Event/Log data.
- [ ] Document this sequence:

```bash
kubectl -n autoresearch port-forward service/agent-orchestration-api 8000:8000
ORCH_UI_API_BASE_URL=http://127.0.0.1:8000 \
ORCH_UI_API_TOKEN="$ORCH_UI_API_TOKEN" \
PYTHONPATH=. uv run streamlit run agent_orchestration/ui/app.py
```

- [ ] State explicitly that the v0 submit path creates only a DB Experiment. It does not create a GitHub `[AR]` issue or start the future execution runner.
- [ ] With explicit user approval, run the focused UI tests, `ruff check agent_orchestration tests`, the required repository test scope, and `git diff --check` before review.

## Plan Self-Review

- Tasks 1 through 4 cover API access, the approved two-screen design, cursor polling, terminal lifecycle, and UI-safe errors.
- Task 5 covers port-forward documentation and the v0 boundary with the future GitHub-issue and execution work.
- Every shared type and function used by later tasks is defined in the Shared Interfaces or the producing task.
