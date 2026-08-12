"""워크벤치 실행 비용의 조회 배선과 결과 탭 렌더를 검증한다.

전체 파이프라인에서 API가 돌려준 실행 비용이 결과 탭에 그려지기까지의 UI 구간을
검증한다. 값의 파생·환산과 endpoint는 `tests/test_experiment_cost_api.py`가 담당한다.

**이 파일이 지키는 것은 격리다.** 비용 조회 하나가 실패해도 워크벤치 갱신 전체가
오류로 넘어가면 안 된다 — 리포트에서 같은 실수를 이미 겪었다(#647).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="orchestration-ui 그룹이 설치돼야 한다")

from streamlit.testing.v1 import AppTest  # noqa: E402

from agent_orchestration.ui.app import refresh_cost  # noqa: E402
from agent_orchestration.ui.client import ApiUnavailableError  # noqa: E402
from agent_orchestration.ui.models import (  # noqa: E402
    Experiment,
    ExperimentCost,
    StageTokens,
)
from agent_orchestration.ui.state import WorkbenchState  # noqa: E402


_COST = ExperimentCost(
    wall_clock_seconds=1800.0,
    compute_usd=0.0178,
    breakdown_available=True,
    stages=(
        StageTokens(
            stage="codex-worker",
            input_tokens=94_393,
            cached_input_tokens=84_954,
            fresh_input_tokens=9_439,
            output_tokens=4_968,
            reasoning_output_tokens=1_200,
        ),
    ),
    total_tokens=99_361,
    token_usd=0.0095,
    token_usd_without_cache=0.0248,
)


class _StubClient:
    """`fetch_cost`만 답하는 최소 client."""

    def __init__(self, result: object) -> None:
        self.result = result
        self.calls = 0

    def fetch_cost(self, experiment_id: str) -> ExperimentCost:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        assert isinstance(self.result, ExperimentCost)
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


def test_cost_is_not_queried_before_the_experiment_completes() -> None:
    """완주 전에는 비용이 확정되지 않으므로 묻지 않는다."""
    client = _StubClient(_COST)
    state = _state_with("RUNNING")

    refresh_cost(client, state)

    assert client.calls == 0
    assert state.cost is None


def test_cost_is_fetched_once_and_stops() -> None:
    """성공하면 한 번으로 그친다 — 5초 polling에 태우지 않는다."""
    client = _StubClient(_COST)
    state = _state_with("PASSED")

    refresh_cost(client, state)
    refresh_cost(client, state)

    assert client.calls == 1
    assert state.cost is not None
    assert state.cost.stages[0].stage == "codex-worker"


def test_cost_failure_retries_and_never_touches_the_detail_error() -> None:
    """비용 조회 실패가 워크벤치 전체를 오류 상태로 만들면 안 된다."""
    client = _StubClient(ApiUnavailableError("일시적 오류"))
    state = _state_with("PASSED")

    refresh_cost(client, state)
    refresh_cost(client, state)

    assert client.calls == 2
    assert state.cost_error is not None
    assert state.detail_error is None


_RENDER_SCRIPT = '''
from datetime import datetime, timezone
import streamlit as st
from agent_orchestration.ui.models import Experiment, ExperimentCost, StageTokens
from agent_orchestration.ui.state import WorkbenchState
from agent_orchestration.ui import views

mode = st.session_state.get("mode", "full")
state = WorkbenchState(selected_id="exp-1")
state.experiment = Experiment(
    id="exp-1", hypothesis="가설", status="PASSED", metric_summary=None,
    agent_session_id=None,
    created_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    updated_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
)
if mode == "full":
    state.cost = ExperimentCost(
        wall_clock_seconds=1834.0, compute_usd=0.0181, breakdown_available=True,
        stages=(StageTokens("codex-worker", 94393, 84954, 9439, 4968, 1200),),
        total_tokens=99361, token_usd=0.0095, token_usd_without_cache=0.0248,
    )
elif mode == "legacy":
    state.cost = ExperimentCost(
        wall_clock_seconds=1834.0, compute_usd=0.0181, breakdown_available=False,
        stages=(), total_tokens=75049, token_usd=None, token_usd_without_cache=None,
    )
else:
    state.cost_error = "일시적 오류"
views._render_cost(state)
'''


@pytest.mark.parametrize("mode", ("full", "legacy", "error"))
def test_cost_section_renders_without_exception(
    tmp_path: Path, mode: str
) -> None:
    """세 갈래(분해 있음·총량뿐·조회 실패) 모두 예외 없이 그려져야 한다."""
    script = tmp_path / "render_cost.py"
    script.write_text(_RENDER_SCRIPT, encoding="utf-8")
    app = AppTest.from_file(str(script))
    app.session_state["mode"] = mode

    app.run()

    assert not app.exception
    if mode == "error":
        assert "실행 비용을 불러오지 못했습니다" in app.warning[0].value
        return
    assert [metric.label for metric in app.metric] == ["실행 시간", "컴퓨트", "토큰"]
    if mode == "legacy":
        # 분해가 없으면 금액 자리는 비고, 왜 비었는지를 캡션이 밝힌다.
        assert app.metric[2].value == "—"
        assert "과금 구분이 남기 전에" in app.caption[0].value
    else:
        assert app.metric[2].value == "$0.0095"
        assert "프롬프트 캐싱으로" in app.caption[0].value
