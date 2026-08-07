"""Streamlit 제출 폼 스크립트를 실제로 실행해 렌더링 계약을 고정한다.

전체 파이프라인 중 사용자가 브라우저에서 보는 폼이 실제로 무엇을 그리고 무엇을 서버로
보내는지 검증한다. 서버의 발행 절차는 test_experiment_issue_publication이 담당한다.

`tests/test_ui_submission_form.py`는 순수 함수만 보므로 위젯 렌더링 버그를 잡지 못한다.
#536에서 실제로 그런 버그가 났다 — `st.form` 안에서 체크박스로 다른 위젯의 `disabled`를
제어했는데, form 안 위젯은 상호작용해도 rerun을 일으키지 않아 사용자가 guardrail 값을
입력할 수 없었고 그대로 guardrail 없이 발행됐다. 그 종류를 여기서 막는다.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("streamlit", reason="orchestration-ui 그룹이 설치돼야 한다")

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

APP_PATH = "agent_orchestration/ui/app.py"


class _StubHandler(BaseHTTPRequestHandler):
    """Experiment API의 최소 스텁. 받은 요청 본문을 클래스 변수에 모은다."""

    captured: list[tuple[str, dict]] = []

    def log_message(self, *_args: object) -> None:
        """테스트 출력에 HTTP 로그를 남기지 않는다."""

    def do_GET(self) -> None:  # noqa: N802
        self._respond({"items": [], "limit": 50, "offset": 0, "total": 0})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode()) if length else {}
        type(self).captured.append((self.path, body))
        if self.path == "/experiments":
            self._respond(
                {
                    "id": "3f2a1c9d-8b7e-4a1f-9c2d-5e6f7a8b9c0d",
                    "hypothesis": body.get("hypothesis", ""),
                    "status": "CREATED",
                    "created_at": "2026-08-05T00:00:00+00:00",
                    "updated_at": "2026-08-05T00:00:00+00:00",
                    "issue_number": None,
                    "issue_branch": None,
                }
            )
            return
        self._respond(
            {
                "issue_number": 537,
                "issue_url": "https://github.com/SKYAHO/Autoresearch/issues/537",
                "issue_branch": "exp/537-views-per-day-ratio-feature",
            }
        )

    def _respond(self, payload: dict) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@pytest.fixture
def stub_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, dict]]]:
    """UI가 부를 Experiment API를 loopback 스텁으로 대체한다.

    `app.get_client()`는 `@st.cache_resource`라 client가 process 전역에 남는다. 비우지
    않으면 두 번째 테스트부터 앞 테스트의 (이미 닫힌) 스텁 주소를 계속 쓴다.
    """
    st.cache_resource.clear()
    _StubHandler.captured = []
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    monkeypatch.setenv("ORCH_UI_API_BASE_URL", f"http://{host}:{port}")
    monkeypatch.setenv("ORCH_UI_API_TOKEN", "t" * 32)
    try:
        yield _StubHandler.captured
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _rendered_app() -> AppTest:
    app = AppTest.from_file(APP_PATH, default_timeout=60)
    app.run()
    return app


_MARKDOWN_HYPOTHESIS = "# 주제\n\n비율 피처가 baseline 대비 test ROC-AUC를 개선한다."


def _fill_required(app: AppTest) -> None:
    app.text_input[0].set_value("views per day ratio feature")
    app.text_area[0].set_value(_MARKDOWN_HYPOTHESIS)


def test_form_renders_without_exception(stub_api: list[tuple[str, dict]]) -> None:
    """스크립트가 예외 없이 폼을 그려야 한다."""
    app = _rendered_app()

    assert not app.exception
    labels = [widget.label for widget in app.text_input] + [
        widget.label for widget in app.text_area
    ]
    assert "실험 제목" in labels
    assert "가설" in labels


def test_form_asks_for_nothing_but_a_title_and_a_hypothesis(
    stub_api: list[tuple[str, dict]],
) -> None:
    """입력칸이 늘어나면 마크다운 자유 서술로 합친 의미가 사라진다(#570)."""
    app = _rendered_app()

    assert len(app.text_input) == 1
    assert len(app.text_area) == 1


def test_hypothesis_editor_is_outside_a_form(stub_api: list[tuple[str, dict]]) -> None:
    """`st.form` 안에 두면 미리보기가 제출 전까지 첫 렌더 상태에 멈춘다.

    form 안 위젯은 상호작용해도 rerun을 일으키지 않으므로, 편집창이 폼 안에 있으면
    사용자가 무엇을 쓰든 미리보기가 따라오지 않는다. 제출 버튼이 일반 `st.button`이면
    폼이 없다는 뜻이다 — 폼이면 `form_submit_button`으로만 제출할 수 있다.
    """
    app = _rendered_app()

    assert len(app.button) >= 1
    assert app.button[0].label == "사전등록하고 이슈 발행"


def test_preview_follows_the_hypothesis(stub_api: list[tuple[str, dict]]) -> None:
    """편집한 마크다운이 미리보기에 반영돼야 한다."""
    app = _rendered_app()
    app.text_area[0].set_value("# 미리보기 제목").run()

    assert any("# 미리보기 제목" == element.value for element in app.markdown)


def test_no_form_widget_is_disabled(stub_api: list[tuple[str, dict]]) -> None:
    """`st.form` 안의 위젯을 `disabled`로 게이팅하면 사용자가 입력할 수 없다.

    form 안 위젯은 상호작용해도 rerun을 일으키지 않으므로, 같은 폼 안의 다른 위젯 값으로
    `disabled`를 계산하면 그 값은 **직전 실행의 값**이다. 사용자는 칸을 열지 못한 채
    제출하게 되고, 빠진 값은 조용히 기본값으로 발행된다.
    """
    app = _rendered_app()

    disabled = [
        widget.label
        for widget in (*app.text_input, *app.text_area, *app.selectbox)
        if widget.disabled
    ]

    assert disabled == []


def test_submitting_sends_the_server_contract(stub_api: list[tuple[str, dict]]) -> None:
    """폼 제출이 생성과 발행을 차례로 부르고, 발행 요청이 서버 계약과 맞아야 한다."""
    app = _rendered_app()
    _fill_required(app)
    app.button[0].click().run()

    assert not app.exception
    paths = [path for path, _ in stub_api]
    assert paths == [
        "/experiments",
        "/experiments/3f2a1c9d-8b7e-4a1f-9c2d-5e6f7a8b9c0d/issue",
    ]
    fields = stub_api[1][1]["fields"]
    assert fields["title"] == "views per day ratio feature"
    assert fields["hypothesis"] == _MARKDOWN_HYPOTHESIS
    # `allowed_scope`는 #570에서 사라졌다. 서버가 `extra="forbid"`라 보내면 422다.
    assert set(stub_api[1][1]) == {"fields"}


def test_submission_carries_no_metric_fields(stub_api: list[tuple[str, dict]]) -> None:
    """지표를 화면에서 없앴으므로 UI는 그 값을 만들지 않는다(#570).

    서버가 기본값을 소유한다. UI가 지어낸 값을 보내면 두 곳이 같은 값을 각자
    선언하게 되어 어긋날 수 있다.
    """
    app = _rendered_app()
    _fill_required(app)
    app.button[0].click().run()

    fields = stub_api[1][1]["fields"]

    assert set(fields) == {"title", "hypothesis"}


def test_blank_required_field_is_blocked_before_any_request(
    stub_api: list[tuple[str, dict]],
) -> None:
    """필수 칸이 비면 서버 왕복 없이 화면에서 알려준다."""
    app = _rendered_app()
    _fill_required(app)
    app.text_input[0].set_value("")
    app.button[0].click().run()

    assert stub_api == []
    assert any("실험 제목" in element.value for element in app.error)


def test_publication_result_is_shown(stub_api: list[tuple[str, dict]]) -> None:
    """발행 결과의 이슈 번호와 브랜치가 화면에 나와야 한다."""
    app = _rendered_app()
    _fill_required(app)
    app.button[0].click().run()

    assert any("537" in element.value for element in app.success)
