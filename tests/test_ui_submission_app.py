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


def _fill_required(app: AppTest) -> None:
    app.text_input[0].set_value("views per day ratio feature")
    app.text_area[0].set_value("비율 피처가 baseline 대비 test ROC-AUC를 개선한다.")
    app.text_area[2].set_value("- 추가 피처: views_per_day = views / (days + 1)")
    app.text_input[1].set_value("roc_auc")
    app.text_input[2].set_value("0.002")


def test_form_renders_without_exception(stub_api: list[tuple[str, dict]]) -> None:
    """스크립트가 예외 없이 폼을 그려야 한다."""
    app = _rendered_app()

    assert not app.exception
    labels = [widget.label for widget in app.text_input] + [
        widget.label for widget in app.text_area
    ]
    assert "실험 제목" in labels
    assert "연구 가설" in labels


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
    assert fields["primary_metric_name"] == "roc_auc"
    assert fields["primary_metric_direction"] == "higher_is_better"
    assert "allowed_scope" in stub_api[1][1]


def test_declared_guardrail_reaches_the_server(stub_api: list[tuple[str, dict]]) -> None:
    """guardrail 이름을 채우면 세 값이 함께 전송돼야 한다.

    #536에서 이 경로가 조용히 깨졌다. 성공 기준을 사용자가 선언하게 만드는 것이 이
    변경의 목적인데, guardrail이 말없이 `없음`으로 떨어지면 그 목적이 무너진다.
    """
    app = _rendered_app()
    _fill_required(app)
    app.text_input[3].set_value("logloss")
    app.selectbox[1].set_value("낮을수록 좋음")
    app.text_input[4].set_value("0.001")
    app.button[0].click().run()

    fields = stub_api[1][1]["fields"]

    assert fields["guardrail_metric_name"] == "logloss"
    assert fields["guardrail_metric_direction"] == "lower_is_better"
    assert fields["maximum_guardrail_regression"] == "0.001"


def test_unset_guardrail_sends_the_sentinels(stub_api: list[tuple[str, dict]]) -> None:
    """guardrail 칸을 비우면 서버가 요구하는 미선언 sentinel로 나가야 한다."""
    app = _rendered_app()
    _fill_required(app)
    app.button[0].click().run()

    fields = stub_api[1][1]["fields"]

    assert fields["guardrail_metric_name"] == "없음"
    assert fields["guardrail_metric_direction"] == "not_applicable"
    assert fields["maximum_guardrail_regression"] == "없음"


def test_partial_guardrail_is_blocked_before_any_request(
    stub_api: list[tuple[str, dict]],
) -> None:
    """이름만 채우고 악화폭을 비우면 요청을 보내지 않고 화면에서 막는다."""
    app = _rendered_app()
    _fill_required(app)
    app.text_input[3].set_value("logloss")
    app.button[0].click().run()

    assert stub_api == []
    assert any("최대 악화폭" in element.value for element in app.error)


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
