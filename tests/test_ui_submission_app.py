"""Streamlit 제출 폼 스크립트를 실제로 실행해 렌더링 계약을 고정한다.

전체 파이프라인 중 사용자가 브라우저에서 보는 폼과 화면 전환이 실제로 무엇을 그리고
무엇을 서버로 보내는지 검증한다. 서버 내부 발행 절차는
test_experiment_issue_publication이 담당한다.

`tests/test_ui_submission_form.py`는 순수 함수만 보므로 위젯 렌더링 버그를 잡지 못한다.
#536에서 실제로 그런 버그가 났다 — `st.form` 안에서 체크박스로 다른 위젯의 `disabled`를
제어했는데, form 안 위젯은 상호작용해도 rerun을 일으키지 않아 사용자가 guardrail 값을
입력할 수 없었고 그대로 guardrail 없이 발행됐다. 그 종류를 여기서 막는다.
"""

from __future__ import annotations

import html
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("streamlit", reason="orchestration-ui 그룹이 설치돼야 한다")

import streamlit as st  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

APP_PATH = "applications/experiment_platform/workbench/app.py"

# 기존 테스트가 이슈 발행 경로 좌표로 쓰는 값. 첫 생성은 항상 이 id를 돌려준다.
CANONICAL_EXPERIMENT_ID = "3f2a1c9d-8b7e-4a1f-9c2d-5e6f7a8b9c0d"


def _sidebar_button(app: AppTest, label: str):
    """sidebar 버튼을 라벨로 고른다.

    인덱스로 고르면 sidebar에 버튼이 하나 추가될 때마다 무관한 테스트가 깨진다 —
    실행 현황 보드 진입 버튼이 들어오면서 실제로 그랬다(#671).
    """
    for button in app.sidebar.button:
        if button.label == label:
            return button
    raise AssertionError(f"sidebar에 '{label}' 버튼이 없습니다.")


def _shows_hypothesis(app: AppTest, hypothesis: str) -> bool:
    """상세 화면이 선택한 실험의 가설 본문을 표시하는지 확인한다.

    `st.title`이 아니라 `workbench-hypothesis` 마크다운으로 그린다 — 가설은 여러
    문장짜리 본문이라 H1으로 그리면 관찰 보드를 화면 밖으로 밀어냈다(#657).
    본문은 `html.escape`를 거쳐 들어가므로 같은 변환 뒤에 비교한다. escape를 함께
    확인하는 것은 덤이 아니라 의도다 — 이 경로는 `unsafe_allow_html`을 켜므로
    escape가 빠지면 사용자 입력이 그대로 HTML이 된다.
    """
    return any(
        "workbench-hypothesis" in element.value
        and html.escape(hypothesis) in element.value
        for element in app.markdown
    )


