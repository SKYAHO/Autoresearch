"""Streamlit workbench의 Step 표시 모델·상태 누적 계약을 검증한다.

전체 파이프라인 중 Experiment API의 Step 응답이 Streamlit 화면 상태로 들어오는 구간을
검증한다. HTTP 전송 자체와 Streamlit 위젯 렌더링은 담당하지 않는다 —
`agent_orchestration.ui.views`는 streamlit 의존성이 필요해 여기서 import하지 않는다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from agent_orchestration.ui.models import Step, step_kind_label, step_status_color
from agent_orchestration.ui.state import (
    WorkbenchState,
    append_step_page,
    clear_activity_cache,
    select_experiment,
)


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


def test_append_step_page_upserts_updated_step() -> None:
    """Step은 mutable이므로 같은 id가 다시 오면 최신 값으로 교체한다.

    Event·Log는 append-only라 중복 id를 무시하지만, Step은 진행 상태가 바뀌어 다시 온다.
    무시하면 STARTED로 굳어 화면이 멈춘 것처럼 보인다.
    """
    state = WorkbenchState()
    append_step_page(state, [_step(status="STARTED", message="조립 시작")], "cursor-1")

    append_step_page(state, [_step(status="COMPLETED", message="조립 완료")], "cursor-1")

    assert len(state.steps) == 1
    assert state.steps[0].status == "COMPLETED"
    assert state.steps[0].message == "조립 완료"


def test_append_step_page_keeps_position_of_existing_step() -> None:
    """갱신된 Step이 목록 끝으로 튀지 않는다."""
    state = WorkbenchState()
    append_step_page(state, [_step(id="a"), _step(id="b")], None)

    append_step_page(state, [_step(id="a", status="COMPLETED")], None)

    assert [step.id for step in state.steps] == ["a", "b"]
    assert state.steps[0].status == "COMPLETED"


def test_append_step_page_advances_cursor_only_when_present() -> None:
    """next_cursor가 null이면 기존 cursor를 유지한다."""
    state = WorkbenchState()
    append_step_page(state, [_step()], "cursor-1")

    append_step_page(state, [], None)

    assert state.step_cursor == "cursor-1"


def test_select_experiment_clears_step_cache() -> None:
    """실험을 바꾸면 Step 캐시와 cursor도 초기화된다."""
    state = WorkbenchState()
    state.selected_id = "one"
    append_step_page(state, [_step()], "cursor-1")

    select_experiment(state, "two")

    assert state.steps == []
    assert state.step_cursor is None


def test_clear_activity_cache_clears_steps() -> None:
    """cursor 복구 경로가 Step 캐시도 함께 비운다."""
    state = WorkbenchState()
    append_step_page(state, [_step()], "cursor-1")

    clear_activity_cache(state)

    assert state.steps == []
    assert state.step_cursor is None
