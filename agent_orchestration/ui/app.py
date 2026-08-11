"""Streamlit Experiment Workbench 애플리케이션 진입점.

[파이프라인]
사용자가 가설을 제출하고 Agent Orchestration Experiment API의 기록을 관찰하는 사용자
인터페이스다. FastAPI는 영속화와 상태 전이를, 후속 실행기는 Event·Log 기록을 담당한다.

[기능]
사전등록 화면, Experiment 상세 화면, 병렬 실행 현황 보드를 sidebar 탐색으로 분리하고, 사전등록 폼 제출로
Experiment 생성과 `[AR]` 이슈 발행을 잇달아 요청하며, 부분 실패한 발행을 저장 입력으로
재시도하거나 취소한다. 최근 실험 선택, 상세·Event·Log의 5초 cursor polling, API 오류의
영역별 사용자 표시와 삭제·만료 cursor 복구를 제공한다. 완주한 실험의 리포트 본문을
`refresh_selected_experiment` 말미에서 한 번만 조회해 결과 탭에 넘긴다.

[비책임]
이슈 본문 조립·`gh` 호출·label 부여(모두 API 서버), 실제 실험 실행, 상태·Event·Log·
승격 쓰기, API 인증 정책 변경.
"""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from agent_orchestration.ui.client import (
    ApiConfigurationError,
    ApiNotFoundError,
    ExperimentApiError,
    ExperimentClient,
)
from agent_orchestration.ui.state import (
    WorkbenchView,
    WorkbenchState,
    append_event_page,
    append_log_page,
    clear_activity_cache,
    discard_pending_publication,
    forget_board_entry,
    merge_steps,
    record_board_stage,
    record_detail_error,
    record_list_error,
    record_report,
    record_report_error,
    record_terminal_refresh,
    select_experiment,
    show_board,
    show_create_view,
    show_experiment,
    should_poll,
)
from agent_orchestration.ui.styles import workbench_css
from agent_orchestration.ui.models import (
    BOARD_RUNNING_STATUSES,
    REPORT_STATUSES,
    Submission,
    stage_index,
)
from agent_orchestration.ui.views import (
    render_board,
    render_board_button,
    render_empty_workbench,
    render_add_hypothesis_button,
    render_experiment_list,
    render_experiment_refresh_button,
    render_pending_publication_actions,
    render_publication_result,
    render_submission_form,
    render_workbench,
)


STATE_KEY = "experiment_workbench_state"
EXPERIMENT_SELECTION_EVENT_KEY = "experiment_selection_event"


@st.cache_resource
def get_client() -> ExperimentClient:
    """Streamlit process에서 재사용할 API client를 구성한다."""
    return ExperimentClient.from_environment()


def get_state() -> WorkbenchState:
    """session state에 WorkbenchState를 최초 생성한다."""
    if STATE_KEY not in st.session_state:
        st.session_state[STATE_KEY] = WorkbenchState()
    return st.session_state[STATE_KEY]


def record_experiment_selection() -> None:
    """sidebar radio의 명시적 Experiment 선택 이벤트를 다음 rerun까지 보존한다."""
    st.session_state[EXPERIMENT_SELECTION_EVENT_KEY] = True


def should_open_experiment_detail(selected_id: str | None, *, selection_changed: bool) -> bool:
    """현재 radio 값이 아니라 사용자 선택 이벤트가 있을 때만 상세 화면을 연다."""
    return selected_id is not None and selection_changed


def render_configuration_notice() -> None:
    """API 토큰 없이 연 화면에 서버 측 설정 방법을 표시한다."""
    st.error("Experiment API 연결 설정이 필요합니다.")
    st.code(
        "ORCH_UI_API_BASE_URL=http://127.0.0.1:8000 \\\nORCH_UI_API_TOKEN=\"$ORCH_API_TOKEN\" \\\nPYTHONPATH=. uv run streamlit run agent_orchestration/ui/app.py",
        language="bash",
    )
    st.caption("토큰은 Streamlit 서버 환경에만 설정되며 브라우저로 전달되지 않습니다.")


