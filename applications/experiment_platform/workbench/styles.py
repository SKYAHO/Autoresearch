"""Streamlit workbench의 상태 표현 규칙을 제공한다.

[파이프라인]
Experiment 상태와 원본 Log를 사람이 읽는 화면으로 변환하는 표현 계층이다. API 호출과
session state는 담당하지 않는다.

[기능]
상태 배지 CSS와 HTML, 테마가 다루지 못하는 타이포그래피·레이아웃 CSS, 인스펙터가
쓰는 라벨/값 행 HTML을 제공한다.

[비책임]
사용자 입력 HTML 처리, API 응답 파싱, Event/Log 영속화. **색·모서리·테두리도 여기가
아니다** — `.streamlit/config.toml`의 `[theme]`가 정본이다.

이 모듈은 한때 `var(--background-color)` 같은 Streamlit 테마 변수를 참조했으나 그
변수들은 존재하지 않았다. Streamlit 1.60은 emotion CSS-in-JS로 계산된 값을 주입할 뿐
전역 CSS 커스텀 속성을 노출하지 않아, 참조한 선언이 전부 무효로 버려졌다(#657).
그래서 남길 CSS의 기준은 하나다 — **테마 설정으로 표현할 수 없는 것만 남긴다.**
"""

from __future__ import annotations

import html

from applications.experiment_platform.workbench.models import status_color, status_label


# 본문 서체. 테마의 `font = "sans-serif"`는 generic family라 한글 자형을 고르지
# 못하므로 여기서 실제 가족 이름을 지정한다.
_BODY_FONT = (
    '"Noto Sans KR", "Apple SD Gothic Neo", "Malgun Gothic", '
    '"Pretendard", sans-serif'
)
# 세리프는 가설 본문 한 곳에만 쓴다. 화면에서 유일한 산문이라 활자를 달리해 구분한다.
_PROSE_FONT = '"Iowan Old Style", Georgia, "Noto Serif KR", serif'
_MONO_FONT = 'ui-monospace, "SF Mono", SFMono-Regular, Menlo, monospace'

# 본문보다 흐린 보조 텍스트. 테마에는 이 단계가 없어 여기서 고정한다.
_MUTED = "#6B7280"


