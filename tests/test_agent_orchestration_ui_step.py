"""Streamlit workbench의 Step 표시 모델·상태 누적 계약을 검증한다.

전체 파이프라인 중 Experiment API의 Step 응답이 Streamlit 화면 상태로 들어오는 구간을
검증한다. HTTP 전송 자체와 Streamlit 위젯 렌더링은 담당하지 않는다 —
`agent_orchestration.ui.views`는 streamlit 의존성이 필요해 여기서 import하지 않는다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from agent_orchestration.ui import state as state_module
from agent_orchestration.ui.models import (
    Experiment,
    Step,
    Submission,
    step_kind_label,
    step_status_color,
)
from agent_orchestration.ui.app import should_open_experiment_detail
from agent_orchestration.ui.client import (
    STEP_PAGE_BUDGET,
    STEP_PAGE_SIZE,
    ExperimentClient,
)
from agent_orchestration.ui.state import (
    WorkbenchState,
    WorkbenchView,
    clear_activity_cache,
    merge_steps,
    select_experiment,
    show_create_view,
    show_experiment,
)


def test_workbench_starts_on_create_view() -> None:
    assert WorkbenchState().view is WorkbenchView.CREATE


def test_show_create_view_preserves_selection_and_activity() -> None:
    state = WorkbenchState(selected_id="one")
    state.event_cursor = "event-1"

    show_create_view(state)

    assert state.view is WorkbenchView.CREATE
    assert state.selected_id == "one"
    assert state.event_cursor == "event-1"


def test_show_experiment_selects_experiment_and_opens_detail() -> None:
    state = WorkbenchState()

    show_experiment(state, "two")

    assert state.view is WorkbenchView.DETAIL
    assert state.selected_id == "two"


def test_retained_sidebar_selection_does_not_exit_create_view() -> None:
    """가설 추가 뒤 유지된 radio 값은 상세 화면 전환 의도가 아니다."""
    state = WorkbenchState(selected_id="one")
    show_create_view(state)

    assert should_open_experiment_detail("one", selection_changed=False) is False
    assert state.view is WorkbenchView.CREATE
    assert state.selected_id == "one"


def test_discard_pending_publication_preserves_created_experiment() -> None:
    """취소는 발행 대기만 풀고 이미 생성된 Experiment는 목록에 남긴다."""
    experiment = Experiment.from_json(
        {
            "id": "exp-pending",
            "hypothesis": "보존할 가설",
            "status": "CREATED",
            "created_at": "2026-08-05T00:00:00+00:00",
            "updated_at": "2026-08-05T00:00:00+00:00",
            "issue_number": None,
            "issue_branch": None,
        }
    )
    submission = Submission(
        title="pending experiment",
        hypothesis="# 주제\n\n보존할 가설",
    )
    state = WorkbenchState(
        experiments=[experiment],
        pending_publications=[
            state_module.PendingPublication(
                experiment_id=experiment.id, submission=submission
            )
        ],
        detail_error="이슈 발행에 실패했습니다.",
    )

    state_module.discard_pending_publications(state)

    assert state.pending_publications == []
    assert state.detail_error is None
    # 폐기하는 것은 발행 대기이지 이미 만든 Experiment가 아니다.
    assert state.experiments == [experiment]


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "step-1",
        "experiment_id": "exp-1",
        "step_kind": "FEATURE_ASSEMBLY",
        "step_type": "assemble_training_dataset",
        "status": "STARTED",
        "message": "피처 조립 중",
        "target": {"features": ["views_per_day"]},
        "created_at": "2026-08-04T00:00:00+00:00",
        "updated_at": "2026-08-04T00:00:05+00:00",
    }
    payload.update(overrides)
    return payload


def _step(**overrides: Any) -> Step:
    return Step.from_json(_payload(**overrides))


def test_step_from_json_parses_all_fields() -> None:
    """API 응답이 표시 모델로 그대로 정규화된다."""
    step = _step()

    assert step.id == "step-1"
    assert step.step_kind == "FEATURE_ASSEMBLY"
    assert step.step_type == "assemble_training_dataset"
    assert step.status == "STARTED"
    assert step.target == {"features": ["views_per_day"]}
    assert step.created_at == datetime(2026, 8, 4, tzinfo=timezone.utc)
    assert step.updated_at == datetime(2026, 8, 4, 0, 0, 5, tzinfo=timezone.utc)


def test_step_accepts_null_message_and_target() -> None:
    """message·target은 PATCH 전체 교체로 null이 될 수 있다."""
    step = _step(message=None, target=None)

    assert step.message is None
    assert step.target is None


def test_display_line_uses_message_when_present() -> None:
    """message가 있으면 그대로 한 줄 표시에 쓴다."""
    assert _step(message="파생 피처 2개 생성").display_line == "파생 피처 2개 생성"


def test_display_line_falls_back_to_kind_and_type() -> None:
    """message가 없으면 step_kind·step_type 라벨로 대신한다 — 표시가 비지 않는다."""
    step = _step(message=None, step_kind="TRAIN", step_type="train_candidate")

    assert step.display_line == "학습 · train_candidate"


def test_display_line_falls_back_on_blank_message() -> None:
    """빈 문자열도 없는 것으로 본다."""
    step = _step(message="", step_kind="EVALUATE", step_type="evaluate_candidate")

    assert step.display_line == "평가 · evaluate_candidate"


def test_unknown_step_kind_label_falls_back_to_raw_value() -> None:
    """모르는 대분류는 원문을 그대로 보여준다 — 서버 CHECK가 막지만 방어한다."""
    assert step_kind_label("DEPLOY") == "DEPLOY"


def test_step_status_color_covers_all_states() -> None:
    """네 진행 상태가 서로 다른 색을 가진다."""
    colors = {
        step_status_color(status)
        for status in ("STARTED", "PROGRESS", "COMPLETED", "FAILED")
    }

    assert len(colors) == 4


def test_step_requires_timezone_aware_timestamp() -> None:
    """timezone 없는 timestamp는 표시 모델로 받지 않는다."""
    with pytest.raises(ValueError):
        _step(updated_at="2026-08-04T00:00:00")


def test_merge_steps_upserts_updated_step() -> None:
    """Step은 mutable이므로 같은 id가 다시 오면 최신 값으로 교체한다.

    Event·Log는 append-only라 중복 id를 무시하지만, Step은 진행 상태가 바뀌어 다시 온다.
    무시하면 STARTED로 굳어 화면이 멈춘 것처럼 보인다.
    """
    state = WorkbenchState()
    merge_steps(state, [_step(status="STARTED", message="조립 시작")])

    merge_steps(state, [_step(status="COMPLETED", message="조립 완료")])

    assert len(state.steps) == 1
    assert state.steps[0].status == "COMPLETED"
    assert state.steps[0].message == "조립 완료"


def test_merge_steps_keeps_position_of_existing_step() -> None:
    """갱신된 Step이 목록 끝으로 튀지 않는다."""
    state = WorkbenchState()
    merge_steps(state, [_step(id="a"), _step(id="b")])

    merge_steps(state, [_step(id="a", status="COMPLETED")])

    assert [step.id for step in state.steps] == ["a", "b"]
    assert state.steps[0].status == "COMPLETED"


def test_workbench_state_has_no_step_cursor() -> None:
    """Step cursor는 상태로 보관하지 않는다.

    갱신 사이에 cursor를 들고 가면 이미 받은 Step의 상태 변화를 관측하지 못한다. 필드를
    두면 event_cursor·log_cursor와 나란히 보여 다음 사람이 자연스럽게 연결하게 되므로,
    아예 두지 않는 것으로 그 경로를 막는다.
    """
    assert not hasattr(WorkbenchState(), "step_cursor")


def test_select_experiment_clears_step_cache() -> None:
    """실험을 바꾸면 Step 캐시도 초기화된다."""
    state = WorkbenchState()
    state.selected_id = "one"
    merge_steps(state, [_step()])

    select_experiment(state, "two")

    assert state.steps == []


def test_clear_activity_cache_clears_steps() -> None:
    """cursor 복구 경로가 Step 캐시도 함께 비운다."""
    state = WorkbenchState()
    merge_steps(state, [_step()])

    clear_activity_cache(state)

    assert state.steps == []


class _RecordingClient(ExperimentClient):
    """`_request_json`만 대체해 요청 경로와 페이지 응답을 통제하는 client."""

    def __init__(self, pages: list[list[dict[str, Any]]]) -> None:
        super().__init__("http://127.0.0.1:8000", "token")
        self._pages = pages
        self.paths: list[str] = []

    def _request_json(self, method: str, path: str, payload: object = None) -> Any:
        self.paths.append(path)
        items = self._pages[len(self.paths) - 1] if len(self.paths) <= len(self._pages) else []
        return {
            "items": items,
            "next_cursor": items[-1]["id"] if items else None,
        }


def test_get_steps_follows_pages_until_short_page() -> None:
    """한 번의 갱신 안에서 페이지를 이어 받아 최신 Step까지 가져온다.

    상한 100으로 한 페이지만 읽으면 화면이 **가장 오래된 100개에 고정**되어, 진행 표시가
    목적인 이 기능이 최신 스텝을 영원히 못 보여준다.
    """
    first = [_payload(id=f"s{index}") for index in range(STEP_PAGE_SIZE)]
    second = [_payload(id="tail-1"), _payload(id="tail-2")]
    client = _RecordingClient([first, second])

    steps, truncated = client.get_steps("exp-1")

    assert [step.id for step in steps] == [item["id"] for item in first + second]
    assert truncated is False
    assert len(client.paths) == 2
    assert f"limit={STEP_PAGE_SIZE}" in client.paths[0]
    assert "after_id=" not in client.paths[0]
    assert f"after_id=s{STEP_PAGE_SIZE - 1}" in client.paths[1]


def test_get_steps_stops_on_first_short_page() -> None:
    """상한보다 적게 오면 더 요청하지 않는다."""
    client = _RecordingClient([[_payload(id="only")]])

    steps, truncated = client.get_steps("exp-1")

    assert [step.id for step in steps] == ["only"]
    assert truncated is False
    assert len(client.paths) == 1


def test_get_steps_stops_at_page_budget() -> None:
    """페이지 예산을 넘겨 1초 polling이 무한 요청이 되지 않는다."""
    pages = [
        [_payload(id=f"p{page}-s{index}") for index in range(STEP_PAGE_SIZE)]
        for page in range(STEP_PAGE_BUDGET + 5)
    ]
    client = _RecordingClient(pages)

    _steps, truncated = client.get_steps("exp-1")

    assert len(client.paths) == STEP_PAGE_BUDGET
    # 조용히 버리지 않는다 — 예산에 걸렸음을 호출자가 알아야 화면에 드러낼 수 있다.
    assert truncated is True


def test_get_steps_stops_when_cursor_does_not_advance() -> None:
    """cursor가 전진하지 않으면 같은 페이지를 무한 반복하지 않는다."""
    same_page = [_payload(id=f"s{index}") for index in range(STEP_PAGE_SIZE)]
    client = _RecordingClient([same_page] * 5)

    _steps, truncated = client.get_steps("exp-1")

    assert len(client.paths) == 2
    assert truncated is False


def test_merge_steps_records_truncation_flag() -> None:
    """예산 초과 여부가 화면 상태로 전달된다."""
    state = WorkbenchState()

    merge_steps(state, [_step()], truncated=True)

    assert state.steps_truncated is True


def test_merge_steps_clears_truncation_when_fully_read() -> None:
    """다음 갱신에서 전부 읽으면 경고가 사라진다."""
    state = WorkbenchState()
    merge_steps(state, [_step()], truncated=True)

    merge_steps(state, [_step()], truncated=False)

    assert state.steps_truncated is False


def test_select_experiment_clears_truncation_flag() -> None:
    """실험을 바꾸면 이전 실험의 경고가 남지 않는다."""
    state = WorkbenchState()
    state.selected_id = "one"
    merge_steps(state, [_step()], truncated=True)

    select_experiment(state, "two")

    assert state.steps_truncated is False
