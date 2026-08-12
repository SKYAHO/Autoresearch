"""Streamlit workbench의 순수 session state 전이를 제공한다.

[파이프라인]
Experiment API에서 받은 목록·상세·Event·Log를 화면 상태로 누적하는 구간이다. API 호출과
Streamlit widget 렌더링은 담당하지 않는다.

[기능]
Experiment 선택, cursor 기반 Event/Log 누적, terminal 상태의 추가 최종 갱신, 목록·상세
오류 분리 보존, 화면 모드 전이와 이슈 발행 재시도·취소 상태 전이를 제공한다. 리포트 본문
캐시와 조회 오류의 분리 보존도 포함한다. 병렬 실행 현황 보드가 실험별로 따로 들고 가는
로그 cursor와 최신 단계도 여기서 관리한다(#671).

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
    ExperimentCost,
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
    BOARD = "BOARD"


@dataclass(frozen=True)
class PendingPublication:
    """생성은 끝났지만 이슈 발행이 남은 실험 하나.

    생성(순수 DB 쓰기)과 발행(외부 부작용)은 서버 계약상 두 번의 요청이라 그 사이에서
    끊길 수 있다. 끊긴 지점을 항목 단위로 들고 있어야 재시도가 이미 만든 Experiment를
    다시 만들지 않는다.
    """

    experiment_id: str
    submission: Submission


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
    # 보드가 실험별로 따로 들고 가는 로그 cursor와 최신 단계.
    #
    # **`log_cursor`와 섞지 않는다.** `log_cursor`는 선택된 실험 하나의 것이고 상세
    # 화면의 원본 로그 탭이 소유한다. 보드가 그 값을 밀면 상세 화면이 이미 읽은
    # 로그를 다시 읽거나 아직 못 읽은 구간을 건너뛴다(spec 결정 4).
    board_log_cursors: dict[str, str | None] = field(default_factory=dict)
    board_stages: dict[str, str] = field(default_factory=dict)
    metadata_loaded_for: str | None = None
    terminal_status_observed: str | None = None
    terminal_refresh_complete: bool = False
    list_error: str | None = None
    detail_error: str | None = None
    last_updated_at: datetime | None = None
    # 방금 발행한 이슈 좌표들. 선택 변경이나 다음 제출 때 비워 이전 결과가 남지 않게
    # 한다. 한 번에 여러 가설을 제출할 수 있으므로 목록이다(#671).
    last_publications: list[IssuePublication] = field(default_factory=list)
    # 생성 성공·발행 실패 사이의 부분 성공을 보존해 재제출 시 Experiment 중복 생성을
    # 막는다. 묶음 제출에서는 일부만 실패할 수 있어 항목 단위로 남긴다.
    pending_publications: list[PendingPublication] = field(default_factory=list)
    # 제출 자체가 실패한 사유. **`detail_error`와 따로 둔다.**
    #
    # 다섯 개를 냈는데 세 번째 생성이 실패하면 앞의 둘은 정상 발행되어 화면이 보드로
    # 넘어간다. 그 사유를 `detail_error`에 담으면 발행 성공 경로가 그것을 지워, 카드가
    # 두 장뿐인 이유를 아무도 설명하지 못한다. 화면 전환을 넘어 살아남아야 한다.
    submission_error: str | None = None
    # 조회한 리포트 본문. `None`은 "아직 안 받음"과 "받았는데 리포트가 없음" 두 가지로
    # 겹치므로, 둘을 가르는 것은 `report_checked_for`다.
    report_markdown: str | None = None
    report_error: str | None = None
    # **표식 두 개가 서로 다른 질문에 답한다.** 하나로 겹치면 한쪽이 반드시 틀어진다.
    #
    # `report_checked_for` — "이 실험을 한 번이라도 조회해 봤는가". 본문 유무와 무관하게
    # 조회가 성공하면 세운다. **화면 문구를 고르는 데만** 쓴다. 이것이 없으면 리포트가
    # 정말 없는 실험(이 변경 이전에 완주한 실험 전부)이 "불러오는 중"에 영구히 머문다.
    #
    # `report_loaded_for` — "캐시할 본문을 이미 받았는가". 본문이 있을 때만 세우며
    # 재조회를 막는다. 본문이 없을 때 세우지 않는 이유는 `record_report`에 있다.
    #
    # 둘 다 실패에는 세우지 않는다 — 일시적 오류 한 번에 리포트가 영구히 가려진다.
    report_checked_for: str | None = None
    report_loaded_for: str | None = None
    # 조회한 실행 비용. 리포트와 같은 이유로 상세 polling에 얹지 않고 따로 받는다.
    # `cost_loaded_for`는 재조회를 막는 캐시다 — 비용은 완주한 실험에서만 조회하므로
    # 리포트처럼 "값이 아직 없는 순간"을 구별할 필요가 없어 표식 하나면 충분하다.
    cost: ExperimentCost | None = None
    cost_error: str | None = None
    cost_loaded_for: str | None = None


def show_create_view(state: WorkbenchState) -> None:
    """Workbench를 Experiment 생성 화면으로 전환한다."""
    state.view = WorkbenchView.CREATE


def discard_pending_publications(state: WorkbenchState) -> None:
    """실패한 이슈 발행 대기와 관련 오류만 정리한다."""
    state.pending_publications.clear()
    state.detail_error = None
    state.submission_error = None


def record_submission_error(state: WorkbenchState, message: str) -> None:
    """제출이 실패한 사유를 화면 전환 뒤에도 남도록 기록한다."""
    state.submission_error = message


def show_board(state: WorkbenchState) -> None:
    """Workbench를 병렬 실행 현황 보드로 전환한다."""
    state.view = WorkbenchView.BOARD


def record_board_stage(
    state: WorkbenchState,
    experiment_id: str,
    *,
    cursor: str | None,
    log_type: str | None,
) -> None:
    """보드가 읽은 로그 위치와 최신 단계를 함께 기록한다.

    cursor는 로그를 읽었으면 항상 갱신하고, 단계는 **이번 페이지에서 단계를 찾았을
    때만** 덮어쓴다. 새 로그가 단계에 속하지 않는 `log_type`(에이전트가 만든 임의
    값)뿐이면 직전 단계를 유지해야 카드가 깜빡이지 않는다.
    """
    state.board_log_cursors[experiment_id] = cursor
    if log_type is not None:
        state.board_stages[experiment_id] = log_type


def forget_board_entry(state: WorkbenchState, experiment_id: str) -> None:
    """종료된 실험의 보드 cursor와 단계를 버린다.

    안 버리면 두 dict가 실험 수만큼 무한히 자란다 — 세션이 길수록 커지고 다시 쓸
    일도 없다.
    """
    state.board_log_cursors.pop(experiment_id, None)
    state.board_stages.pop(experiment_id, None)


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
    state.last_publications.clear()
    state.report_markdown = None
    state.report_error = None
    state.report_checked_for = None
    state.report_loaded_for = None
    state.cost = None
    state.cost_error = None
    state.cost_loaded_for = None


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

    `[재-정정 — #647, 2026-08-10]` 표식 하나로 "캐시할 값이 있는가"와 "조회를 해 봤는가"를
    동시에 표현하려다 후자를 잃었다. 본문이 없을 때 아무 표식도 세우지 않으면, 리포트가
    **정말 없는** 실험(이 변경 이전에 완주한 실험 전부)은 화면이 "불러오는 중"에서
    내려오지 못한다 — `PROMOTED`는 terminal이라 폴링까지 멈춰 그 문구에 영구히 고착되고,
    `PASSED`는 문구가 그대로인 채 5초마다 조회만 반복한다. 그래서 조회를 시도했다는
    사실은 `report_checked_for`에 **본문 유무와 무관하게** 남기고(문구 선택 전용),
    재조회를 막는 캐시만 `report_loaded_for`에 남긴다.
    """
    state.report_markdown = markdown_text
    state.report_error = None
    state.report_checked_for = experiment_id
    if markdown_text is not None:
        state.report_loaded_for = experiment_id


def record_cost(
    state: WorkbenchState, experiment_id: str, cost: ExperimentCost
) -> None:
    """조회한 실행 비용을 기록하고 재조회를 막는다."""
    state.cost = cost
    state.cost_error = None
    state.cost_loaded_for = experiment_id


def record_cost_error(state: WorkbenchState, message: str) -> None:
    """비용 조회 실패만 기록한다.

    `cost_loaded_for`를 세우지 않아 다음 갱신에서 다시 시도된다. 리포트와 같은 이유로
    `detail_error`를 건드리지 않는다 — 비용 조회 하나가 워크벤치 전체를 오류 상태로
    만들면 안 된다.
    """
    state.cost_error = message


def record_report_error(state: WorkbenchState, message: str) -> None:
    """리포트 조회 실패만 기록한다.

    `report_loaded_for`를 **세우지 않아** 다음 갱신에서 다시 시도된다. `detail_error`를
    건드리지 않는 이유는 리포트 실패가 워크벤치 전체를 오류 상태로 만들면 안 되기
    때문이다(spec 결정 7).
    """
    state.report_error = message