def workbench_css() -> str:
    """테마 설정으로 표현할 수 없는 CSS만 반환한다."""
    return f"""
    <style>
      .stApp {{ font-family: {_BODY_FONT}; }}
      /* 상단 여백은 Streamlit 헤더(`stHeader`)를 피하려고 크게 잡는다. 그 헤더는
         `position: absolute`에 높이 60px이라 본문 위를 덮는데, 배경색이 페이지와
         같아 덮은 티가 안 나고 글자만 잘려 보인다. 2.2rem(35.2px)으로 줄였더니
         kicker가 8.8px 잘렸다(#657). 60px보다 확실히 큰 값을 쓴다. */
      .block-container {{ max-width: 1380px; padding-top: 5rem; padding-bottom: 3rem; }}

      /* 패널 제목. `#### `를 큰 세리프 제목이 아니라 작은 라벨로 그린다 — 카드
         네 개가 저마다 큰 제목을 이고 있으면 정작 내용이 부속처럼 보인다. */
      .stApp h4 {{
        color: {_MUTED};
        font-family: {_BODY_FONT};
        font-size: 0.75rem;
        font-weight: 600;
        letter-spacing: 0.05em;
        margin: 0 0 0.85rem;
        text-transform: uppercase;
      }}
      .stApp h3 {{ font-size: 1.3rem; font-weight: 600; letter-spacing: -0.015em; }}

      /* `.stApp p.<class>`로 특정성을 올린다. 클래스 하나짜리 선택자는 Streamlit이
         `stMarkdownContainer`의 `p`에 거는 크기 규칙에 밀려 `font-size`만 조용히
         무시됐다 — 색·굵기는 먹는데 크기만 안 먹어 알아채기 어렵다(#657). */
      /* 고정폭 서체를 쓰지 않는다. 한글이 mono 가족에 없어 폴백 자형으로 떨어지면
         자간만 벌어진 채 흐릿하게 보인다. */
      .stApp p.workbench-kicker {{
        color: #4B5563;
        font-family: {_BODY_FONT};
        font-size: 0.72rem;
        font-weight: 600;
        letter-spacing: 0.05em;
        margin-bottom: 0.5rem;
        text-transform: uppercase;
      }}

      /* 상태 전이는 한 줄에 담고 시각을 오른쪽 끝으로 민다. 전이·시각을 두 블록으로
         쌓으면 이벤트 여덟 개가 패널 하나를 통째로 차지한다. */
      .timeline-step {{
        align-items: baseline;
        display: flex;
        font-size: 0.8rem;
        gap: 0.4rem;
        padding: 0.32rem 0;
        border-top: 1px solid #EFF1F1;
      }}
      .timeline-step:first-of-type {{ border-top: none; }}
      .timeline-from {{ color: {_MUTED}; }}
      .timeline-arrow {{ color: #9CA3AF; }}
      .timeline-to {{ font-weight: 600; }}
      .timeline-time {{
        color: {_MUTED};
        font-size: 0.72rem;
        font-variant-numeric: tabular-nums;
        margin-left: auto;
        white-space: nowrap;
      }}
      /* 가설은 제목이 아니라 본문이다. `st.title`로 그리면 여러 문장짜리 가설이
         H1 크기로 화면을 채워 관찰 보드를 접어버린다(#657). */
      .stApp p.workbench-hypothesis {{
        font-family: {_PROSE_FONT};
        font-size: 1.3rem;
        font-weight: 400;
        letter-spacing: -0.005em;
        line-height: 1.55;
        margin: 0.1rem 0 0.2rem;
        text-wrap: pretty;
      }}

      /* 상태 배지. 꽉 찬 원색 바탕에 흰 볼드는 강해서 화면의 다른 정보를 눌렀다.
         상태색은 글자와 옅은 바탕에만 쓰고 면적을 줄인다. */
      .status-badge {{
        background: color-mix(in srgb, var(--badge) 9%, transparent);
        border: 1px solid color-mix(in srgb, var(--badge) 26%, transparent);
        border-radius: 0.3rem;
        color: var(--badge);
        display: inline-block;
        font-size: 0.72rem;
        font-weight: 600;
        letter-spacing: 0.01em;
        padding: 0.24rem 0.5rem;
        white-space: nowrap;
      }}

      /* 보드 카드에서 배지 옆에 붙는 경과 시간. 크기를 주지 않으면 본문 크기로
         그려져 배지와 같은 줄에 들어가지 못하고 접힌다. */
      .board-meta {{
        color: {_MUTED};
        font-size: 0.72rem;
        margin-left: 0.35rem;
        white-space: nowrap;
      }}

      /* 인스펙터의 라벨/값 행. 캡션을 여러 줄 쌓으면 라벨과 값이 구분되지 않아
         디버그 출력처럼 보인다. 라벨 열을 고정해 값의 왼쪽 끝을 맞춘다. */
      .fact-row {{
        display: grid;
        grid-template-columns: 5.9rem 1fr;
        gap: 0.6rem;
        padding: 0.3rem 0;
        border-top: 1px solid #EFF1F1;
        font-size: 0.82rem;
        line-height: 1.45;
      }}
      .fact-row:first-of-type {{ border-top: none; }}
      .fact-key {{ color: {_MUTED}; }}
      .fact-value {{ overflow-wrap: anywhere; }}
      .fact-value.is-code {{ font-family: {_MONO_FONT}; font-size: 0.76rem; }}
      .fact-note {{
        color: {_MUTED};
        font-size: 0.76rem;
        margin-top: 0.7rem;
      }}

      /* 숫자는 자릿수가 흔들리면 비교가 어렵다. */
      [data-testid="stMetricValue"] {{
        font-family: {_BODY_FONT};
        font-size: 1.9rem;
        font-variant-numeric: tabular-nums;
        letter-spacing: -0.02em;
      }}
      [data-testid="stMetricLabel"] {{ color: {_MUTED}; }}
      [data-testid="stMetricDelta"] {{ font-variant-numeric: tabular-nums; }}

      [data-testid="stVerticalBlockBorderWrapper"] {{ background: #FFFFFF; }}
      [data-baseweb="tab-list"] {{ gap: 1.1rem; }}
      [data-baseweb="tab"] {{
        font-size: 0.86rem;
        font-weight: 600;
        padding-left: 0.1rem;
        padding-right: 0.1rem;
      }}
      /* 실험 목록. 25개를 세로로 훑는 화면이라 행 하나의 높이를 묶어 둔다 — 요약이
         길어져도 목록 전체가 늘어나지 않게 세 줄에서 자른다(#657). */
      [data-testid="stSidebar"] .stRadio label {{
        align-items: flex-start;
        border-top: 1px solid #E7E9E9;
        font-size: 0.8rem;
        line-height: 1.45;
        padding: 0.5rem 0.1rem;
      }}
      [data-testid="stSidebar"] .stRadio label:first-of-type {{ border-top: none; }}
      [data-testid="stSidebar"] .stRadio label p {{
        display: -webkit-box;
        -webkit-box-orient: vertical;
        -webkit-line-clamp: 3;
        overflow: hidden;
      }}
      /* 라디오 원은 첫 줄 높이에 맞춰 위로 붙인다. 기본값은 세로 가운데라 세 줄짜리
         항목에서 두 번째 줄 옆에 떠 있었다. */
      [data-testid="stSidebar"] .stRadio label > div:first-child {{ margin-top: 0.15rem; }}

      @media (max-width: 760px) {{
        /* 좁은 화면에서도 헤더 높이는 그대로다 — 여기서 줄이면 같은 자리가 잘린다. */
        .block-container {{ padding-top: 4.5rem; }}
        .stApp p.workbench-hypothesis {{ font-size: 1.1rem; }}
      }}
    </style>
    """


def status_badge(status: str) -> str:
    """안전한 내부 상태값으로 생성한 status badge HTML을 반환한다."""
    return (
        f'<span class="status-badge" style="--badge:{status_color(status)}">'
        f"{status_label(status)}</span>"
    )


def fact_row(key: str, value: str, *, code: bool = False) -> str:
    """인스펙터의 라벨/값 한 줄을 HTML로 반환한다.

    값은 실험 스냅샷에서 오고 그 스냅샷은 결국 에이전트가 쓴다. 이 문자열은
    `unsafe_allow_html`로 그려지므로 **키와 값 모두 escape한다** — 신뢰 경계가
    여기를 지난다.
    """
    value_class = "fact-value is-code" if code else "fact-value"
    return (
        f'<div class="fact-row"><span class="fact-key">{html.escape(key)}</span>'
        f'<span class="{value_class}">{html.escape(value)}</span></div>'
    )
