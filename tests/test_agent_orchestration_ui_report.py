"""워크벤치 리포트의 md → HTML 변환과 결과 탭 렌더 계약을 검증한다.

전체 파이프라인에서 API가 돌려준 리포트 본문이 화면에 그려지기까지의 UI 구간을
검증한다. 본문의 적재와 조회 endpoint는 `tests/test_experiment_report_api.py`가
담당한다.

**이 파일이 지키는 것은 escape 하나다.** iframe은 격리 경계가 아니므로
(Streamlit sandbox가 `allow-same-origin`과 `allow-scripts`를 둘 다 포함한다),
`html=False`가 뚫리면 방어가 남지 않는다.

뒤쪽 절반은 조회 배선(`app.refresh_report`)과 결과 탭 렌더(`views._render_results`)를
검증한다. `refresh_report`가 `report_error`만 건드리고 `detail_error`나 갱신 흐름을
건드리지 않는지, 지표·리포트 다섯 조합에서 `AppTest`가 예외 없이 렌더되는지가 핵심이다.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("markdown_it", reason="orchestration-ui 그룹이 설치돼야 한다")
pytest.importorskip("streamlit", reason="orchestration-ui 그룹이 설치돼야 한다")

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

from agent_orchestration.ui.app import refresh_report  # noqa: E402
from agent_orchestration.ui.client import ApiUnavailableError  # noqa: E402
from agent_orchestration.ui.report import (  # noqa: E402
    build_report_document,
    render_report_html,
    report_document,
)
from agent_orchestration.ui.models import Experiment, REPORT_STATUSES  # noqa: E402
from agent_orchestration.ui.state import (  # noqa: E402
    WorkbenchState,
    record_report,
    record_report_error,
    select_experiment,
)


APP_PATH = "agent_orchestration/ui/app.py"


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
    """`javascript:` 링크가 앵커로 만들어지지 않는다.

    `javascript:` 링크가 거부되면 markdown-it은 원문 그대로(`<p>[누르지
    마시오](javascript:alert(1))</p>`) escape해 출력한다. 이전 첫 단언
    (`rendered.replace(...)` 후 검사)은 입력 문자열이 그대로 남아 있기만 하면
    항등 함수여도 통과하는 assert-nothing이었다 — 실질 방어는 앵커가 아예 만들어지지
    않는다는 것이므로 그것을 직접 본다.
    """
    rendered = render_report_html("[누르지 마시오](javascript:alert(1))")

    assert "<a" not in rendered
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


def test_document_opens_links_in_a_new_tab() -> None:
    """M5: 링크 클릭이 620px iframe이나 워크벤치 전체를 이동시키지 않는다.

    Streamlit iframe sandbox의 `allow-top-navigation-by-user-activation` 때문에,
    `target` 지정 없이는 에이전트가 쓴 링크(외부 이슈 본문이 섞여 있다)를 클릭하면
    iframe 전체가 외부 페이지로 바뀌어 돌아갈 방법이 없다. `<base target="_blank">`가
    모든 링크를 새 tab으로 열어 이를 막는다.
    """
    document = build_report_document("<p>본문</p>")

    assert '<base target="_blank">' in document
    # head 안에, 어떤 스타일·본문보다 앞에 있어야 문서 전체 링크에 적용된다.
    assert document.index('<base target="_blank">') < document.index("<style>")


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


def test_record_report_does_not_mark_loaded_when_the_body_is_none() -> None:
    """`[정정 — #647, 2026-08-10]` 본문 `None`은 조회 완료로 캐시하지 않는다.

    결정 2가 지표 커밋과 리포트 커밋을 별도 트랜잭션으로 나눈 결과, `PASSED`로 막
    전이했지만 리포트 트랜잭션이 아직 커밋되지 않은 순간에도 200 + null이 온다. UI는
    그것이 "진짜 리포트 없음"인지 "아직 두 번째 트랜잭션 전"인지 구별할 수 없으므로,
    구별 불가능한 상태를 `report_loaded_for`로 영구 고착시키지 않는다 — 표식이 없으면
    다음 5초 polling에서 자연히 재조회된다.
    """
    state = WorkbenchState(selected_id="exp-1")
    record_report(state, "exp-1", None)

    assert state.report_markdown is None
    assert state.report_loaded_for is None


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


class _StubClient:
    """`fetch_report`만 답하는 최소 client."""

    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0

    def fetch_report(self, experiment_id: str) -> str | None:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _state_with(status: str) -> WorkbenchState:
    """선택 실험이 주어진 상태인 workbench state를 만든다."""
    state = WorkbenchState(selected_id="exp-1")
    state.experiment = Experiment(
        id="exp-1",
        hypothesis="가설",
        status=status,
        metric_summary=None,
        agent_session_id=None,
        created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    return state


def test_refresh_report_does_not_query_before_the_experiment_completes() -> None:
    """PASSED 이전에는 리포트가 반드시 없으므로 묻지 않는다."""
    client = _StubClient("# 결론")
    state = _state_with("EVALUATING")

    refresh_report(client, state)

    assert client.calls == 0
    assert state.report_loaded_for is None


def test_refresh_report_fetches_once_and_stops() -> None:
    """성공하면 한 번으로 그친다 — 5초 polling에 태우지 않는다."""
    client = _StubClient("# 결론")
    state = _state_with("PASSED")

    refresh_report(client, state)
    refresh_report(client, state)

    assert client.calls == 1
    assert state.report_markdown == "# 결론"


def test_refresh_report_retries_after_a_failure() -> None:
    """실패는 다음 갱신에서 다시 시도된다."""
    client = _StubClient(ApiUnavailableError("일시적 오류"))
    state = _state_with("PASSED")

    refresh_report(client, state)
    refresh_report(client, state)

    assert client.calls == 2
    assert state.report_error is not None


def test_refresh_report_failure_does_not_touch_the_detail_error() -> None:
    """리포트 실패가 워크벤치 전체를 오류 상태로 만들지 않는다."""
    client = _StubClient(ApiUnavailableError("일시적 오류"))
    state = _state_with("PASSED")

    refresh_report(client, state)

    assert state.detail_error is None


# 결과 탭 다섯 조합이 실제로 참고하는 `measurement.build_metric_snapshot`의 산출 형태다
# (`agent_orchestration/executor/measurement.py`). 값 자체는 임의지만 키 구조는 맞춘다.
SNAPSHOT_FIXTURE: dict[str, object] = {
    "contract_version": "experiment-metric-snapshot-v1",
    "primary_metric": "roc_auc",
    "seeds": [42, 43, 44],
    "conditions": {
        "baseline": {"roc_auc": 0.812, "log_loss": 0.421, "brier": 0.152},
        "candidate": {"roc_auc": 0.831, "log_loss": 0.402, "brier": 0.147},
    },
    "paired": {
        "roc_auc": {"mean": 0.019, "standard_error": 0.004},
        "log_loss": {"mean": -0.019, "standard_error": 0.003},
        "brier": {"mean": -0.005, "standard_error": 0.002},
    },
    "split_matches": True,
    "dataset_fingerprint": "sha256:abcdef0123456789",
    "results_uri": "gs://autoresearch-results/exp-report-combo/results.json",
}


class _ReportStubHandler(BaseHTTPRequestHandler):
    """결과 탭 조합 테스트 전용 Experiment API 최소 스텁.

    `tests/test_ui_submission_app.py`의 `_StubHandler` 라우팅을 그대로 따르고
    `/experiments/{id}/report`만 더한다.
    """

    experiment: dict = {}
    report_body: str | None = None
    report_status: int = 200

    def log_message(self, *_args: object) -> None:
        """테스트 출력에 HTTP 로그를 남기지 않는다."""

    def do_GET(self) -> None:  # noqa: N802
        route = self.path.split("?", 1)[0]
        if route == "/experiments":
            self._respond(
                {
                    "items": [type(self).experiment],
                    "limit": 50,
                    "offset": 0,
                    "total": 1,
                }
            )
            return
        parts = route.split("/")
        if len(parts) == 3:
            self._respond(type(self).experiment)
            return
        if parts[3] == "metadata":
            self._respond({"entries": {}})
            return
        if parts[3] == "report":
            if type(self).report_status != 200:
                self._respond({"detail": "unavailable"}, status=type(self).report_status)
                return
            self._respond(
                {
                    "experiment_id": type(self).experiment.get("id"),
                    "report_markdown": type(self).report_body,
                }
            )
            return
        self._respond({"items": [], "next_cursor": None})

    def _respond(self, payload: dict, *, status: int = 200) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _rendered_workbench(
    *,
    metric_summary: dict | None,
    report_body: str | None,
    report_status: int,
) -> AppTest:
    """지표·리포트 조합 하나로 결과 탭까지 그린 workbench `AppTest`를 반환한다.

    실험 상태를 `PASSED`로 고정한다 — `refresh_report`는 `REPORT_STATUSES`가 아니면
    조회 자체를 시도하지 않으므로, 그 이전 상태로 두면 리포트 실패 조합을 검증할 수
    없다.
    """
    st.cache_resource.clear()
    experiment_id = "exp-report-combo"
    _ReportStubHandler.experiment = {
        "id": experiment_id,
        "hypothesis": "결과 탭 조합 검증",
        "status": "PASSED",
        "metric_summary": metric_summary,
        "agent_session_id": None,
        "created_at": "2026-08-05T00:00:00+00:00",
        "updated_at": "2026-08-05T00:00:00+00:00",
    }
    _ReportStubHandler.report_body = report_body
    _ReportStubHandler.report_status = report_status
    server = HTTPServer(("127.0.0.1", 0), _ReportStubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[0], server.server_address[1]
    previous_base_url = os.environ.get("ORCH_UI_API_BASE_URL")
    previous_token = os.environ.get("ORCH_UI_API_TOKEN")
    os.environ["ORCH_UI_API_BASE_URL"] = f"http://{host}:{port}"
    os.environ["ORCH_UI_API_TOKEN"] = "t" * 32
    try:
        app = AppTest.from_file(APP_PATH, default_timeout=60)
        app.run()
        app.sidebar.radio[0].set_value(experiment_id).run()
        return app
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        _restore_env("ORCH_UI_API_BASE_URL", previous_base_url)
        _restore_env("ORCH_UI_API_TOKEN", previous_token)


def _restore_env(name: str, previous: str | None) -> None:
    """테스트가 건드린 환경 변수를 이전 값으로 되돌린다."""
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


@pytest.mark.parametrize(
    ("metric_summary", "report_body", "report_status"),
    [
        pytest.param(SNAPSHOT_FIXTURE, None, 200, id="지표만"),
        pytest.param(None, "# 결론", 200, id="리포트만"),
        pytest.param(SNAPSHOT_FIXTURE, "# 결론", 200, id="둘_다"),
        pytest.param(None, None, 200, id="둘_다_없음"),
        pytest.param(SNAPSHOT_FIXTURE, None, 503, id="fetch_실패"),
    ],
)
def test_results_tab_survives_every_combination(
    metric_summary: dict | None, report_body: str | None, report_status: int
) -> None:
    """다섯 조합 어디서도 결과 탭이 죽지 않는다."""
    app = _rendered_workbench(
        metric_summary=metric_summary,
        report_body=report_body,
        report_status=report_status,
    )

    assert not app.exception