class _StubHandler(BaseHTTPRequestHandler):
    """Experiment API의 최소 스텁. 받은 요청 본문을 클래스 변수에 모은다."""

    captured: list[tuple[str, dict]] = []
    get_paths: list[str] = []
    experiments: list[dict] = []
    missing_experiment_ids: set[str] = set()
    publication_failures = 0
    created_count = 0
    # 몇 번째 생성 요청을 실패시킬지(1-based). None이면 전부 성공.
    fail_creation_at: int | None = None

    def log_message(self, *_args: object) -> None:
        """테스트 출력에 HTTP 로그를 남기지 않는다."""

    def do_GET(self) -> None:  # noqa: N802
        type(self).get_paths.append(self.path)
        route = self.path.split("?", 1)[0]
        if route == "/experiments":
            self._respond(
                {
                    "items": type(self).experiments,
                    "limit": 50,
                    "offset": 0,
                    "total": len(type(self).experiments),
                }
            )
            return

        parts = route.split("/")
        experiment_id = parts[2]
        if experiment_id in type(self).missing_experiment_ids:
            type(self).experiments = [
                item
                for item in type(self).experiments
                if item["id"] != experiment_id
            ]
            self._respond({"detail": "not found"}, status=404)
            return
        if len(parts) == 3:
            experiment = next(
                (
                    item
                    for item in type(self).experiments
                    if item["id"] == experiment_id
                ),
                None,
            )
            if experiment is None:
                self._respond({"detail": "not found"}, status=404)
            else:
                self._respond(experiment)
            return
        if parts[3] == "metadata":
            self._respond({"entries": {}})
            return
        self._respond({"items": [], "next_cursor": None})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode()) if length else {}
        type(self).captured.append((self.path, body))
        if self.path == "/experiments":
            if type(self).fail_creation_at == type(self).created_count + 1:
                self._respond({"detail": "creation exploded"}, status=500)
                return
            # 묶음 제출은 생성을 여러 번 부른다. 매번 같은 id를 돌려주면 화면이
            # 같은 실험을 여러 번 그리게 되어 실제와 다른 상태를 만든다.
            # 첫 건은 기존 테스트가 좌표로 쓰는 값을 유지한다.
            index = type(self).created_count
            type(self).created_count += 1
            experiment = _experiment_payload(
                CANONICAL_EXPERIMENT_ID if index == 0 else f"exp-batch-{index}",
                body.get("hypothesis", ""),
            )
            type(self).experiments.insert(0, experiment)
            self._respond(experiment, status=201)
            return
        if type(self).publication_failures:
            type(self).publication_failures -= 1
            self._respond({"detail": "temporary failure"}, status=503)
            return
        self._respond(
            {
                "issue_number": 537,
                "issue_url": "https://github.com/SKYAHO/Autoresearch/issues/537",
                "issue_branch": "exp/537-views-per-day-ratio-feature",
            }
        )

    def _respond(self, payload: dict, *, status: int = 200) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _experiment_payload(experiment_id: str, hypothesis: str) -> dict:
    return {
        "id": experiment_id,
        "hypothesis": hypothesis,
        "status": "CREATED",
        "created_at": "2026-08-05T00:00:00+00:00",
        "updated_at": "2026-08-05T00:00:00+00:00",
        "issue_number": None,
        "issue_branch": None,
    }


@pytest.fixture
def stub_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, dict]]]:
    """UI가 부를 Experiment API를 loopback 스텁으로 대체한다.

    `app.get_client()`는 `@st.cache_resource`라 client가 process 전역에 남는다. 비우지
    않으면 두 번째 테스트부터 앞 테스트의 (이미 닫힌) 스텁 주소를 계속 쓴다.
    """
    st.cache_resource.clear()
    _StubHandler.captured = []
    _StubHandler.get_paths = []
    _StubHandler.experiments = []
    _StubHandler.missing_experiment_ids = set()
    _StubHandler.publication_failures = 0
    _StubHandler.created_count = 0
    _StubHandler.fail_creation_at = None
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


def test_empty_list_stays_on_create_without_activity_polling(
    stub_api: list[tuple[str, dict]],
) -> None:
    """빈 목록의 CREATE rerun은 상세·activity endpoint를 호출하지 않는다."""
    app = _rendered_app()

    app.run()
    _sidebar_button(app, "실험 목록 새로고침").click().run()

    assert not app.exception
    assert app.text_input[0].label == "실험 제목"
    assert _StubHandler.get_paths
    assert all(path.startswith("/experiments?") for path in _StubHandler.get_paths)


def test_same_experiment_can_be_reselected_after_returning_to_create(
    stub_api: list[tuple[str, dict]],
) -> None:
    """radio callback은 연속 rerun 뒤 동일 항목도 다시 DETAIL로 연다."""
    experiment_id = "exp-one"
    _StubHandler.experiments = [_experiment_payload(experiment_id, "첫 번째 가설")]
    app = _rendered_app()

    app.sidebar.radio[0].set_value(experiment_id).run()
    assert _shows_hypothesis(app, "첫 번째 가설")

    _sidebar_button(app, "+ 가설 추가하기").click().run()
    assert app.text_input[0].label == "실험 제목"

    app.sidebar.radio[0].set_value(experiment_id).run()

    assert not app.exception
    assert _shows_hypothesis(app, "첫 번째 가설")


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


