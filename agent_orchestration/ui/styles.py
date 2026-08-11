"""Streamlit workbench의 상태 표현 규칙을 제공한다.

[파이프라인]
Experiment 상태와 원본 Log를 사람이 읽는 화면으로 변환하는 표현 계층이다. API 호출과
session state는 담당하지 않는다.

[기능]
상태 배지 CSS, Streamlit 테마 변수를 따르는 페이지 스타일, 원본 Log의 고정폭 표시 형식을
제공한다.

[비책임]
사용자 입력 HTML 처리, API 응답 파싱, Event/Log 영속화.
"""

from __future__ import annotations

from agent_orchestration.ui.models import status_color, status_label


def workbench_css() -> str:
    """현재 Streamlit 테마에 맞춰 적용할 CSS를 반환한다."""
    return """
    <style>
      .stApp {
        background: var(--background-color);
        color: var(--text-color);
        font-family: "Avenir Next", "Pretendard", "Noto Sans KR", sans-serif;
      }
      [data-testid="stAppViewContainer"],
      [data-testid="stHeader"] { background: var(--background-color); }
      [data-testid="stSidebar"] {
        background: var(--secondary-background-color);
        border-right: 1px solid color-mix(in srgb, var(--text-color) 18%, transparent);
      }
      [data-testid="stSidebar"] .stRadio label { border-radius: 0.45rem; padding: 0.28rem 0.15rem; }
      .block-container { max-width: 1440px; padding-top: 2.4rem; padding-bottom: 3rem; }
      .stApp input,
      .stApp textarea {
        background: var(--secondary-background-color) !important;
        color: var(--text-color) !important;
      }
      .stApp textarea::placeholder,
      .stApp input::placeholder { color: var(--text-color) !important; opacity: 0.78; }
      .stApp [data-testid="stMarkdownContainer"] .status-badge { color: #ffffff !important; }
      .stApp pre,
      .stApp pre *,
      .stApp code,
      .stApp code * { background: #132c3b !important; color: #ecf5f1 !important; }
      [data-testid="stCaptionContainer"],
      [data-testid="stCaptionContainer"] p { color: var(--text-color) !important; }
      .workbench-kicker {
        color: var(--primary-color);
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 0.73rem;
        font-weight: 700;
        letter-spacing: 0.12em;
        margin-bottom: 0.35rem;
        text-transform: uppercase;
      }
      .workbench-title { color: var(--text-color); font-family: Georgia, "Noto Serif KR", serif; font-size: 2.85rem; font-weight: 600; letter-spacing: -0.045em; line-height: 1.12; margin-bottom: 0.65rem; }
      .status-badge { border-radius: 999px; display: inline-block; font-size: 0.75rem; font-weight: 700; letter-spacing: 0.03em; padding: 0.32rem 0.68rem; }
      [data-testid="stMetric"],
      [data-testid="stVerticalBlockBorderWrapper"] {
        background: var(--secondary-background-color);
        border: 1px solid color-mix(in srgb, var(--text-color) 18%, transparent);
        border-radius: 0.65rem;
      }
      [data-testid="stMetric"] { padding: 0.55rem 0.7rem; }
      [data-baseweb="tab-list"] { gap: 0.75rem; }
      [data-baseweb="tab"] { font-weight: 700; padding-left: 0.1rem; padding-right: 0.1rem; }
      @media (max-width: 760px) {
        .block-container { padding-top: 1.35rem; }
        .workbench-title { font-size: 2rem; }
      }
    </style>
    """


def status_badge(status: str) -> str:
    """안전한 내부 상태값으로 생성한 status badge HTML을 반환한다."""
    return (
        f'<span class="status-badge" style="background:{status_color(status)}">'
        f"{status_label(status)}</span>"
    )
