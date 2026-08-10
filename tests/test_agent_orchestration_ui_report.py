"""워크벤치 리포트의 md → HTML 변환과 결과 탭 렌더 계약을 검증한다.

전체 파이프라인에서 API가 돌려준 리포트 본문이 화면에 그려지기까지의 UI 구간을
검증한다. 본문의 적재와 조회 endpoint는 `tests/test_experiment_report_api.py`가
담당한다.

**이 파일이 지키는 것은 escape 하나다.** iframe은 격리 경계가 아니므로
(Streamlit sandbox가 `allow-same-origin`과 `allow-scripts`를 둘 다 포함한다),
`html=False`가 뚫리면 방어가 남지 않는다.
"""

from __future__ import annotations

import pytest

pytest.importorskip("markdown_it", reason="orchestration-ui 그룹이 설치돼야 한다")

from agent_orchestration.ui.report import (  # noqa: E402
    build_report_document,
    render_report_html,
    report_document,
)
from agent_orchestration.ui.models import REPORT_STATUSES  # noqa: E402
from agent_orchestration.ui.state import (  # noqa: E402
    WorkbenchState,
    record_report,
    record_report_error,
    select_experiment,
)


def test_inline_raw_html_is_escaped() -> None:
    """에이전트가 쓴 인라인 HTML이 태그가 아니라 텍스트가 된다."""
    rendered = render_report_html("raw <script>alert(1)</script> 끝")

    assert "&lt;script&gt;" in rendered
    assert "<script>" not in rendered


def test_block_raw_html_is_escaped() -> None:
    """블록 HTML도 escape된다 — 인라인만 막으면 뚫린다."""
    rendered = render_report_html('<div onclick="x">블록</div>')

    assert "&lt;div" in rendered
    assert "<div" not in rendered
    assert "onclick" not in rendered or "&quot;" in rendered


def test_javascript_links_are_neutralized() -> None:
    """`javascript:` 링크가 앵커로 만들어지지 않는다."""
    rendered = render_report_html("[누르지 마시오](javascript:alert(1))")

    assert "javascript:" not in rendered.replace("javascript:alert(1)", "")
    assert "<a href=\"javascript:" not in rendered


def test_data_text_html_links_are_neutralized() -> None:
    """`data:text/html` 링크도 앵커가 되지 않는다."""
    rendered = render_report_html("[문서](data:text/html,<b>x</b>)")

    assert '<a href="data:text/html' not in rendered


def test_ordinary_links_survive() -> None:
    """정상 링크는 그대로 앵커가 된다."""
    assert '<a href="https://example.com"' in render_report_html("[예](https://example.com)")


def test_empty_report_renders_without_error() -> None:
    """빈 본문도 빈 문자열로 변환된다 — 호출부가 분기하지 않아도 된다."""
    assert render_report_html("") == ""


def test_document_carries_no_script() -> None:
    """우리 템플릿이 스크립트 실행 표면을 만들지 않는다.

    격리가 없는 곳에 우리 손으로 스크립트를 넣을 이유가 없다(spec 결정 5).
    """
    document = build_report_document("<p>본문</p>")

    assert "<script" not in document.lower()
    assert "onload" not in document.lower()


def test_document_is_a_complete_html_page() -> None:
    """srcdoc에 넣을 완결된 문서다."""
    document = build_report_document("<p>본문</p>")

    assert document.startswith("<!doctype html>")
    assert "<p>본문</p>" in document
    assert 'lang="ko"' in document


def test_report_document_composes_both_steps() -> None:
    """호출부가 두 단계를 따로 부르지 않아도 된다."""
    document = report_document("# 제목")

    assert document.startswith("<!doctype html>")
    assert "<h1>제목</h1>" in document


def test_report_statuses_are_exactly_the_states_that_can_hold_a_report() -> None:
    """리포트를 가진 실험은 PASSED 아니면 PROMOTED다.

    `record_experiment_result`가 유일한 기록자이고 PASSED를 하드코딩하며,
    `ALLOWED_TRANSITIONS[PASSED] = {PROMOTED}`라 PASSED에서 FAILED로 가는 간선이 없다.
    전이가 늘면 이 집합도 함께 넓혀야 한다.
    """
    assert REPORT_STATUSES == frozenset({"PASSED", "PROMOTED"})


def test_record_report_marks_the_experiment_as_loaded() -> None:
    """성공하면 본문과 함께 조회 완료 표식을 세운다."""
    state = WorkbenchState(selected_id="exp-1")
    record_report(state, "exp-1", "# 결론")

    assert state.report_markdown == "# 결론"
    assert state.report_loaded_for == "exp-1"
    assert state.report_error is None


def test_record_report_marks_loaded_even_when_there_is_no_report() -> None:
    """리포트가 없다는 사실도 조회 결과다 — 다시 묻지 않는다."""
    state = WorkbenchState(selected_id="exp-1")
    record_report(state, "exp-1", None)

    assert state.report_markdown is None
    assert state.report_loaded_for == "exp-1"


def test_record_report_error_does_not_mark_loaded() -> None:
    """실패에는 표식을 세우지 않는다 — 일시적 오류가 리포트를 영구히 가리면 안 된다."""
    state = WorkbenchState(selected_id="exp-1")
    record_report_error(state, "일시적 오류")

    assert state.report_error == "일시적 오류"
    assert state.report_loaded_for is None


def test_selecting_another_experiment_clears_the_report() -> None:
    """실험을 바꾸면 이전 리포트가 남지 않는다."""
    state = WorkbenchState(selected_id="exp-1")
    record_report(state, "exp-1", "# 결론")
    record_report_error(state, "오류")

    select_experiment(state, "exp-2")

    assert state.report_markdown is None
    assert state.report_error is None
    assert state.report_loaded_for is None
