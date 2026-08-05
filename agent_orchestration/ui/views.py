"""Streamlit Experiment Workbench의 화면 컴포넌트를 렌더링한다.

[파이프라인]
사용자가 실험을 사전등록하고 Experiment API가 기록한 상태·Event·Log를 관찰하는 UI
구간을 담당한다. API 호출과 polling state 전이는 app/client 모듈이 담당한다.

[기능]
단일 화면 상단의 사전등록 제출 폼과 발행 결과 표시, 실험 선택 목록, 빈 관찰 패널,
상태 타임라인, 결과·Event·원본 Log 탭, KST 시각이 포함된 요약 패널을 렌더링한다.

[비책임]
HTTP 인증, API 오류 분류, 상태 기록, Agent 실행, 이슈 본문 조립과 GitHub 이슈 생성
(모두 API 서버의 책임이다).
"""

from __future__ import annotations

import html
import json
from collections.abc import Sequence

import streamlit as st

from agent_orchestration.ui.models import (
    METRIC_DIRECTIONS,
    NONE_VALUE,
    NOT_APPLICABLE,
    SCOPE_CHOICES,
    Event,
    Experiment,
    IssuePublication,
    Step,
    Submission,
    status_label,
    step_kind_label,
    step_status_color,
)
from agent_orchestration.ui.state import WorkbenchState
from agent_orchestration.ui.styles import status_badge
from agent_orchestration.ui.time import format_time


