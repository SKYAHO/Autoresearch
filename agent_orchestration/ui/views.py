"""Streamlit Experiment Workbench의 화면 컴포넌트를 렌더링한다.

[파이프라인]
사용자가 실험을 사전등록하고 Experiment API가 기록한 상태·Event·Log를 관찰하는 UI
구간을 담당한다. API 호출과 polling state 전이는 app/client 모듈이 담당한다.

[기능]
sidebar 탐색으로 분리된 사전등록 화면과 상세 화면의 컴포넌트를 렌더링한다. 사전등록
제출 폼, 실패한 이슈 발행의 재시도·취소 동작과 발행 결과 표시, 실험 선택 목록, 빈 관찰
패널, 상태 타임라인, 결과·Event·원본 Log 탭, KST 시각이 포함된 요약 패널을 제공한다.
결과 탭의 지표 카드와 리포트 HTML 렌더도 이 모듈이 담당한다.

[비책임]
HTTP 인증, API 오류 분류, 상태 기록, Agent 실행, 이슈 본문 조립과 GitHub 이슈 생성
(모두 API 서버의 책임이다).
"""

from __future__ import annotations

import html
import json
from collections.abc import Callable, Sequence

import streamlit as st

from agent_orchestration.ui.models import (
    Event,
    Experiment,
    IssuePublication,
    REPORT_STATUSES,
    Step,
    Submission,
    status_label,
    step_kind_label,
    step_status_color,
)
from agent_orchestration.ui.report import report_document
from agent_orchestration.ui.state import WorkbenchState
from agent_orchestration.ui.styles import status_badge
from agent_orchestration.ui.time import format_time


HYPOTHESIS_KEY = "submission-hypothesis"

_HYPOTHESIS_PLACEHOLDER = "마크다운 형식으로 가설을 작성해 주세요"


def _render_hypothesis_editor() -> str:
    """마크다운 편집창과 미리보기를 나란히 렌더링하고 현재 본문을 반환한다.

    `st.form` **밖**에서 호출해야 한다. 폼 안의 위젯은 상호작용해도 rerun을 일으키지
    않으므로, 안에 두면 미리보기가 제출 전까지 첫 렌더 상태에 멈춘다. 밖에 두면
    편집창이 포커스를 잃거나 Ctrl+Enter를 받을 때 rerun이 돌아 미리보기가 따라온다.
    """
    editor_column, preview_column = st.columns(2, gap="medium")
    with editor_column:
        st.caption("편집")
        hypothesis = st.text_area(
            "가설",
            key=HYPOTHESIS_KEY,
            placeholder=_HYPOTHESIS_PLACEHOLDER,
            height=340,
            label_visibility="collapsed",
        )
    with preview_column:
        st.caption("미리보기")
        with st.container(border=True, height=340):
            if hypothesis.strip():
                # 사용자가 쓴 마크다운이다. `unsafe_allow_html`을 켜지 않으므로 본문에
                # 든 HTML은 렌더링되지 않고 글자로 표시된다.
                st.markdown(hypothesis)
            else:
                st.caption("여기에 미리보기가 나옵니다.")
    return hypothesis


def _render_agent_instructions() -> None:
    """실험별 에이전트 지침을 올리고 고르는 화면을 렌더링한다.

    화면만 제공한다(#570). 올린 파일을 실행기 작업 폴더에 놓는 배선은 아직 없으므로,
    여기서 모은 값은 어디로도 전송되지 않는다.
    """
    st.markdown("**에이전트 지침 (선택)**")
    preset_column, share_column = st.columns([3, 2], gap="medium")
    preset_column.selectbox(
        "저장된 지침에서 고르기",
        (
            "선택 안 함 — 파일만 올립니다",
            "내 지침 · lgbm 실험 공통",
            "내 지침 · 피처 조립 주의사항",
            "공개 · 트리 모델 튜닝 가이드 (12명 사용 중)",
        ),
    )
    share_column.toggle("다른 리서처에게 공개")
    st.file_uploader(
        "AGENTS.md · CLAUDE.md",
        type=["md"],
        accept_multiple_files=True,
        label_visibility="collapsed",
    )