def test_pending_publication_retries_saved_submission_without_duplicate(
    stub_api: list[tuple[str, dict]],
) -> None:
    """부분 실패는 원 폼을 재제출하지 않고 저장된 submission으로 재시도한다."""
    _StubHandler.publication_failures = 1
    app = _rendered_app()
    _fill_required(app)

    app.button[0].click().run()

    assert app.text_input[0].label == "실험 제목"
    assert any("이슈 발행에 실패" in element.value for element in app.error)
    retry_buttons = [
        button for button in app.button if button.label == "이슈 발행 다시 시도"
    ]
    assert len(retry_buttons) == 1

    retry_buttons[0].click().run()

    paths = [path for path, _ in stub_api]
    assert paths.count("/experiments") == 1
    assert paths.count(
        "/experiments/3f2a1c9d-8b7e-4a1f-9c2d-5e6f7a8b9c0d/issue"
    ) == 2
    assert any("537" in element.value for element in app.success)
    assert _shows_hypothesis(app, _MARKDOWN_HYPOTHESIS)


def test_pending_publication_retry_failure_keeps_create_and_pending(
    stub_api: list[tuple[str, dict]],
) -> None:
    """재시도도 실패하면 CREATE와 pending 동작 버튼을 그대로 유지한다."""
    _StubHandler.publication_failures = 2
    app = _rendered_app()
    _fill_required(app)
    app.button[0].click().run()

    next(
        button for button in app.button if button.label == "이슈 발행 다시 시도"
    ).click().run()

    paths = [path for path, _ in stub_api]
    assert paths.count("/experiments") == 1
    assert paths.count(
        "/experiments/3f2a1c9d-8b7e-4a1f-9c2d-5e6f7a8b9c0d/issue"
    ) == 2
    assert app.text_input[0].label == "실험 제목"
    assert any("이슈 발행에 실패" in element.value for element in app.error)
    labels = [button.label for button in app.button]
    assert "이슈 발행 다시 시도" in labels
    assert "실패한 등록 취소하고 새 가설 작성" in labels


def test_discard_pending_publication_unlocks_new_submission(
    stub_api: list[tuple[str, dict]],
) -> None:
    """취소는 생성된 Experiment를 남기고 다른 가설의 새 등록을 허용한다."""
    _StubHandler.publication_failures = 1
    app = _rendered_app()
    _fill_required(app)
    app.button[0].click().run()

    next(
        button
        for button in app.button
        if button.label == "실패한 등록 취소하고 새 가설 작성"
    ).click().run()

    assert _StubHandler.experiments
    assert _StubHandler.experiments[0]["hypothesis"] == _MARKDOWN_HYPOTHESIS
    assert "이슈 발행 다시 시도" not in [button.label for button in app.button]
    _fill_required(app)
    app.text_area[0].set_value("새 가설은 pending 등록과 다른 입력이다.")
    next(
        button for button in app.button if button.label == "사전등록하고 이슈 발행"
    ).click().run()

    paths = [path for path, _ in stub_api]
    assert paths.count("/experiments") == 2
    assert any("537" in element.value for element in app.success)


def test_pending_actions_are_hidden_without_pending_publication(
    stub_api: list[tuple[str, dict]],
) -> None:
    """pending이 없는 CREATE에는 재시도·취소 버튼을 표시하지 않는다."""
    app = _rendered_app()

    labels = [button.label for button in app.button]
    assert "이슈 발행 다시 시도" not in labels
    assert "실패한 등록 취소하고 새 가설 작성" not in labels


def test_refresh_exposes_new_experiment_and_hides_other_publication_result(
    stub_api: list[tuple[str, dict]],
) -> None:
    """목록 갱신 후 B를 선택하면 A의 발행 결과가 B 상세에 남지 않는다."""
    app = _rendered_app()
    _fill_required(app)
    app.button[0].click().run()
    assert any("537" in element.value for element in app.success)

    second_id = "exp-two"
    _StubHandler.experiments.append(_experiment_payload(second_id, "두 번째 가설"))
    _sidebar_button(app, "실험 목록 새로고침").click().run()
    app.sidebar.radio[0].set_value(second_id).run()

    assert not app.exception
    assert _shows_hypothesis(app, "두 번째 가설")
    assert not app.success


