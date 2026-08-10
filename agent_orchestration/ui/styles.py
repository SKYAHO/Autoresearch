"""Streamlit workbench의 상태 표현 규칙을 제공한다.

[파이프라인]
Experiment 상태와 원본 Log를 사람이 읽는 화면으로 변환하는 표현 계층이다. API 호출과
session state는 담당하지 않는다.

[기능]
상태 배지 CSS, 테마가 다루지 못하는 타이포그래피·레이아웃 CSS, 상태 배지 HTML을
제공한다.

[비책임]
사용자 입력 HTML 처리, API 응답 파싱, Event/Log 영속화. **색·모서리·테두리도 여기가
아니다** — `.streamlit/config.toml`의 `[theme]`가 정본이다.

이 모듈은 한때 `var(--background-color)` 같은 Streamlit 테마 변수를 참조했으나 그
변수들은 존재하지 않았다. Streamlit 1.60은 emotion CSS-in-JS로 계산된 값을 주입할 뿐
전역 CSS 커스텀 속성을 노출하지 않아, 참조한 선언이 전부 무효로 버려졌다(#657).
그래서 남길 CSS의 기준은 하나다 — **테마 설정으로 표현할 수 없는 것만 남긴다.**
"""

from __future__ import annotations

from agent_orchestration.ui.models import status_color, status_label


# 본문 서체. 테마의 `font = "sans-serif"`는 generic family라 한글 자형을 고르지
# 못하므로 여기서 실제 가족 이름을 지정한다.
_BODY_FONT = '"Avenir Next", "Pretendard", "Noto Sans KR", "Malgun Gothic", sans-serif'
_HEADING_FONT = 'Georgia, "Noto Serif KR", serif'


def workbench_css() -> str:
    """테마 설정으로 표현할 수 없는 CSS만 반환한다."""
    return f"""
    <style>
      .stApp {{ font-family: {_BODY_FONT}; }}
      .stApp h1, .stApp h2, .stApp h3, .stApp h4 {{ font-family: {_HEADING_FONT}; }}
      .block-container {{ max-width: 1440px; padding-top: 2.4rem; padding-bottom: 3rem; }}
      .workbench-kicker {{
        color: #146B5C;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 0.73rem;
        font-weight: 700;
        letter-spacing: 0.12em;
        margin-bottom: 0.35rem;
        text-transform: uppercase;
      }}
      /* 가설은 제목이 아니라 본문이다. `st.title`로 그리면 여러 문장짜리 가설이
         H1 크기로 화면을 채워 관찰 보드를 접어버린다(#657). */
      .workbench-hypothesis {{
        font-family: {_HEADING_FONT};
        font-size: 1.35rem;
        font-weight: 500;
        letter-spacing: -0.01em;
        line-height: 1.5;
        margin: 0.1rem 0 0.2rem;
      }}
      .status-badge {{
        border-radius: 999px;
        display: inline-block;
        font-size: 0.75rem;
        font-weight: 700;
        letter-spacing: 0.03em;
        padding: 0.32rem 0.68rem;
      }}
      /* 배지 바탕은 models.py의 짙은 상태색이므로 글자는 항상 흰색이어야 한다. */
      .stApp [data-testid="stMarkdownContainer"] .status-badge {{ color: #FFFFFF !important; }}
      [data-baseweb="tab-list"] {{ gap: 0.75rem; }}
      [data-baseweb="tab"] {{ font-weight: 700; padding-left: 0.1rem; padding-right: 0.1rem; }}
      @media (max-width: 760px) {{
        .block-container {{ padding-top: 1.35rem; }}
        .workbench-hypothesis {{ font-size: 1.15rem; }}
      }}
    </style>
    """


def status_badge(status: str) -> str:
    """안전한 내부 상태값으로 생성한 status badge HTML을 반환한다."""
    return (
        f'<span class="status-badge" style="background:{status_color(status)}">'
        f"{status_label(status)}</span>"
    )