def refresh_experiment_list(client: ExperimentClient, state: WorkbenchState) -> None:
    """최근 Experiment 목록을 갱신한다."""
    state.experiments = client.list_experiments()
    state.list_error = None
    if state.selected_id is None and state.experiments:
        select_experiment(state, state.experiments[0].id)


def try_refresh_experiment_list(client: ExperimentClient, state: WorkbenchState) -> None:
    """목록 조회 실패를 화면 오류로 보존하고 다음 수동 재시도를 허용한다."""
    try:
        refresh_experiment_list(client, state)
    except ExperimentApiError as error:
        record_list_error(state, str(error))


def remove_selected_experiment(state: WorkbenchState, message: str) -> bool:
    """삭제된 선택 Experiment를 목록과 workbench에서 제거한다."""
    selected_id = state.selected_id
    if selected_id is None:
        return False
    state.experiments = [experiment for experiment in state.experiments if experiment.id != selected_id]
    select_experiment(state, None)
    record_detail_error(state, message)
    return True


def refresh_selected_experiment(client: ExperimentClient, state: WorkbenchState) -> bool:
    """선택 Experiment를 갱신하고, 선택 변경 시 전체 재렌더링 여부를 반환한다."""
    if state.selected_id is None:
        return False
    try:
        state.experiment = client.get_experiment(state.selected_id)
    except ApiNotFoundError as error:
        return remove_selected_experiment(state, str(error))
    except ExperimentApiError as error:
        record_detail_error(state, str(error))
        return False

    try:
        events, event_cursor = client.get_events(state.selected_id, state.event_cursor)
        logs, log_cursor = client.get_logs(state.selected_id, state.log_cursor)
        # Step은 갱신 사이에 cursor를 들고 가지 않고 매번 처음부터 다시 읽는다. Step은
        # PATCH로 갱신되는 mutable 리소스인데, cursor는 `after_id` **뒤의** row만 돌려주므로
        # 이미 받은 Step의 상태 변화(STARTED -> COMPLETED)를 영원히 관측하지 못한다.
        # 페이지 넘김은 client가 한 번의 갱신 안에서 처리한다.
        steps, steps_truncated = client.get_steps(state.selected_id)
        append_event_page(state, events, event_cursor)
        append_log_page(state, logs, log_cursor)
        merge_steps(state, steps, truncated=steps_truncated)
    except ApiNotFoundError:
        try:
            state.experiment = client.get_experiment(state.selected_id)
        except ApiNotFoundError as error:
            return remove_selected_experiment(state, str(error))
        except ExperimentApiError as error:
            record_detail_error(state, str(error))
            return False
        clear_activity_cache(state)
        record_detail_error(state, "Event 또는 Log cursor를 초기화해 최신 기록을 다시 불러옵니다.")
        return False
    except ExperimentApiError as error:
        record_detail_error(state, str(error))
        return False

    try:
        if state.metadata_loaded_for != state.selected_id:
            state.metadata = client.get_metadata(state.selected_id)
            state.metadata_loaded_for = state.selected_id
    except ApiNotFoundError as error:
        return remove_selected_experiment(state, str(error))
    except ExperimentApiError as error:
        record_detail_error(state, str(error))
        return False

    record_terminal_refresh(state)
    state.detail_error = None
    state.last_updated_at = datetime.now(timezone.utc)
    # 맨 끝이다. 여기서 무엇이 나도 위의 갱신 결과와 반환값을 바꾸지 않는다.
    refresh_report(client, state)
    return False


