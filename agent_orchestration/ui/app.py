"""Streamlit Experiment Workbench 애플리케이션 진입점.

[파이프라인]
사용자가 가설을 제출하고 Agent Orchestration Experiment API의 기록을 관찰하는 사용자
인터페이스다. FastAPI는 영속화와 상태 전이를, 후속 실행기는 Event·Log 기록을 담당한다.

[기능]
사전등록 화면과 Experiment 상세 화면을 sidebar 탐색으로 분리하고, 사전등록 폼 제출로
Experiment 생성과 `[AR]` 이슈 발행을 잇달아 요청한다. 최근 실험 선택, 상세·Event·Log의
5초 cursor polling, API 오류의 영역별 사용자 표시와 삭제·만료 cursor 복구를 제공한다.

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
    merge_steps,
    record_detail_error,
    record_list_error,
    record_terminal_refresh,
    select_experiment,
    show_create_view,
    show_experiment,
    should_poll,
)
from agent_orchestration.ui.styles import workbench_css
from agent_orchestration.ui.models import Submission
from agent_orchestration.ui.views import (
    render_empty_workbench,
    render_add_hypothesis_button,
    render_experiment_list,
    render_experiment_refresh_button,
    render_publication_result,
    render_submission_form,
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
    return False


def submit_experiment(
    client: ExperimentClient, state: WorkbenchState, submission: Submission
) -> bool:
    """Experiment를 만들고 곧바로 `[AR]` 이슈를 발행한다.

    두 번 호출하는 이유는 서버 계약이 그렇기 때문이다 — 생성은 순수 DB 쓰기이고 발행은
    외부 부작용이다.

    **발행이 실패하면 이슈 없는 Experiment가 남는다.** 이 함수는 매번
    `create_experiment()`부터 부르므로 폼에서 다시 제출하면 **새 Experiment**가 생기고,
    앞서 실패한 실험은 `CREATED`인 채로 목록에 남는다. 그 실험을 재발행하는 경로는
    아직 UI에 없다 — `POST /experiments/{id}/issue`를 직접 부르면 서버가 저장된 본문
    그대로 재발행한다.
    """
    try:
        experiment = client.create_experiment(submission.hypothesis)
    except ExperimentApiError as error:
        record_detail_error(state, str(error))
        return False
    state.experiments.insert(0, experiment)
    show_experiment(state, experiment.id)

    try:
        state.last_publication = client.publish_issue(
            experiment.id, submission.to_fields(), submission.allowed_scope
        )
    except ExperimentApiError as error:
        # 갱신을 **먼저** 한다. `refresh_selected_experiment()`는 정상 종료 경로에서
        # `detail_error`를 지우므로(아래), 순서를 뒤집으면 방금 기록한 발행 실패
        # 메시지가 조회 성공과 함께 사라져 사용자가 아무 피드백도 받지 못한다.
        refresh_selected_experiment(client, state)
        record_detail_error(
            state, f"실험은 생성됐지만 이슈 발행에 실패했습니다: {error}"
        )
        return False

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
    if render_experiment_refresh_button():
        if client is None:
            record_list_error(state, "Experiment API 연결을 먼저 복구해 주세요.")
        else:
            try_refresh_experiment_list(client, state)
    selected_id = render_experiment_list(state.experiments, state.selected_id)
    if selected_id is not None and (
        selected_id != state.selected_id or state.view is not WorkbenchView.DETAIL
    ):
        show_experiment(state, selected_id)
        st.rerun()
    if state.list_error:
        st.warning(state.list_error)

    if state.view is WorkbenchView.CREATE:
        submission = render_submission_form(state.detail_error)
        if submission is not None:
            missing = submission.missing_required()
            if client is None:
                st.error("Experiment API 연결을 먼저 복구해 주세요.")
            elif missing:
                st.error("다음 항목을 채워 주세요: " + ", ".join(missing))
            else:
                state.last_publication = None
                submit_experiment(client, state, submission)
                st.rerun()
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