def test_deleted_selected_experiment_is_removed_on_detail_refresh(
    stub_api: list[tuple[str, dict]],
) -> None:
    """선택 직후 404가 나면 목록·선택을 비우고 빈 DETAIL 안내를 표시한다."""
    experiment_id = "exp-deleted"
    _StubHandler.experiments = [_experiment_payload(experiment_id, "삭제된 가설")]
    app = _rendered_app()
    _StubHandler.missing_experiment_ids.add(experiment_id)

    app.sidebar.radio[0].set_value(experiment_id).run()

    assert not app.exception
    assert any("항목을 선택" in element.value for element in app.info)
    assert not app.sidebar.radio


def test_board_view_renders_with_three_tabs(stub_api: list[tuple[str, dict]]) -> None:
    """보드 진입 버튼이 실행 현황 화면을 연다.

    보드는 `st.fragment` 안에서 그려지고 카드마다 버튼을 단다 — 스크립트를 실제로
    돌려보지 않으면 fragment·버튼 key 문제를 잡을 수 없다.
    """
    _StubHandler.experiments = [
        _experiment_payload("exp-running", "실행 중 가설"),
        _experiment_payload("exp-waiting", "대기 가설"),
    ]
    _StubHandler.experiments[0]["status"] = "RUNNING"
    app = _rendered_app()

    _sidebar_button(app, "실행 현황 보기").click().run()

    assert not app.exception
    labels = [element.label for element in app.tabs]
    assert any(label.startswith("실행 중") for label in labels)
    assert any(label.startswith("대기") for label in labels)
    assert any(label.startswith("완료") for label in labels)


def test_board_card_opens_the_detail_view(stub_api: list[tuple[str, dict]]) -> None:
    """카드의 상세 보기가 기존 상세 화면으로 넘어간다.

    fragment 안에서 기본 `st.rerun()`을 쓰면 fragment만 다시 돌아 상태는 DETAIL인데
    화면은 보드에 머문다. `scope="app"`이 필요한 이유다(#671 spec 결정 3).
    """
    _StubHandler.experiments = [_experiment_payload("exp-running", "실행 중 가설")]
    _StubHandler.experiments[0]["status"] = "RUNNING"
    app = _rendered_app()
    _sidebar_button(app, "실행 현황 보기").click().run()

    open_buttons = [b for b in app.button if b.label == "상세 보기"]
    assert open_buttons, "보드 카드에 상세 보기 버튼이 없습니다."
    open_buttons[0].click().run()

    assert not app.exception
    assert _shows_hypothesis(app, "실행 중 가설")


def _fill_draft(app: AppTest, order: int, title: str, hypothesis: str) -> None:
    """`order`번째 가설 탭의 제목·본문을 채운다."""
    suffix = "" if order == 1 else f"-{order}"
    app.text_input(key=f"submission-title{suffix}").set_value(title)
    app.text_area(key=f"submission-hypothesis{suffix}").set_value(hypothesis)


def test_batch_submission_creates_one_experiment_per_hypothesis(
    stub_api: list[tuple[str, dict]],
) -> None:
    """가설 3개를 한 번에 내면 실험 3개와 이슈 3개가 열린다.

    동시 실행 상한만큼 한 번에 내야 "여러 가설이 동시에 검증된다"가 성립한다(#671).
    """
    app = _rendered_app()
    app.slider(key="submission-count").set_value(3).run()
    for order in range(1, 4):
        _fill_draft(app, order, f"제목 {order}", f"가설 {order}입니다.")

    app.button[0].click().run()

    assert not app.exception
    created = [path for path, _ in stub_api if path == "/experiments"]
    published = [path for path, _ in stub_api if path.endswith("/issue")]
    assert len(created) == 3
    assert len(published) == 3
    # 본문이 서로 달라야 한다 — 탭마다 위젯 key가 갈리지 않으면 같은 값 3개가 간다.
    hypotheses = [body["hypothesis"] for path, body in stub_api if path == "/experiments"]
    assert sorted(hypotheses) == ["가설 1입니다.", "가설 2입니다.", "가설 3입니다."]