def render_submission_form(api_error: str | None) -> Submission | None:
    """사전등록 제출 폼을 렌더링하고 제출 값을 반환한다.

    필드 구성은 예측 모델링 사전등록 표준(arXiv 2311.18807)의 Phase A 중 연구자가
    선언해야 하는 항목이다. 데이터·split·시드는 실험 간 비교가 성립하도록 서버가
    고정하므로 입력받지 않고 아래에 읽기 전용으로 보여준다.
    """
    st.markdown('<p class="workbench-kicker">AUTORESEARCH / NEW EXPERIMENT</p>', unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown("### 실험을 사전등록합니다")
        st.caption(
            "제출하면 `[AR]` 이슈가 열리고 `auto-experiment` label이 붙습니다. "
            "성공 기준은 결과를 보기 전에 정해야 하므로 지표와 임계값을 직접 선언합니다."
        )
        if api_error:
            st.error(api_error)
        with st.form("submission-form", clear_on_submit=False):
            title = st.text_input(
                "실험 제목",
                placeholder="예: views per day ratio feature",
                help="이슈 제목과 실험 브랜치 이름이 여기서 만들어집니다. 영소문자와 숫자를 포함해 주세요.",
            )
            hypothesis = st.text_area(
                "연구 가설",
                placeholder="예: 비율 피처가 baseline 대비 test ROC-AUC를 개선한다.",
                height=90,
                help="무엇이 왜 개선될 것이라 보는지. 근거까지 적으면 실행기가 검증 방법을 좁힐 수 있습니다.",
            )
            related_work = st.text_area(
                "선행 연구 참조 (선택)",
                placeholder="예: https://arxiv.org/abs/1706.09516",
                height=68,
            )
            change = st.text_area(
                "변경할 피처 · 모델",
                value="- 추가/변경할 피처 (계산식):\n- 변경할 하이퍼파라미터 (없으면 \"없음\"):\n- baseline과 동일하게 유지할 것:",
                height=110,
            )

            st.markdown("**성공 기준**")
            metric_left, metric_mid, metric_right = st.columns([2, 2, 1])
            primary_metric_name = metric_left.text_input("주 지표 이름", value="roc_auc")
            primary_direction_label = metric_mid.selectbox(
                "주 지표 방향", list(METRIC_DIRECTIONS)
            )
            minimum_primary_delta = metric_right.text_input("최소 개선폭", value="0.002")

            # 체크박스로 아래 칸의 `disabled`를 제어하지 않는다. `st.form` 안의 위젯은
            # 상호작용해도 rerun을 일으키지 않으므로, 체크박스를 켜도 이번 렌더의
            # `disabled`는 직전 값(최초 True)인 채로 남아 사용자가 값을 넣지 못한다.
            # 그대로 제출되면 guardrail 없이 발행되고, 본문이 커밋된 뒤에는 그 실험에
            # guardrail을 붙일 수 없다. 이름 칸이 채워졌는지로만 선언을 판정한다.
            st.caption("Guardrail을 쓰지 않으려면 아래 두 칸을 비워 두십시오.")
            guardrail_left, guardrail_mid, guardrail_right = st.columns([2, 2, 1])
            guardrail_metric_name = guardrail_left.text_input(
                "Guardrail 지표 이름 (선택)", value=""
            )
            guardrail_direction_label = guardrail_mid.selectbox(
                "Guardrail 방향", list(METRIC_DIRECTIONS)
            )
            maximum_guardrail_regression = guardrail_right.text_input(
                "최대 악화폭", value=""
            )
            secondary_metrics = st.text_input(
                "보조 관측 지표 (선택)", placeholder="예: pr_auc"
            )

            st.markdown("**허용 범위**")
            st.caption("실행기가 수정해도 되는 범위입니다. 에이전트가 스스로 넓힐 수 없습니다.")
            allowed_scope = [
                key for key, label in SCOPE_CHOICES.items() if st.checkbox(label, key=f"scope-{key}")
            ]

            with st.expander("서버가 고정하는 값"):
                st.caption(
                    "실험 간 비교가 성립하려면 데이터와 분할이 같아야 하므로 입력받지 않습니다."
                )
                st.markdown(
                    "- 랜덤 시드: `42..71` (30개)\n"
                    "- 비교 대상: 동일 조건 baseline 재학습\n"
                    "- Split 시드 · Test/Validation 비율 · 데이터셋 스냅샷 · 학습 설정 참조\n"
                    "- 대상 기간: 발행 시점 기준 최근 30일 (KST)"
                )

            submitted = st.form_submit_button("사전등록하고 이슈 발행", type="primary")

    if not submitted:
        return None
    declared = guardrail_metric_name.strip() != ""
    return Submission(
        title=title.strip(),
        hypothesis=hypothesis.strip(),
        related_work=related_work.strip(),
        change=change.strip(),
        primary_metric_name=primary_metric_name.strip(),
        primary_metric_direction=METRIC_DIRECTIONS[primary_direction_label],
        minimum_primary_delta=minimum_primary_delta.strip(),
        guardrail_metric_name=(
            guardrail_metric_name.strip() if declared else NONE_VALUE
        ),
        guardrail_metric_direction=(
            METRIC_DIRECTIONS[guardrail_direction_label] if declared else NOT_APPLICABLE
        ),
        maximum_guardrail_regression=(
            maximum_guardrail_regression.strip() if declared else NONE_VALUE
        ),
        secondary_metrics=secondary_metrics.strip(),
        allowed_scope=tuple(allowed_scope),
    )


def render_publication_result(publication: IssuePublication) -> None:
    """발행된 이슈 좌표를 보여준다."""
    st.success(
        f"이슈 #{publication.issue_number}가 열렸습니다 · 실험 브랜치 "
        f"`{publication.issue_branch}`"
    )
    st.markdown(f"[GitHub에서 열기]({publication.issue_url})")


def render_empty_workbench() -> None:
    """선택된 Experiment가 없을 때 단일 화면의 빈 관찰 패널을 렌더링한다."""
    with st.container(border=True):
        st.markdown("### 실험 관찰 영역")
        st.info("상단에서 가설을 등록하거나 좌측 실험 기록에서 항목을 선택해 주세요.")
        st.caption("실행기가 연결되면 상태, 이벤트, 원본 로그, 평가 결과가 이곳에 실시간으로 표시됩니다.")


def render_experiment_refresh_button() -> bool:
    """sidebar의 Experiment 목록 수동 새로고침 버튼을 렌더링한다."""
    return st.sidebar.button("실험 목록 새로고침", use_container_width=True)


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
    default_index = ids.index(selected_id) if selected_id in ids else None
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


def _render_steps(steps: Sequence[Step], truncated: bool = False) -> None:
    """에이전트가 지금 무엇을 하고 있는지 단계별로 표시한다."""
    if truncated:
        # 조용히 버리지 않는다 — 뒷부분을 못 읽었으면 화면이 오래된 구간에 고정된 상태다.
        st.warning(
            f"작업 단계가 너무 많아 앞부분 {len(steps)}개까지만 읽었습니다. "
            "아래 목록에 최신 단계가 없을 수 있습니다."
        )
    if not steps:
        st.caption("아직 기록된 작업 단계가 없습니다.")
        return
    if len(steps) > 30:
        st.caption(f"최근 30개 단계를 표시합니다. 전체 {len(steps)}개")
    for step in steps[-30:]:
        color = step_status_color(step.status)
        # step_type은 에이전트가 정하는 자유 문자열이라 이 마크업에서 유일하게 신뢰 경계를
        # 넘는다. step_kind는 서버 CHECK로 닫혀 있지만 라벨 폴백이 원문을 그대로 내보내므로
        # 함께 escape한다. color는 닫힌 집합, format_time은 포맷된 timestamp라 안전하다.
        st.markdown(
            f"<span style='color:{color};font-weight:600'>&#9679; "
            f"{html.escape(step_kind_label(step.step_kind))}</span> "
            f"<span style='opacity:0.7'>{html.escape(step.step_type)}</span> "
            f"<span style='opacity:0.5'>· {format_time(step.updated_at)}</span>",
            unsafe_allow_html=True,
        )
        # message가 없으면 display_line이 kind·type 라벨로 대신하므로 표시가 비지 않는다.
        st.write(step.display_line)
        if step.target:
            with st.expander("상세", expanded=False):
                st.json(step.target)


def _render_tabs(state: WorkbenchState) -> None:
    progress_tab, results_tab, events_tab, logs_tab = st.tabs(
        ["진행 단계", "결과", "이벤트", "원본 로그"]
    )
    with progress_tab:
        _render_steps(state.steps, state.steps_truncated)
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