def refresh_board(client: ExperimentClient, state: WorkbenchState) -> None:
    """보드가 그릴 목록과 실험별 현재 단계를 갱신한다.

    목록은 `list_experiments` **한 번**이다 — 카드 수에 비례해 늘지 않는다. 단계는
    비종료 실험마다 로그를 읽되 cursor를 들고 가므로 첫 조회 이후에는 증분만 온다.

    로그 본문은 보관하지 않는다. 실험별로 남기는 것은 최신 `log_type` 하나와
    cursor뿐이다 — 상세 화면의 `state.logs`와는 별개 저장소다(spec 결정 4).
    """
    try:
        state.experiments = client.list_experiments()
        state.list_error = None
    except ExperimentApiError as error:
        record_list_error(state, str(error))
        return

    # 단계를 읽을 대상은 **지금 executor가 도는 실험**뿐이다. `POLLING_STATUSES`는
    # `PASSED`를 포함하므로(승격 전이가 남아 있다) 여기 쓰면 이미 끝난 실험의 로그를
    # 세션 내내 다시 읽는다.
    running_ids = {
        experiment.id
        for experiment in state.experiments
        if experiment.status in BOARD_RUNNING_STATUSES
    }
    for finished_id in set(state.board_log_cursors) - running_ids:
        forget_board_entry(state, finished_id)

    for experiment_id in running_ids:
        refresh_board_stage(client, state, experiment_id)


def refresh_board_stage(
    client: ExperimentClient, state: WorkbenchState, experiment_id: str
) -> None:
    """한 실험의 로그를 증분으로 읽어 현재 단계를 갱신한다.

    **실패해도 보드를 죽이지 않는다.** 카드 하나의 단계 표시가 비는 것과 보드 전체가
    오류로 덮이는 것은 다르다 — 나머지 실험이 무엇을 하고 있는지는 여전히 보여야
    한다(리포트 조회를 `report_error`로 격리한 것과 같은 이유).

    cursor가 만료됐으면(`ApiNotFoundError`) 처음부터 다시 읽는다. 그 실험의 단계
    표시가 잠깐 뒤로 갔다가 따라잡을 뿐, 다른 카드에는 영향이 없다.
    """
    cursor = state.board_log_cursors.get(experiment_id)
    try:
        logs, next_cursor = client.get_logs(experiment_id, cursor)
    except ApiNotFoundError:
        state.board_log_cursors.pop(experiment_id, None)
        return
    except ExperimentApiError:
        return

    latest_stage: str | None = None
    for log in logs:
        if stage_index(log.log_type) is not None:
            latest_stage = log.log_type
    record_board_stage(
        state, experiment_id, cursor=next_cursor, log_type=latest_stage
    )


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


def submit_experiment(
    client: ExperimentClient, state: WorkbenchState, submission: Submission
) -> bool:
    """Experiment를 만들고 곧바로 `[AR]` 이슈를 발행한다.

    두 번 호출하는 이유는 서버 계약이 그렇기 때문이다 — 생성은 순수 DB 쓰기이고 발행은
    외부 부작용이다. 발행 실패 시 생성된 Experiment와 원 제출을 보존한다.
    """
    experiment_id = state.pending_publication_experiment_id
    if experiment_id is None:
        try:
            experiment = client.create_experiment(submission.hypothesis)
        except ExperimentApiError as error:
            record_detail_error(state, str(error))
            return False
        state.experiments.insert(0, experiment)
        experiment_id = experiment.id
        state.pending_publication_experiment_id = experiment_id
        state.pending_publication_submission = submission
    elif state.pending_publication_submission != submission:
        record_detail_error(
            state,
            "이전에 생성된 실험의 이슈 발행이 완료되지 않았습니다. "
            "입력값을 원래대로 되돌린 뒤 다시 제출해 주세요.",
        )
        return False

    return publish_pending_issue(client, state)


def publish_pending_issue(client: ExperimentClient, state: WorkbenchState) -> bool:
    """저장된 submission으로 기존 Experiment의 이슈 발행만 수행한다."""
    experiment_id = state.pending_publication_experiment_id
    submission = state.pending_publication_submission
    if experiment_id is None or submission is None:
        record_detail_error(state, "재시도할 이슈 발행 정보가 없습니다.")
        return False

    try:
        publication = client.publish_issue(
            experiment_id, submission.to_fields()
        )
    except ExperimentApiError as error:
        show_create_view(state)
        record_detail_error(
            state,
            f"실험은 생성됐지만 이슈 발행에 실패했습니다. "
            f"저장된 입력으로 이슈 발행을 다시 시도할 수 있습니다: {error}",
        )
        return False

    discard_pending_publication(state)
    show_experiment(state, experiment_id)
    state.last_publication = publication
    refresh_selected_experiment(client, state)
    return True