def render_submission_form(api_error: str | None) -> Submission | None:
    """사전등록 제출 폼을 렌더링하고 제출 값을 반환한다.

    입력은 제목과 마크다운 가설 두 가지다(#570). 예측 모델링 사전등록 표준
    (arXiv 2311.18807)의 Phase A 항목은 가설 본문 안에 자유롭게 적는다. 데이터·split·
    시드는 실험 간 비교가 성립하도록 서버가 고정하므로 아래에 읽기 전용으로 보여준다.
    """
    st.markdown('<p class="workbench-kicker">AUTORESEARCH / NEW EXPERIMENT</p>', unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown("### 실험을 사전등록합니다")
        st.caption(
            "제출하면 `[AR]` 이슈가 열리고 `auto-experiment` label이 붙습니다. "
            "제목만 따로 받고, 나머지는 모두 가설 본문에 자유롭게 적습니다."
        )
        if api_error:
            st.error(api_error)

        title = st.text_input(
            "실험 제목",
            key="submission-title",
            placeholder="예: 조회수 비율 피처 실험",
            help="이슈 제목이 여기서 만들어집니다. 실험 브랜치 이름은 이슈 번호로 정해집니다.",
        )

        st.markdown("**가설**")
        st.caption(
            "배경 · 근거 · 바꿀 피처 · 참고 링크를 원하는 구조로 적으세요. "
            "미리보기는 편집창 밖을 클릭하거나 Ctrl+Enter를 누를 때 갱신됩니다."
        )
        hypothesis = _render_hypothesis_editor()

        _render_agent_instructions()

        with st.expander("서버가 고정하는 값"):
            st.caption(
                "실험 간 비교가 성립하려면 데이터와 분할이 같아야 하므로 입력받지 않습니다."
            )
            st.markdown(
                "- 랜덤 시드: `42..44` (3개, 데모 스코프)\n"
                "- 비교 대상: 동일 조건 baseline 재학습\n"
                "- Split 시드 · Test/Validation 비율 · 데이터셋 스냅샷 · 학습 설정 참조\n"
                "- 대상 기간: 발행 시점 기준 최근 30일 (KST)"
            )

        # 폼을 쓰지 않는다. 마크다운 편집기가 폼 밖에 있어야 미리보기가 갱신되는데,
        # 제출 버튼만 폼에 남기면 제목·가설이 서로 다른 rerun에서 읽혀 값이 어긋난다.
        submitted = st.button("사전등록하고 이슈 발행", type="primary")

    if not submitted:
        return None
    return Submission(title=title.strip(), hypothesis=hypothesis.strip())


def render_pending_publication_actions() -> tuple[bool, bool]:
    """실패한 이슈 발행을 저장 입력으로 재시도하거나 폐기하는 동작을 렌더링한다."""
    with st.container(border=True):
        st.warning(
            "Experiment는 생성됐지만 이슈 발행이 완료되지 않았습니다. "
            "저장된 입력으로 다시 시도하거나 이 등록을 취소할 수 있습니다."
        )
        retry_column, discard_column = st.columns(2)
        retry = retry_column.button(
            "이슈 발행 다시 시도",
            type="primary",
            use_container_width=True,
        )
        discard = discard_column.button(
            "실패한 등록 취소하고 새 가설 작성",
            use_container_width=True,
        )
    return retry, discard


def render_publication_result(publication: IssuePublication) -> None:
    """발행된 이슈 좌표를 보여준다."""
    st.success(
        f"이슈 #{publication.issue_number}가 열렸습니다 · 실험 브랜치 "
        f"`{publication.issue_branch}`"
    )
    st.markdown(f"[GitHub에서 열기]({publication.issue_url})")


def render_empty_workbench() -> None:
    """선택된 Experiment가 없을 때 빈 관찰 패널을 렌더링한다."""
    with st.container(border=True):
        st.markdown("### 실험 관찰 영역")
        st.info("좌측 실험 기록에서 항목을 선택해 주세요.")
        st.caption("실행기가 연결되면 상태, 이벤트, 원본 로그, 평가 결과가 이곳에 실시간으로 표시됩니다.")


def render_add_hypothesis_button() -> bool:
    """sidebar 최상단의 가설 추가 화면 전환 버튼을 렌더링한다."""
    return st.sidebar.button(
        "+ 가설 추가하기",
        type="primary",
        use_container_width=True,
    )


def render_experiment_refresh_button() -> bool:
    """sidebar의 Experiment 목록 수동 새로고침 버튼을 렌더링한다."""
    return st.sidebar.button("실험 목록 새로고침", use_container_width=True)


def render_experiment_list(
    experiments: Sequence[Experiment],
    selected_id: str | None,
    *,
    on_change: Callable[[], None] | None = None,
) -> str | None:
    """sidebar Experiment 목록을 렌더링하고 현재 선택 ID를 반환한다."""
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
        on_change=on_change,
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
        _render_results(state)
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


# 결과 탭이 카드로 그리는 지표와 화면 이름. 낮을수록 좋은 지표는 delta 색을 뒤집는다.
_METRIC_CARDS: tuple[tuple[str, str, bool], ...] = (
    ("roc_auc", "ROC-AUC", False),
    ("log_loss", "LogLoss", True),
    ("brier", "Brier", True),
)

# 리포트 iframe의 고정 높이(px). 자동 리사이즈를 넣지 않는다 — 실패하면 리포트가 안
# 보이는 실패 모드만 늘어난다. Streamlit이 srcdoc iframe의 스크롤을 항상 켜므로 긴
# 리포트는 내부에서 스크롤된다.
_REPORT_HEIGHT_PX = 620


def _render_results(state: WorkbenchState) -> None:
    """결과 탭에 경고·지표 카드·리포트 본문을 그린다.

    **지표와 리포트의 결손은 독립이다** — 리포트가 없어도 카드는 나오고, 지표가 없어도
    본문은 나온다. 지표를 iframe 밖 Streamlit 위젯으로 두는 이유가 그것이다.
    """
    metrics = state.experiment.metric_summary if state.experiment else None
    _render_split_warning(metrics)
    _render_metric_cards(metrics)
    _render_report(state)


def _render_split_warning(metrics: dict[str, object] | None) -> None:
    """두 조건의 테스트셋이 갈렸으면 지표보다 **위**에 경고한다.

    숫자는 멀쩡해 보이므로 경고가 지표 아래에 있으면 읽는 사람이 delta를 먼저 믿는다.
    """
    if not metrics or metrics.get("split_matches") is not False:
        return
    st.warning(
        "두 조건의 테스트셋이 다릅니다 — 이 delta는 변경의 효과로 읽을 수 없습니다."
    )


def _render_metric_cards(metrics: dict[str, object] | None) -> None:
    """조건별 평균과 짝지은 delta를 카드로 그린다.

    숫자는 전부 `metric_summary`에서 온다 — **에이전트가 쓴 텍스트를 파싱하지 않는다.**
    seed별 delta는 요약에 없고 전문(GCS)에만 있으므로 그리지 않는다.
    """
    if not metrics:
        st.caption("아직 평가 전입니다.")
        return
    conditions = metrics.get("conditions")
    paired = metrics.get("paired")
    if not isinstance(conditions, dict) or not isinstance(paired, dict):
        st.caption("지표 요약 형식을 읽을 수 없습니다.")
        return
    candidate = conditions.get("candidate")
    baseline = conditions.get("baseline")
    columns = st.columns(len(_METRIC_CARDS))
    for column, (name, label, lower_is_better) in zip(columns, _METRIC_CARDS):
        summary = paired.get(name)
        mean = summary.get("mean") if isinstance(summary, dict) else None
        error = summary.get("standard_error") if isinstance(summary, dict) else None
        value = candidate.get(name) if isinstance(candidate, dict) else None
        with column:
            st.metric(
                label,
                f"{float(value):.4f}" if isinstance(value, (int, float)) else "—",
                delta=f"{float(mean):+.4f}" if isinstance(mean, (int, float)) else None,
                delta_color="inverse" if lower_is_better else "normal",
            )
            # seed가 하나면 표본 표준편차가 정의되지 않아 `None`이다. 0으로 보이면
            # "변동이 없다"로 읽히므로 표기 자체를 생략한다.
            if isinstance(error, (int, float)):
                st.caption(f"표준오차 ±{float(error):.4f}")
            if isinstance(baseline, dict) and isinstance(baseline.get(name), (int, float)):
                st.caption(f"baseline {float(baseline[name]):.4f}")


def _render_report(state: WorkbenchState) -> None:
    """리포트 본문을 고정 높이 iframe에 그린다.

    네 갈래를 구분한다. `report_error`가 최우선인 이유는, 실패했는데 "리포트가
    없습니다"로 보이면 **없는 것과 못 받은 것이 구별되지 않기** 때문이다.
    """
    if state.report_error is not None:
        st.warning(f"리포트를 불러오지 못했습니다 — {state.report_error}")
        return
    if state.experiment is None or state.experiment.status not in REPORT_STATUSES:
        return
    if state.report_loaded_for != state.selected_id:
        st.caption("리포트를 불러오는 중입니다.")
        return
    if not state.report_markdown:
        st.caption("아직 리포트가 없습니다.")
        return
    st.iframe(report_document(state.report_markdown), height=_REPORT_HEIGHT_PX)


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
