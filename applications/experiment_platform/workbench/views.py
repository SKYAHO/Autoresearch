"""Streamlit Experiment Workbench의 화면 컴포넌트를 렌더링한다.

[파이프라인]
사용자가 실험을 사전등록하고 Experiment API가 기록한 상태·Event·Log를 관찰하는 UI
구간을 담당한다. API 호출과 polling state 전이는 app/client 모듈이 담당한다.

[기능]
sidebar 탐색으로 분리된 사전등록 화면과 상세 화면의 컴포넌트를 렌더링한다. 사전등록
제출 폼, 실패한 이슈 발행의 재시도·취소 동작과 발행 결과 표시, 실험 선택 목록, 빈 관찰
패널, 상태 타임라인, 결과·Event·원본 Log 탭, KST 시각이 포함된 요약 패널을 제공한다.
결과 탭의 지표 카드와 리포트 HTML 렌더, 실행 비용 영역도 이 모듈이 담당한다.

지표를 그리는 곳은 **결과 탭 하나**다. 인스펙터의 "실행 요약"은 시드·테스트셋 일치
여부 같은 실행 조건만 적는다(#657).

병렬 실행 현황 보드도 이 모듈이 그린다 — 실행 중·대기·완료 탭과 실험별 진행 단계
카드다. 단계는 최신 로그의 `log_type`에서 온다(#671).

[비책임]
HTTP 인증, API 오류 분류, 상태 기록, Agent 실행, 이슈 본문 조립과 GitHub 이슈 생성
(모두 API 서버의 책임이다).
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Sequence
from datetime import datetime

import streamlit as st

from agent_orchestration.ui.models import (
    BOARD_RUNNING_STATUSES,
    BOARD_WAITING_STATUSES,
    Event,
    EXECUTOR_STAGES,
    Experiment,
    IssuePublication,
    REPORT_STATUSES,
    Step,
    Submission,
    stage_index,
    stage_label,
    status_color,
    status_label,
    status_tone,
    step_kind_label,
    step_status_color,
    step_status_label,
)
from agent_orchestration.ui.report import report_document
from agent_orchestration.ui.state import PendingPublication, WorkbenchState
from agent_orchestration.ui.styles import fact_row, status_badge
from agent_orchestration.ui.time import format_short_time, format_time


HYPOTHESIS_KEY = "submission-hypothesis"

# 한 번에 낼 수 있는 가설 수. 배포된 launcher의 동시 실행 상한과 같은 값이라 다섯 개를
# 내면 전부 곧바로 돌기 시작한다. 상한이 바뀌어도 이 값은 "한 화면에서 쓸 만한 개수"라
# 자동으로 따라가지 않는다.
_MAX_SUBMISSION_COUNT = 5

_HYPOTHESIS_PLACEHOLDER = "마크다운 형식으로 가설을 작성해 주세요"



def _one_line(text: str) -> str:
    """줄바꿈과 연속 공백을 한 칸으로 접는다. **글자는 버리지 않는다.**

    한때 여기서 글자 수까지 잘랐다. 32자·34자·50자를 차례로 시도했지만 어느 값이든
    결과는 같았다 — 목록 전체가 `…`로 끝나고, 같은 파라미터를 건드린 실험들이 서로
    구별되지 않았다(#657). 잘라야 할 이유도 없었다. 사이드바 라벨도 인스펙터 값도
    줄바꿈이 되는 자리라, 접기만 하면 폭에 맞춰 알아서 흐른다.
    """
    return " ".join(text.split())


# 문장 끝. `0.05`의 소수점은 뒤에 공백이 없으므로 걸리지 않는다.
_SENTENCE_END = re.compile(r"\.(?=\s|$)")

# 요약 줄이 쓸 수 있는 글자 수. 첫 문장이 이보다 길면 그때는 자른다.
#
# 자르지 않고 전문을 넣어 봤더니 항목 하나가 스무 줄이 되어 목록으로 쓸 수 없었고,
# 34·50자로 짧게 자르니 같은 파라미터를 건드린 실험들이 모두 같은 접두사로 끝나
# 구별되지 않았다(#657). 윗줄에 시각이 붙어 중복이 갈리므로 요약은 더 짧아도 된다.
_LIST_LABEL_MAX = 72


def _list_label(status: str, hypothesis: str, started_at: datetime) -> str:
    """사이드바 목록 한 항목을 두 줄로 만든다.

    윗줄은 상태와 시각, 아랫줄은 가설 요약이다. 한 줄에 모두 이어 붙이면 상태가
    문장에 묻혀 목록을 훑을 수 없다 — 25개를 세로로 훑는 화면에서 눈이 먼저 찾는
    것은 상태와 시각이다.

    위젯 라벨에는 HTML을 넣을 수 없어 Streamlit 마크다운 색 문법으로 상태를
    물들인다. 줄바꿈은 마크다운 hard break(공백 두 개 + 개행)다.

    요약은 문장 경계에서 끊는다. 마침표로 끝나니 잘린 티가 나지 않고, 실험을 가르는
    값도 남는다.
    """
    text = _one_line(hypothesis)
    sentence_end = _SENTENCE_END.search(text)
    if sentence_end:
        text = text[: sentence_end.end()]
    if len(text) > _LIST_LABEL_MAX:
        head = text[:_LIST_LABEL_MAX]
        boundary = head.rfind(" ")
        if boundary >= _LIST_LABEL_MAX // 2:
            head = head[:boundary]
        text = head.rstrip() + "…"
    tone = status_tone(status)
    return f":{tone}[{status}]  ·  {format_short_time(started_at)}  \n{text}"


def _render_hypothesis_editor(key: str) -> str:
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
            key=key,
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


def render_submission_form(api_error: str | None) -> list[Submission] | None:
    """사전등록 제출 폼을 렌더링하고 제출 값 목록을 반환한다.

    입력은 제목과 마크다운 가설 두 가지다(#570). 예측 모델링 사전등록 표준
    (arXiv 2311.18807)의 Phase A 항목은 가설 본문 안에 자유롭게 적는다. 데이터·split·
    시드는 실험 간 비교가 성립하도록 서버가 고정하므로 아래에 읽기 전용으로 보여준다.

    가설을 여러 개 받는다(#671). 동시 실행 상한만큼 한 번에 내야 "여러 가설이 동시에
    검증된다"는 장면이 성립하고, 그때마다 폼을 다시 채우는 것은 그 장면을 만들기 위한
    수고일 뿐이다. 개수는 기본 1이라 한 개만 낼 때의 화면은 그대로다.

    가설마다 탭을 쓴다. 세로로 쌓으면 다섯 개째 편집창이 화면 두 배 아래에 있다.
    Streamlit은 탭 내용을 모두 렌더링하므로 안 보이는 탭의 입력도 함께 읽힌다.
    """
    st.markdown('<p class="workbench-kicker">AUTORESEARCH / NEW EXPERIMENT</p>', unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown("### 실험을 사전등록합니다")
        st.caption(
            "제출하면 가설마다 `[AR]` 이슈가 열리고 `auto-experiment` label이 붙습니다. "
            "제목만 따로 받고, 나머지는 모두 가설 본문에 자유롭게 적습니다."
        )
        if api_error:
            st.error(api_error)

        count = st.slider(
            "한 번에 제출할 가설 수",
            min_value=1,
            max_value=_MAX_SUBMISSION_COUNT,
            value=1,
            key="submission-count",
            help="여러 개를 내면 슬롯이 있는 만큼 동시에 실행되고, 나머지는 대기합니다.",
        )

        drafts: list[Submission] = []
        tabs = st.tabs([f"가설 {order}" for order in range(1, count + 1)])
        for order, tab in enumerate(tabs, start=1):
            with tab:
                drafts.append(_render_submission_draft(order))

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
        label = (
            "사전등록하고 이슈 발행"
            if count == 1
            else f"가설 {count}개 사전등록하고 이슈 발행"
        )
        submitted = st.button(label, type="primary")

    if not submitted:
        return None
    return drafts


def _render_submission_draft(order: int) -> Submission:
    """가설 하나의 제목·본문 입력을 렌더링한다.

    위젯 key에 순서를 넣는다. 고정 key를 쓰면 탭이 늘어날 때 모든 탭이 같은 값을
    공유해 다섯 개를 써도 같은 가설 다섯 개가 제출된다.

    첫 번째만 예전 key를 그대로 쓴다 — 개수를 늘렸다 줄여도 쓰던 내용이 남는다.
    """
    suffix = "" if order == 1 else f"-{order}"
    title = st.text_input(
        "실험 제목",
        key=f"submission-title{suffix}",
        placeholder="예: 조회수 비율 피처 실험",
        help="이슈 제목이 여기서 만들어집니다. 실험 브랜치 이름은 이슈 번호로 정해집니다.",
    )
    st.markdown("**가설**")
    st.caption(
        "배경 · 근거 · 바꿀 피처 · 참고 링크를 원하는 구조로 적으세요. "
        "미리보기는 편집창 밖을 클릭하거나 Ctrl+Enter를 누를 때 갱신됩니다."
    )
    hypothesis = _render_hypothesis_editor(f"{HYPOTHESIS_KEY}{suffix}")
    return Submission(title=title.strip(), hypothesis=hypothesis.strip())


def render_pending_publication_actions(
    pending: Sequence[PendingPublication],
) -> tuple[bool, bool]:
    """실패한 이슈 발행을 저장 입력으로 재시도하거나 폐기하는 동작을 렌더링한다.

    **어느 것이 남았는지 적는다.** 묶음 제출에서는 일부만 실패하므로, 건수만 알려주면
    사용자가 무엇을 다시 내야 하는지 알 수 없다.
    """
    with st.container(border=True):
        st.warning(
            f"Experiment {len(pending)}건은 생성됐지만 이슈 발행이 완료되지 않았습니다. "
            "저장된 입력으로 다시 시도하거나 이 등록을 취소할 수 있습니다."
        )
        for item in pending:
            st.caption(f"· {_one_line(item.submission.title) or '(제목 없음)'}")
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


def render_publication_result(publications: Sequence[IssuePublication]) -> None:
    """발행된 이슈 좌표를 보여준다."""
    if len(publications) == 1:
        publication = publications[0]
        st.success(
            f"이슈 #{publication.issue_number}가 열렸습니다 · 실험 브랜치 "
            f"`{publication.issue_branch}`"
        )
        st.markdown(f"[GitHub에서 열기]({publication.issue_url})")
        return
    numbers = ", ".join(f"#{item.issue_number}" for item in publications)
    st.success(f"이슈 {len(publications)}건이 열렸습니다 · {numbers}")
    for publication in publications:
        st.markdown(
            f"[#{publication.issue_number} 열기]({publication.issue_url}) · "
            f"`{publication.issue_branch}`"
        )


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


def render_board_button() -> bool:
    """sidebar의 병렬 실행 현황 보드 진입 버튼을 렌더링한다."""
    return st.sidebar.button("실행 현황 보기", use_container_width=True)


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
        experiment.id: _list_label(
            experiment.status, experiment.hypothesis, experiment.created_at
        )
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
        # `st.title`이 아니다. 가설은 여러 문장짜리 본문이라 H1으로 그리면 세로
        # 400px를 먹고 관찰 보드를 화면 밖으로 밀어냈다(#657).
        #
        # 여기는 `unsafe_allow_html`을 켜므로 **가설은 사용자 입력**임을 잊지 않는다.
        # `st.title`이 해 주던 escape를 직접 해야 한다 — `_render_steps`가 step_type에
        # 같은 처리를 하는 것과 같은 이유다.
        st.markdown(
            f'<p class="workbench-hypothesis">{html.escape(experiment.hypothesis)}</p>',
            unsafe_allow_html=True,
        )
    with status_column:
        st.markdown(status_badge(experiment.status), unsafe_allow_html=True)
        st.caption(f"마지막 갱신 {format_time(experiment.updated_at)}")

    # 관찰 보드가 전체 폭을 쓰고 보조 패널 셋은 그 아래로 간다. 우측 인스펙터 열로
    # 두면 원본 로그·작업 단계처럼 가로가 필요한 내용이 좁은 열에서 접히는데, 정작
    # 그 옆의 보조 패널이 더 길어 화면 오른쪽만 계속 늘어났다.
    with st.container(border=True):
        st.markdown("#### 관찰 보드")
        _render_tabs(state)

    summary_column, timeline_column, metadata_column = st.columns(3, gap="large")
    with summary_column:
        with st.container(border=True):
            st.markdown("#### 실행 요약")
            _render_metrics(experiment.metric_summary)
    with timeline_column:
        with st.container(border=True):
            st.markdown("#### 진행 단계")
            _render_timeline(state.events[-8:], experiment.status)
    with metadata_column:
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
        # 상태값은 서버가 닫아 둔 집합이지만 `unsafe_allow_html` 경로이므로 escape한다.
        st.markdown(
            '<div class="timeline-step">'
            f'<span class="timeline-from">{html.escape(source)}</span>'
            '<span class="timeline-arrow">&rarr;</span>'
            f'<span class="timeline-to">{html.escape(event.to_status)}</span>'
            f'<span class="timeline-time">{format_time(event.created_at)}</span>'
            "</div>",
            unsafe_allow_html=True,
        )
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
        # `OTHER`는 라벨을 그리지 않는다. 컨테이너 진행 단계(#688)가 전부 이 값이라 그대로
        # 두면 모든 줄이 "기타"로 시작해, 정작 무엇인지 알려주는 step_type을 가린다. 의미
        # 단계(`TRAIN` 등)가 들어오면 그때는 라벨이 붙는다.
        kind = (
            ""
            if step.step_kind == "OTHER"
            else f"{html.escape(step_kind_label(step.step_kind))} "
        )
        # status를 색뿐 아니라 글자로도 낸다. 작은 불릿에서 회색(`STARTED`)과
        # 파랑(`PROGRESS`)은 구분이 어렵고, 색각 이상에서는 초록·빨강이 겹쳐 정보가 사라진다.
        #
        # step_type은 에이전트가 정하는 자유 문자열이라 이 마크업에서 유일하게 신뢰 경계를
        # 넘는다. step_kind·status는 서버 CHECK로 닫혀 있지만 라벨 폴백이 원문을 그대로
        # 내보내므로 함께 escape한다. color는 닫힌 집합, format_time은 포맷된 timestamp라
        # 안전하다.
        st.markdown(
            f"<span style='color:{color};font-weight:600'>&#9679; {kind}"
            f"{html.escape(step.step_type)}</span> "
            f"<span style='color:{color};opacity:0.85'>"
            f"{html.escape(step_status_label(step.status))}</span> "
            f"<span style='opacity:0.5'>· {format_time(step.updated_at)}</span>",
            unsafe_allow_html=True,
        )
        # message가 있을 때만 아래 줄을 쓴다. 없을 때 kind·type으로 대신하던 폴백은 위
        # 줄이 이미 그 둘을 담게 되면서 같은 내용을 두 번 그리는 일이 됐다.
        if step.message:
            st.write(step.message)
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


# 인스펙터 "실행 요약"이 쓰는 화면 이름. 여기 없는 키는 원문 그대로 적는다.
_RUN_FACT_LABELS: dict[str, str] = {
    "primary_metric": "주요 지표",
    "dataset_fingerprint": "데이터셋 지문",
    "results_uri": "결과 원본",
    "contract_version": "스냅샷 계약",
}

# `_render_metrics`가 앞에서 전용 문구로 이미 그린 키.
_RUN_FACTS_RENDERED_ABOVE = frozenset({"seeds", "split_matches"})

# 사람이 읽는 문장이 아니라 식별자인 값. 고정폭으로 그려야 자릿수가 흔들리지 않는다.
_RUN_FACTS_AS_CODE = frozenset(
    {"primary_metric", "dataset_fingerprint", "results_uri", "contract_version"}
)

# 결과 탭의 지표 카드가 소유하는 키. 인스펙터에서 중복해 그리지 않는다.
_METRICS_OWNED_BY_RESULTS_TAB = frozenset({"conditions", "paired"})

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
    """결과 탭에 경고·지표 카드·리포트 본문·실행 비용을 그린다.

    비용을 리포트 **아래**에 두는 이유는 그것이 가설 판정이 아니기 때문이다. 리포트가
    답하는 질문("가설이 섰는가")과 비용이 답하는 질문("얼마가 들었는가")은 다르고,
    읽는 순서도 그 순서다.

    **지표와 리포트의 결손은 독립이다** — 리포트가 없어도 카드는 나오고, 지표가 없어도
    본문은 나온다. 지표를 iframe 밖 Streamlit 위젯으로 두는 이유가 그것이다.
    """
    metrics = state.experiment.metric_summary if state.experiment else None
    _render_split_warning(metrics)
    _render_metric_cards(metrics)
    _render_report(state)
    _render_cost(state)


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
    # 문구는 `report_checked_for`로 고른다. `report_loaded_for`로 고르면 리포트가 정말
    # 없는 실험은 그 표식이 영영 세워지지 않아 "불러오는 중"에서 내려오지 못한다
    # (`state.record_report`의 `[재-정정]` 참고).
    if state.report_checked_for != state.selected_id:
        st.caption("리포트를 불러오는 중입니다.")
        return
    if not state.report_markdown:
        st.caption("아직 리포트가 없습니다.")
        return
    st.iframe(report_document(state.report_markdown), height=_REPORT_HEIGHT_PX)


def _render_cost(state: WorkbenchState) -> None:
    """실행 비용을 리포트 아래 접이식 영역에 그린다.

    `report.md`가 아니라 여기 두는 이유는 두 가지다. 리포트를 쓰는 에이전트는
    `metrics.json`에 없는 숫자를 쓰지 못하도록 지시받고 있고, **리포트를 쓰는 그 실행의
    토큰은 리포트를 다 쓴 뒤에야 확정**되므로 원리적으로 본문에 들어갈 수 없다.

    분해가 없는 실험에 금액을 만들어 붙이지 않는다 — 정가로 매기면 실제보다 몇 배 큰
    값이 사실처럼 보인다. 총량만 적고 왜 금액이 없는지 밝힌다.
    """
    if state.cost_error is not None:
        st.warning(f"실행 비용을 불러오지 못했습니다 — {state.cost_error}")
        return
    if state.experiment is None or state.experiment.status not in REPORT_STATUSES:
        return
    cost = state.cost
    if cost is None:
        return
    with st.expander("실행 비용", expanded=False):
        columns = st.columns(3)
        columns[0].metric(
            "실행 시간",
            "—"
            if cost.wall_clock_seconds is None
            else f"{cost.wall_clock_seconds / 60:,.1f}분",
        )
        columns[1].metric(
            "컴퓨트",
            "—" if cost.compute_usd is None else f"${cost.compute_usd:,.4f}",
        )
        columns[2].metric(
            "토큰",
            "—" if cost.token_usd is None else f"${cost.token_usd:,.4f}",
        )
        if not cost.breakdown_available:
            st.caption(
                f"Codex 토큰 총 {cost.total_tokens:,}개. 이 실험은 과금 구분이 남기 전에"
                " 실행되어 금액을 환산하지 않았습니다."
            )
            return
        st.table(
            {
                "stage": [stage.stage for stage in cost.stages],
                "input": [f"{stage.input_tokens:,}" for stage in cost.stages],
                "캐시 적중": [f"{stage.cached_input_tokens:,}" for stage in cost.stages],
                "신규 input": [f"{stage.fresh_input_tokens:,}" for stage in cost.stages],
                "output": [f"{stage.output_tokens:,}" for stage in cost.stages],
            }
        )
        if cost.token_usd_without_cache and cost.token_usd is not None:
            saved = cost.token_usd_without_cache - cost.token_usd
            ratio = 1 - cost.token_usd / cost.token_usd_without_cache
            st.caption(
                f"프롬프트 캐싱으로 ${saved:,.4f}({ratio:.0%})를 덜었습니다."
                " 구독 인증으로 실행 중이라 실제 지출은 없습니다."
            )


def _render_metrics(metrics: dict[str, object] | None) -> None:
    """인스펙터 "실행 요약"에 **실행 조건**을 적는다. 지표는 여기 없다.

    지표와 delta는 결과 탭의 `_render_metric_cards`가 소유한다. 이 패널은 한때
    `metric_summary` 전체를 `st.code(json.dumps(...))`로 덤프했는데, 그러면 같은 값이
    한쪽은 카드로 한쪽은 날 JSON으로 두 번 나오고, 검은 블록이 패널을 채운 채 숫자가
    가로로 잘렸다(#657). 그래서 `conditions`·`paired`는 여기서 그리지 않는다.

    모르는 키는 버리지 않는다 — 스냅샷 계약이 넓어져도 화면에서 조용히 사라지지
    않도록 스칼라는 그대로, 중첩 값은 항목 수로 적는다.
    """
    if not metrics:
        st.caption("아직 평가 전입니다.")
        return

    rows: list[str] = []
    seeds = metrics.get("seeds")
    if isinstance(seeds, list) and seeds:
        rows.append(
            fact_row("시드", ", ".join(str(seed) for seed in seeds), code=True)
        )

    split_matches = metrics.get("split_matches")
    if split_matches is False:
        rows.append(fact_row("테스트셋", "불일치 — delta를 효과로 읽을 수 없음"))
    elif split_matches is True:
        rows.append(fact_row("테스트셋", "두 조건 동일"))

    for key, value in metrics.items():
        if key in _RUN_FACTS_RENDERED_ABOVE or key in _METRICS_OWNED_BY_RESULTS_TAB:
            continue
        label = _RUN_FACT_LABELS.get(key, key)
        if isinstance(value, (dict, list)):
            rows.append(fact_row(label, f"항목 {len(value)}개"))
        else:
            rows.append(
                fact_row(
                    label,
                    _one_line(str(value)),
                    code=key in _RUN_FACTS_AS_CODE,
                )
            )

    # 한 번에 그린다. 행마다 `st.markdown`을 부르면 Streamlit이 사이에 블록 간격을
    # 넣어 표로 읽히지 않는다.
    st.markdown(
        "".join(rows)
        + '<p class="fact-note">지표와 delta는 결과 탭에 있습니다.</p>',
        unsafe_allow_html=True,
    )


# 보드 카드의 가설 요약 길이. 카드 폭이 좁아 목록 라벨보다 짧게 잡는다.
_BOARD_SUMMARY_MAX = 62

# 한 줄에 놓는 카드 수. 동시 실행 상한이 5라 5개가 한 줄에 들어가야 "동시에 돌고
# 있다"가 한눈에 읽힌다.
_BOARD_COLUMNS = 5

# 완료 탭이 보여줄 최근 실험 수. 전부 그리면 세션이 길수록 보드가 무거워진다.
_BOARD_DONE_LIMIT = 10


def _board_summary(hypothesis: str) -> str:
    """카드에 넣을 가설 요약. 문장 경계를 우선하고 넘치면 자른다."""
    text = _one_line(hypothesis)
    sentence_end = _SENTENCE_END.search(text)
    if sentence_end:
        text = text[: sentence_end.end()]
    if len(text) <= _BOARD_SUMMARY_MAX:
        return text
    head = text[:_BOARD_SUMMARY_MAX]
    boundary = head.rfind(" ")
    if boundary >= _BOARD_SUMMARY_MAX // 2:
        head = head[:boundary]
    return head.rstrip() + "…"


def _render_board_card(
    experiment: Experiment, stage: str | None, *, show_stage: bool
) -> bool:
    """보드 카드 하나를 그리고 상세 보기 버튼이 눌렸는지 반환한다."""
    with st.container(border=True):
        color = status_color(experiment.status)
        st.markdown(
            f'<span class="status-badge" style="--badge:{color}">'
            f"{status_label(experiment.status)}</span>"
            # 경과 시간이 아니라 **제출 시각(KST)** 을 적는다. 경과는 `created_at`
            # 기준이라 슬롯을 기다린 시간까지 포함하는데, 카드에 "얼마나 돌고 있나"로
            # 읽히면 `kubectl`의 pod AGE와 체계적으로 어긋난다. 시작 시각을 담은
            # 필드가 없으므로 있는 사실(제출 시각)만 적는다.
            f'<span class="board-meta"> 제출 {format_short_time(experiment.created_at)}</span>',
            unsafe_allow_html=True,
        )
        if show_stage:
            _render_board_stage(stage)
        st.caption(_board_summary(experiment.hypothesis))
        # key를 인덱스가 아니라 실험 id로 만든다. 목록 순서는 상태가 바뀔 때마다
        # 흔들리는데, 인덱스 key를 쓰면 Streamlit이 직전 클릭을 엉뚱한 카드에 붙인다.
        return st.button(
            "상세 보기", key=f"board-open-{experiment.id}", use_container_width=True
        )


def _render_board_stage(stage: str | None) -> None:
    """카드의 진행 단계를 그린다.

    단계를 모르는 경우가 둘이다 — 아직 로그가 없거나(`stage is None`), executor
    단계가 아닌 `log_type`만 온 경우다. 후자를 7단계 어딘가로 우겨넣지 않는다.
    """
    index = stage_index(stage) if stage is not None else None
    if index is None:
        st.caption("단계 기록 대기 중")
        st.progress(0.0)
        return
    st.markdown(f"**{stage_label(stage)}**")
    st.caption(f"{stage} · 단계 {index + 1}/{len(EXECUTOR_STAGES)}")
    st.progress((index + 1) / len(EXECUTOR_STAGES))


def _render_board_grid(
    experiments: Sequence[Experiment],
    stages: dict[str, str],
    *,
    show_stage: bool,
    empty_message: str,
) -> str | None:
    """카드 격자를 그리고 사용자가 연 실험 id를 반환한다."""
    # 같은 id가 두 번 오면 버튼 key가 겹쳐 `StreamlitDuplicateElementKey`로 **페이지
    # 전체**가 죽는다. 목록은 생성 직후 낙관적 insert와 다음 갱신의 전체 교체가
    # 겹치는 자리라, 한 번의 rerun 안에서 중복이 스칠 수 있다. 카드 하나를 덜 그리는
    # 편이 화면이 사라지는 것보다 낫다.
    seen: set[str] = set()
    unique = [
        experiment
        for experiment in experiments
        if not (experiment.id in seen or seen.add(experiment.id))
    ]
    if not unique:
        st.caption(empty_message)
        return None
    opened: str | None = None
    for start in range(0, len(unique), _BOARD_COLUMNS):
        row = unique[start : start + _BOARD_COLUMNS]
        for column, experiment in zip(st.columns(_BOARD_COLUMNS), row):
            with column:
                if _render_board_card(
                    experiment, stages.get(experiment.id), show_stage=show_stage
                ):
                    opened = experiment.id
    return opened


def render_board(state: WorkbenchState) -> str | None:
    """병렬 실행 현황 보드를 그리고 사용자가 연 실험 id를 반환한다.

    화면 전환은 하지 않는다 — `app.py`가 반환값을 받아 `show_experiment`를 부른다.
    views는 상태를 바꾸지 않는다는 기존 경계를 지킨다.
    """
    st.markdown(
        '<p class="workbench-kicker">실행 현황</p>', unsafe_allow_html=True
    )
    running = [e for e in state.experiments if e.status in BOARD_RUNNING_STATUSES]
    waiting = [e for e in state.experiments if e.status in BOARD_WAITING_STATUSES]
    done = [
        e
        for e in state.experiments
        if e.status not in BOARD_RUNNING_STATUSES | BOARD_WAITING_STATUSES
    ]

    running_column, waiting_column, done_column = st.columns(3)
    running_column.metric("동시 실행 중", len(running))
    waiting_column.metric("슬롯 대기", len(waiting))
    done_column.metric("완료", len(done))

    running_tab, waiting_tab, done_tab = st.tabs(
        [f"실행 중 {len(running)}", f"대기 {len(waiting)}", f"완료 {len(done)}"]
    )
    with running_tab:
        opened = _render_board_grid(
            running,
            state.board_stages,
            show_stage=True,
            empty_message="지금 실행 중인 실험이 없습니다.",
        )
    with waiting_tab:
        st.caption(
            "동시 실행 상한이 차 있으면 여기서 기다리다가 슬롯이 나는 대로 시작합니다."
        )
        opened = (
            _render_board_grid(
                waiting,
                state.board_stages,
                show_stage=False,
                empty_message="대기 중인 실험이 없습니다.",
            )
            or opened
        )
    with done_tab:
        opened = (
            _render_board_grid(
                done[:_BOARD_DONE_LIMIT],
                state.board_stages,
                show_stage=False,
                empty_message="완료된 실험이 없습니다.",
            )
            or opened
        )
    return opened