def main() -> None:
    """Streamlit 페이지를 조립하고 현재 상세 화면만 polling한다."""
    st.set_page_config(page_title="Autoresearch Experiment Console", layout="wide")
    st.markdown(workbench_css(), unsafe_allow_html=True)
    state = get_state()
    client: ExperimentClient | None = None
    configuration_error = False
    try:
        client = get_client()
    except ApiConfigurationError as error:
        record_list_error(state, str(error))
        configuration_error = True
    if client is not None and not state.experiments:
        try_refresh_experiment_list(client, state)

    if configuration_error:
        render_configuration_notice()

    if render_add_hypothesis_button():
        show_create_view(state)
        st.rerun()
    if render_board_button():
        show_board(state)
        st.rerun()
    if render_experiment_refresh_button():
        if client is None:
            record_list_error(state, "Experiment API 연결을 먼저 복구해 주세요.")
        else:
            try_refresh_experiment_list(client, state)
    selected_id = render_experiment_list(
        state.experiments,
        state.selected_id if state.view is WorkbenchView.DETAIL else None,
        on_change=record_experiment_selection,
    )
    selection_changed = st.session_state.pop(EXPERIMENT_SELECTION_EVENT_KEY, False)
    if should_open_experiment_detail(selected_id, selection_changed=selection_changed):
        show_experiment(state, selected_id)
        st.rerun()
    if state.list_error:
        st.warning(state.list_error)

    if state.view is WorkbenchView.CREATE:
        if (
            state.pending_publication_experiment_id is not None
            and state.pending_publication_submission is not None
        ):
            retry_publication, discard_publication = (
                render_pending_publication_actions()
            )
            if retry_publication:
                if client is None:
                    record_detail_error(
                        state, "Experiment API 연결을 먼저 복구해 주세요."
                    )
                else:
                    publish_pending_issue(client, state)
                st.rerun()
            if discard_publication:
                discard_pending_publication(state)
                st.rerun()
        submission = render_submission_form(state.detail_error)
        if submission is not None:
            problems = submission.blocking_problems()
            if client is None:
                st.error("Experiment API 연결을 먼저 복구해 주세요.")
            elif problems:
                # 첫 요청 전에 끊는다. 발행 실패는 재시도 화면으로 복구할 수 있지만,
                # `### `처럼 고치기 전에는 반드시 실패하는 값이면 Experiment를 만드는
                # 요청 자체가 헛일이고 사용자는 두 단계 뒤에야 원인을 본다.
                for problem in problems:
                    st.error(problem)
            else:
                state.last_publication = None
                submit_experiment(client, state, submission)
                st.rerun()
        return

    if state.view is WorkbenchView.BOARD:

        @st.fragment(run_every="5s")
        def live_board() -> None:
            if client is not None:
                refresh_board(client, state)
            if state.list_error:
                st.warning(state.list_error)
            opened_id = render_board(state)
            if opened_id is not None:
                show_experiment(state, opened_id)
                # fragment 안의 기본 `st.rerun()`은 **fragment만** 다시 돌린다.
                # 그러면 상태는 DETAIL로 바뀌었는데 화면은 보드에 머문다.
                st.rerun(scope="app")

        live_board()
        return

    if state.selected_id is None:
        render_empty_workbench()
        return
    if state.last_publication is not None:
        render_publication_result(state.last_publication)

    @st.fragment(run_every="5s")
    def live_workbench() -> None:
        if client is not None and (state.experiment is None or should_poll(state)):
            if refresh_selected_experiment(client, state):
                st.rerun()
        if state.detail_error:
            st.warning(state.detail_error)
        render_workbench(state)

    live_workbench()


if __name__ == "__main__":
    main()
