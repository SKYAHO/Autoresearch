"""Streamlit workbench의 순수 session state 전이를 제공한다.

[파이프라인]
Experiment API에서 받은 목록·상세·Event·Log를 화면 상태로 누적하는 구간이다. API 호출과
Streamlit widget 렌더링은 담당하지 않는다.

[기능]
Experiment 선택, cursor 기반 Event/Log 누적, terminal 상태의 마지막 갱신, 오류 보존을
제공한다.

[비책임]
HTTP 요청, API 인증, 상태 전이 기록, Agent 실행, GitHub 이슈 처리.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from agent_orchestration.ui.models import Event, Experiment, Log, POLLING_STATUSES, TERMINAL_STATUSES


@dataclass
class WorkbenchState:
    """선택 Experiment와 incremental polling 상태."""

    experiments: list[Experiment] = field(default_factory=list)
    selected_id: str | None = None
    experiment: Experiment | None = None
    events: list[Event] = field(default_factory=list)
    logs: list[Log] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    event_cursor: str | None = None
    log_cursor: str | None = None
    metadata_loaded_for: str | None = None
    terminal_refresh_complete: bool = False
    last_error: str | None = None
    last_updated_at: datetime | None = None


def select_experiment(state: WorkbenchState, experiment_id: str | None) -> None:
    """새 Experiment 선택 시 cursor와 상세 캐시를 초기화한다."""
    if state.selected_id == experiment_id:
        return
    state.selected_id = experiment_id
    state.experiment = None
    state.events.clear()
    state.logs.clear()
    state.metadata.clear()
    state.event_cursor = None
    state.log_cursor = None
    state.metadata_loaded_for = None
    state.terminal_refresh_complete = False
    state.last_error = None


def append_event_page(
    state: WorkbenchState,
    events: list[Event],
    next_cursor: str | None,
) -> None:
    """중복 없이 Event page를 누적한다."""
    known_ids = {event.id for event in state.events}
    state.events.extend(event for event in events if event.id not in known_ids)
    if next_cursor is not None:
        state.event_cursor = next_cursor


def append_log_page(
    state: WorkbenchState,
    logs: list[Log],
    next_cursor: str | None,
) -> None:
    """중복 없이 Log page를 누적한다."""
    known_ids = {log.id for log in state.logs}
    state.logs.extend(log for log in logs if log.id not in known_ids)
    if next_cursor is not None:
        state.log_cursor = next_cursor


def should_poll(state: WorkbenchState) -> bool:
    """현재 선택 Experiment가 polling 대상인지 판단한다."""
    if state.experiment is None:
        return False
    if state.experiment.status in POLLING_STATUSES:
        return True
    if state.experiment.status in TERMINAL_STATUSES:
        return not state.terminal_refresh_complete
    return False


def mark_terminal_refresh_complete(state: WorkbenchState) -> None:
    """terminal 상태의 마지막 Event/Log 조회 완료를 기록한다."""
    if state.experiment is not None and state.experiment.status in TERMINAL_STATUSES:
        state.terminal_refresh_complete = True


def record_refresh_error(state: WorkbenchState, message: str) -> None:
    """마지막 성공 데이터를 유지한 채 조회 오류를 기록한다."""
    state.last_error = message
