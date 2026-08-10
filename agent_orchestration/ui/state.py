"""Streamlit workbench의 순수 session state 전이를 제공한다.

[파이프라인]
Experiment API에서 받은 목록·상세·Event·Log를 화면 상태로 누적하는 구간이다. API 호출과
Streamlit widget 렌더링은 담당하지 않는다.

[기능]
Experiment 선택, cursor 기반 Event/Log 누적, terminal 상태의 추가 최종 갱신, 목록·상세
오류 분리 보존, 화면 모드 전이와 이슈 발행 재시도·취소 상태 전이를 제공한다. 리포트 본문
캐시와 조회 오류의 분리 보존도 포함한다.

[비책임]
HTTP 요청, API 인증, 상태 전이 기록, Agent 실행, GitHub 이슈 처리.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from agent_orchestration.ui.models import (
    Event,
    Experiment,
    IssuePublication,
    Log,
    POLLING_STATUSES,
    Step,
    Submission,
    TERMINAL_STATUSES,
)


class WorkbenchView(StrEnum):
    """Workbench가 표시할 화면 모드."""

    CREATE = "CREATE"
    DETAIL = "DETAIL"


@dataclass
class WorkbenchState:
    """선택 Experiment와 incremental polling 상태."""

    view: WorkbenchView = WorkbenchView.CREATE
    experiments: list[Experiment] = field(default_factory=list)
    selected_id: str | None = None
    experiment: Experiment | None = None
    events: list[Event] = field(default_factory=list)
    logs: list[Log] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    # 페이지 예산에 걸려 뒷부분을 못 읽었는지. 화면에 반드시 드러낸다.
    steps_truncated: bool = False
    metadata: dict[str, str] = field(default_factory=dict)
    event_cursor: str | None = None
    log_cursor: str | None = None
    metadata_loaded_for: str | None = None
    terminal_status_observed: str | None = None
    terminal_refresh_complete: bool = False
    list_error: str | None = None
    detail_error: str | None = None
    last_updated_at: datetime | None = None
    # 방금 발행한 이슈 좌표. 선택 변경이나 다음 제출 때 지워 이전 결과가 남지 않게 한다.
    last_publication: IssuePublication | None = None
    # 생성 성공·발행 실패 사이의 부분 성공을 보존해 재제출 시 Experiment 중복 생성을 막는다.
    pending_publication_experiment_id: str | None = None
    pending_publication_submission: Submission | None = None
    # 조회한 리포트 본문. `None`은 "아직 안 받음"과 "받았는데 리포트가 없음" 두 가지로
    # 겹치므로, 둘을 가르는 것은 `report_loaded_for`다.
    report_markdown: str | None = None
    report_error: str | None = None
    # 리포트를 실제로 조회해 본 실험 id. **성공했을 때만** 세운다 — 실패에도 세우면
    # 일시적 오류 한 번에 그 세션 동안 리포트가 영구히 가려진다.
    report_loaded_for: str | None = None


def show_create_view(state: WorkbenchState) -> None:
    """Workbench를 Experiment 생성 화면으로 전환한다."""
    state.view = WorkbenchView.CREATE


def discard_pending_publication(state: WorkbenchState) -> None:
    """실패한 이슈 발행 대기와 관련 오류만 정리한다."""
    state.pending_publication_experiment_id = None
    state.pending_publication_submission = None
    state.detail_error = None


def show_experiment(state: WorkbenchState, experiment_id: str) -> None:
    """Experiment를 선택하고 상세 화면으로 전환한다."""
    select_experiment(state, experiment_id)
    state.view = WorkbenchView.DETAIL


def select_experiment(state: WorkbenchState, experiment_id: str | None) -> None:
    """새 Experiment 선택 시 cursor와 상세 캐시를 초기화한다."""
    if state.selected_id == experiment_id:
        return
    state.selected_id = experiment_id
    state.experiment = None
    state.events.clear()
    state.logs.clear()
    state.steps.clear()
    state.steps_truncated = False
    state.metadata.clear()
    state.event_cursor = None
    state.log_cursor = None
    state.metadata_loaded_for = None
    state.terminal_status_observed = None
    state.terminal_refresh_complete = False
    state.detail_error = None
    state.last_publication = None
    state.report_markdown = None
    state.report_error = None
    state.report_loaded_for = None


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


def merge_steps(
    state: WorkbenchState,
    steps: list[Step],
    *,
    truncated: bool = False,
) -> None:
    """조회한 Step 전체를 기존 목록에 병합한다.

    Step은 Event·Log와 달리 **PATCH로 갱신되는 mutable 리소스**다. 같은 id가 다시 오면
    새 row가 아니라 진행 상태가 바뀐 것이므로, 무시하지 않고 최신 값으로 덮어쓴다.

    **cursor를 상태로 들고 있지 않는다.** `after_id`는 cursor 뒤의 row만 돌려주므로,
    갱신과 갱신 사이에 cursor를 유지하면 이미 받은 Step의 상태 변화를 관측하지 못한다.
    페이지 넘김은 `ExperimentClient.get_steps()`가 한 번의 갱신 안에서만 처리한다.
    """
    by_id = {step.id: index for index, step in enumerate(state.steps)}
    for step in steps:
        existing = by_id.get(step.id)
        if existing is None:
            by_id[step.id] = len(state.steps)
            state.steps.append(step)
        else:
            state.steps[existing] = step
    state.steps_truncated = truncated


def clear_activity_cache(state: WorkbenchState) -> None:
    """만료된 cursor를 재조회할 수 있도록 Event·Log·Step 캐시를 비운다."""
    state.events.clear()
    state.logs.clear()
    state.steps.clear()
    state.steps_truncated = False
    state.event_cursor = None
    state.log_cursor = None


def should_poll(state: WorkbenchState) -> bool:
    """현재 선택 Experiment가 polling 대상인지 판단한다."""
    if state.experiment is None:
        return False
    if state.experiment.status in POLLING_STATUSES:
        return True
    if state.experiment.status in TERMINAL_STATUSES:
        return not state.terminal_refresh_complete
    return False


def record_terminal_refresh(state: WorkbenchState) -> None:
    """종료 상태를 한 번 더 polling한 뒤에만 갱신 완료를 기록한다."""
    if state.experiment is None or state.experiment.status not in TERMINAL_STATUSES:
        state.terminal_status_observed = None
        state.terminal_refresh_complete = False
        return
    if state.terminal_status_observed == state.experiment.status:
        state.terminal_refresh_complete = True
        return
    state.terminal_status_observed = state.experiment.status
    state.terminal_refresh_complete = False


def record_list_error(state: WorkbenchState, message: str) -> None:
    """목록 영역의 마지막 조회 오류를 기록한다."""
    state.list_error = message


def record_detail_error(state: WorkbenchState, message: str) -> None:
    """상세 workbench의 마지막 조회 오류를 기록한다."""
    state.detail_error = message


def record_report(
    state: WorkbenchState,
    experiment_id: str,
    markdown_text: str | None,
) -> None:
    """조회한 리포트 본문을 기록하고, 본문이 있을 때만 조회 완료 표식을 세운다.

    `[정정 — #647, 2026-08-10]` 이전에는 본문이 `None`이어도 표식을 세웠다 — "리포트가
    없다"도 조회 결과로 보고 매 갱신마다 다시 묻지 않으려는 의도였다(spec 결정 7 원문).
    그런데 결정 2가 지표 커밋과 리포트 커밋을 별도 트랜잭션으로 나눈 결과, `PASSED`로
    막 전이했지만 두 번째 트랜잭션이 아직 커밋되지 않아 `report_markdown`이 실제로는
    `NULL`인 순간이 실재한다. 그 틈에 5초 polling이 조회하면 200 + null을 받는데, UI는
    그것이 "진짜 리포트 없음"인지 "아직 두 번째 트랜잭션 전"인지 구별할 수 없다.
    구별 불가능한 것을 표식으로 캐시하면 `refresh_report`가 영구히 early-return하고
    결과 탭이 "아직 리포트가 없습니다."에 고착된다 — `select_experiment`는 같은 id면
    no-op이라 사이드바에서 같은 실험을 다시 눌러도 풀리지 않는다.

    그래서 본문이 있을 때만 표식을 세운다. `PASSED`는 `TERMINAL_STATUSES`에 없는
    polling 상태이므로 표식이 없으면 다음 갱신에서 자연히 재조회되어 스스로 낫는다.
    `PROMOTED`는 terminal이라 `record_terminal_refresh`가 폴링을 끝내므로 재시도가
    무한히 돌지 않는다. 트레이드오프: 리포트를 끄고 돌린 배포에서는 `PASSED` 실험이
    선택돼 있는 동안 5초마다 null 조회가 반복된다 — 응답이 작아 비용은 낮다.
    """
    state.report_markdown = markdown_text
    state.report_error = None
    if markdown_text is not None:
        state.report_loaded_for = experiment_id


def record_report_error(state: WorkbenchState, message: str) -> None:
    """리포트 조회 실패만 기록한다.

    `report_loaded_for`를 **세우지 않아** 다음 갱신에서 다시 시도된다. `detail_error`를
    건드리지 않는 이유는 리포트 실패가 워크벤치 전체를 오류 상태로 만들면 안 되기
    때문이다(spec 결정 7).
    """
    state.report_error = message
