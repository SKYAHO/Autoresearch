"""Streamlit Experiment Workbench의 화면 컴포넌트를 렌더링한다.

[파이프라인]
사용자가 가설을 제출하고 Experiment API가 기록한 상태·Event·Log를 관찰하는 UI 구간을
담당한다. API 호출과 polling state 전이는 app/client 모듈이 담당한다.

[기능]
단일 화면 상단의 가설 작성 패널, 실험 선택 목록, 빈 관찰 패널, 상태 타임라인,
결과·Event·원본 Log 탭, 요약 패널을 렌더링한다.

[비책임]
HTTP 인증, API 오류 분류, 상태 기록, Agent 실행, GitHub 이슈 생성.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime

import streamlit as st

from agent_orchestration.ui.models import Event, Experiment, status_label
from agent_orchestration.ui.state import WorkbenchState
from agent_orchestration.ui.styles import status_badge


def format_time(value: datetime) -> str:
    """화면용 지역 시각 문자열을 반환한다."""
    return value.astimezone().strftime("%m-%d %H:%M")


def render_hypothesis_composer(api_error: str | None) -> str | None:
    """단일 화면 상단의 가설 작성 패널을 렌더링하고 제출 가설을 반환한다."""
    st.markdown('<p class="workbench-kicker">AUTORESEARCH / NEW HYPOTHESIS</p>', unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown("### 새 가설을 작성합니다")
        st.caption("가설 하나는 하나의 실험으로 저장됩니다. 실행기가 연결되면 진행 과정이 아래에 표시됩니다.")
        if api_error:
            st.error(api_error)
        with st.form("hypothesis-form", clear_on_submit=True):
            hypothesis = st.text_area(
                "가설",
                placeholder="예: 썸네일의 색상 대비를 높이면 CTR이 개선될 것이다.",
                height=110,
                label_visibility="collapsed",
            )
            submitted = st.form_submit_button("가설 등록", type="primary")
    if submitted:
        return hypothesis
    return None


def render_empty_workbench() -> None:
    """선택된 Experiment가 없을 때 단일 화면의 빈 관찰 패널을 렌더링한다."""
    with st.container(border=True):
        st.markdown("### 실험 관찰 영역")
        st.info("상단에서 가설을 등록하거나 좌측 실험 기록에서 항목을 선택해 주세요.")
        st.caption("실행기가 연결되면 상태, 이벤트, 원본 로그, 평가 결과가 이곳에 실시간으로 표시됩니다.")


def render_experiment_list(
    experiments: Sequence[Experiment],
    selected_id: str | None,
) -> str | None:
    """sidebar Experiment 목록을 렌더링하고 새 선택 ID를 반환한다."""
    st.sidebar.markdown("### 실험 기록")
    st.sidebar.caption("가설 하나는 하나의 Auto Research 이슈로 이어집니다.")
    if not experiments:
        st.sidebar.caption("아직 실험이 없습니다.")
        return None
    ids = [experiment.id for experiment in experiments]
    labels = {
        experiment.id: f"{experiment.status} | {experiment.hypothesis[:32]}"
        for experiment in experiments
    }
    default_index = ids.index(selected_id) if selected_id in ids else 0
    return st.sidebar.radio(
        "최근 실험",
        ids,
        index=default_index,
        format_func=lambda experiment_id: labels[experiment_id],
        label_visibility="collapsed",
    )


def render_workbench(state: WorkbenchState) -> None:
    """선택 Experiment의 가설 중심 workbench를 렌더링한다."""
    if state.experiment is None:
        st.info("좌측 목록에서 실험을 선택하거나 새 가설을 제출해 주세요.")
        return

    experiment = state.experiment
    st.markdown('<p class="workbench-kicker">선택한 가설</p>', unsafe_allow_html=True)
    title_column, status_column = st.columns([4.0, 1.0], vertical_alignment="center")
    with title_column:
        st.title(experiment.hypothesis)
    with status_column:
        st.markdown(status_badge(experiment.status), unsafe_allow_html=True)
        st.caption(f"마지막 갱신 {format_time(experiment.updated_at)}")

    main_column, inspector_column = st.columns([2.2, 1.0], gap="large")
    with main_column:
        with st.container(border=True):
            st.markdown("#### 관찰 보드")
            _render_tabs(state)
    with inspector_column:
        with st.container(border=True):
            st.markdown("#### 실행 요약")
            _render_metrics(experiment.metric_summary)
        with st.container(border=True):
            st.markdown("#### 진행 단계")
            _render_timeline(state.events[-8:], experiment.status)
        with st.container(border=True):
            st.markdown("#### 메타데이터")
            if state.metadata:
                for key, value in state.metadata.items():
                    st.caption(f"{key}: {value}")
            else:
                st.caption("등록된 메타데이터가 없습니다.")


def _render_timeline(events: Sequence[Event], current_status: str) -> None:
    if not events:
        st.caption(f"현재 상태: {status_label(current_status)}")
        return
    for event in events:
        source = event.from_status or "START"
        st.markdown(f"**{source} -> {event.to_status}**")
        st.caption(format_time(event.created_at))
        if event.reason:
            st.write(event.reason)


def _render_tabs(state: WorkbenchState) -> None:
    results_tab, events_tab, logs_tab = st.tabs(["결과", "이벤트", "원본 로그"])
    with results_tab:
        _render_metrics(state.experiment.metric_summary if state.experiment else None)
    with events_tab:
        if not state.events:
            st.caption("아직 기록된 이벤트가 없습니다.")
        if len(state.events) > 20:
            st.caption(f"최근 20개 이벤트를 표시합니다. 전체 {len(state.events)}개")
        for event in state.events[-20:]:
            source = event.from_status or "START"
            st.markdown(f"**{source} -> {event.to_status}** · {format_time(event.created_at)}")
            if event.reason:
                st.write(event.reason)
            if event.metric_snapshot:
                st.json(event.metric_snapshot)
    with logs_tab:
        if not state.logs:
            st.caption("아직 기록된 원본 로그가 없습니다.")
        if len(state.logs) > 30:
            st.caption(f"최근 30개 로그를 표시합니다. 전체 {len(state.logs)}개")
        with st.container(height=360, border=False):
            for log in state.logs[-30:]:
                st.caption(f"{format_time(log.created_at)} · {log.log_type}")
                st.code(log.content, language=None)


def _render_metrics(metrics: dict[str, object] | None) -> None:
    if not metrics:
        st.caption("아직 평가 전입니다.")
        return
    for key, value in metrics.items():
        if isinstance(value, (dict, list)):
            st.markdown(f"**{key}**")
            st.code(json.dumps(value, ensure_ascii=False, indent=2), language="json")
        else:
            st.metric(key, str(value))
