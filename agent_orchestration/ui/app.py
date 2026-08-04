"""Streamlit Experiment Workbench 애플리케이션 진입점.

[파이프라인]
사용자가 가설을 제출하고 Agent Orchestration Experiment API의 기록을 관찰하는 사용자
인터페이스다. FastAPI는 영속화와 상태 전이를, 후속 실행기는 Event·Log 기록을 담당한다.

[기능]
가설 기반 v0 Experiment 생성, 최근 실험 선택, 상세·Event·Log의 5초 cursor polling,
API 오류의 사용자 표시를 제공한다.

[비책임]
GitHub Auto Research 이슈 발행, 실제 실험 실행, 상태·Event·Log·승격 쓰기, API 인증
정책 변경.
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
    WorkbenchState,
    append_event_page,
    append_log_page,
    mark_terminal_refresh_complete,
    record_refresh_error,
    select_experiment,
    should_poll,
)
from agent_orchestration.ui.styles import workbench_css
from agent_orchestration.ui.views import (
    render_empty_workbench,
    render_experiment_list,
    render_hypothesis_composer,
    render_workbench,
)


STATE_KEY = "experiment_workbench_state"


@st.cache_resource
def get_client() -> ExperimentClient:
    """Streamlit process에서 재사용할 API client를 구성한다."""
    return ExperimentClient.from_environment()


def get_state() -> WorkbenchState:
    """session state에 WorkbenchState를 최초 생성한다."""
    if STATE_KEY not in st.session_state:
        st.session_state[STATE_KEY] = WorkbenchState()
    return st.session_state[STATE_KEY]


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
    if state.selected_id is None and state.experiments:
        select_experiment(state, state.experiments[0].id)


def refresh_selected_experiment(client: ExperimentClient, state: WorkbenchState) -> None:
    """선택 Experiment와 cursor 이후 Event/Log를 갱신한다."""
    if state.selected_id is None:
        return
    try:
        state.experiment = client.get_experiment(state.selected_id)
        events, event_cursor = client.get_events(state.selected_id, state.event_cursor)
        logs, log_cursor = client.get_logs(state.selected_id, state.log_cursor)
        append_event_page(state, events, event_cursor)
        append_log_page(state, logs, log_cursor)
        if state.metadata_loaded_for != state.selected_id:
            state.metadata = client.get_metadata(state.selected_id)
            state.metadata_loaded_for = state.selected_id
        if state.experiment.status not in {"FAILED", "ERROR", "PROMOTED"}:
            state.terminal_refresh_complete = False
        else:
            mark_terminal_refresh_complete(state)
        state.last_error = None
        state.last_updated_at = datetime.now(timezone.utc)
    except ApiNotFoundError as error:
        select_experiment(state, None)
        record_refresh_error(state, str(error))
    except ExperimentApiError as error:
        record_refresh_error(state, str(error))


def create_from_hypothesis(client: ExperimentClient, state: WorkbenchState, hypothesis: str) -> None:
    """가설 제출 후 생성 Experiment를 선택하고 workbench로 전환한다."""
    try:
        experiment = client.create_experiment(hypothesis)
    except ExperimentApiError as error:
        record_refresh_error(state, str(error))
        return
    state.experiments.insert(0, experiment)
    select_experiment(state, experiment.id)
    refresh_selected_experiment(client, state)


def main() -> None:
    """Streamlit 페이지를 조립하고 active Experiment를 polling한다."""
    st.set_page_config(page_title="Autoresearch Experiment Console", layout="wide")
    st.markdown(workbench_css(), unsafe_allow_html=True)
    state = get_state()
    configuration_error = False
    try:
        client = get_client()
        if not state.experiments:
            refresh_experiment_list(client, state)
    except ApiConfigurationError as error:
        record_refresh_error(state, str(error))
        configuration_error = True
        client = None
    except ExperimentApiError as error:
        record_refresh_error(state, str(error))
        client = None

    if configuration_error:
        render_configuration_notice()

    submitted_hypothesis = render_hypothesis_composer(state.last_error)
    if submitted_hypothesis is not None:
        if client is None:
            st.error("Experiment API 연결을 먼저 복구해 주세요.")
        else:
            create_from_hypothesis(client, state, submitted_hypothesis)
            if state.selected_id is not None:
                st.rerun()

    selected_id = render_experiment_list(state.experiments, state.selected_id)
    if selected_id != state.selected_id:
        select_experiment(state, selected_id)
        st.rerun()
    if state.last_error:
        st.warning(state.last_error)
    if state.selected_id is None:
        render_empty_workbench()
        return

    @st.fragment(run_every="5s")
    def live_workbench() -> None:
        if client is not None and (state.experiment is None or should_poll(state)):
            refresh_selected_experiment(client, state)
        render_workbench(state)

    live_workbench()


if __name__ == "__main__":
    main()