def test_batch_submission_lands_on_the_board(
    stub_api: list[tuple[str, dict]],
) -> None:
    """여러 건을 냈으면 볼 곳은 상세가 아니라 보드다."""
    app = _rendered_app()
    app.slider(key="submission-count").set_value(2).run()
    _fill_draft(app, 1, "제목 1", "가설 1입니다.")
    _fill_draft(app, 2, "제목 2", "가설 2입니다.")

    app.button[0].click().run()

    assert not app.exception
    labels = [element.label for element in app.tabs]
    assert any(label.startswith("실행 중") for label in labels)


def test_partial_publication_failure_keeps_only_the_failed_items(
    stub_api: list[tuple[str, dict]],
) -> None:
    """한 건이 실패해도 나머지는 계속 발행하고, 실패한 것만 재시도 대상이 된다."""
    _StubHandler.publication_failures = 1
    app = _rendered_app()
    app.slider(key="submission-count").set_value(2).run()
    _fill_draft(app, 1, "제목 1", "가설 1입니다.")
    _fill_draft(app, 2, "제목 2", "가설 2입니다.")

    app.button[0].click().run()

    assert not app.exception
    # 첫 건은 실패, 둘째 건은 성공 — 생성은 둘 다 됐다.
    created = [path for path, _ in stub_api if path == "/experiments"]
    assert len(created) == 2
    assert any("1건은 생성됐지만" in element.value for element in app.warning)


def test_new_submission_is_refused_while_publications_are_pending(
    stub_api: list[tuple[str, dict]],
) -> None:
    """남은 발행을 처리하기 전에 또 만들면 무엇이 어디까지 갔는지 설명할 수 없다."""
    _StubHandler.publication_failures = 1
    app = _rendered_app()
    _fill_required(app)
    app.button[0].click().run()
    created_before = len([path for path, _ in stub_api if path == "/experiments"])

    app.button[0].click().run()

    assert not app.exception
    created_after = len([path for path, _ in stub_api if path == "/experiments"])
    assert created_after == created_before


def test_creation_failure_survives_the_jump_to_the_board(
    stub_api: list[tuple[str, dict]],
) -> None:
    """3개를 냈는데 3번째 생성이 끊기면 앞의 둘은 정상 발행되어 화면이 보드로 넘어간다.
    그때 카드 수가 모자란 이유가 화면에 남아야 한다.

    이 사유를 `detail_error`에 담으면 발행 성공 경로가 지운다 — 사용자는 아무 설명
    없이 카드가 부족한 보드를 본다(#681 리뷰).
    """
    _StubHandler.fail_creation_at = 3
    app = _rendered_app()
    app.slider(key="submission-count").set_value(3).run()
    for order in range(1, 4):
        _fill_draft(app, order, f"제목 {order}", f"가설 {order}입니다.")

    app.button[0].click().run()

    assert not app.exception
    # 앞의 둘만 만들어지고 발행됐다.
    created = [path for path, _ in stub_api if path == "/experiments"]
    published = [path for path, _ in stub_api if path.endswith("/issue")]
    assert len(created) == 3  # 3번째 요청은 500으로 끊겼다
    assert len(published) == 2
    # 보드로 넘어갔지만 이유가 남아 있다.
    labels = [element.label for element in app.tabs]
    assert any(label.startswith("실행 중") for label in labels)
    warnings = [element.value for element in app.warning]
    assert any("가설 3번에서 실험 생성이 실패" in message for message in warnings)
    assert any("2건만 제출" in message for message in warnings)


def test_first_creation_failure_reports_the_real_cause(
    stub_api: list[tuple[str, dict]],
) -> None:
    """첫 생성이 실패하면 `pending_publications`가 빈 채로 넘어간다.

    거기서 발행 단계로 들어가면 "재시도할 이슈 발행 정보가 없습니다"가 실제 원인
    (500·타임아웃)을 덮어 사용자가 원인을 볼 수 없다(#681 리뷰).
    """
    _StubHandler.fail_creation_at = 1
    app = _rendered_app()
    _fill_required(app)

    app.button[0].click().run()

    assert not app.exception
    messages = [element.value for element in app.error]
    assert any("실험 생성이 실패" in message for message in messages)
    assert not any("재시도할 이슈 발행 정보가 없습니다" in message for message in messages)
    # 하나도 못 만들었으면 발행을 시도하지 않는다.
    assert not [path for path, _ in stub_api if path.endswith("/issue")]
