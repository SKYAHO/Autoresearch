"""병렬 실행 현황 보드의 계약을 고정한다.

전체 파이프라인 중 여러 실험이 동시에 진행되는 것을 사용자가 보는 화면 구간을
검증한다. 실제 실행과 상태 기록은 executor와 API 서버가 담당한다.

여기서 잡는 실패는 화면을 열어봐야 겨우 눈치채는 것들이다 — 보드 cursor가 상세
화면 cursor를 밀어 로그가 사라지거나, 종료된 실험의 cursor가 세션 내내 쌓이거나,
단계 조회 하나가 실패해 보드 전체가 오류로 덮이는 경우다(#671).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("streamlit", reason="orchestration-ui 그룹이 설치돼야 한다")

from applications.experiment_platform.workbench.models import (  # noqa: E402
    BOARD_RUNNING_STATUSES,
    BOARD_WAITING_STATUSES,
    EXECUTOR_STAGES,
    Experiment,
    POLLING_STATUSES,
    stage_index,
    stage_label,
)
from applications.experiment_platform.workbench.time import format_short_time  # noqa: E402
from applications.experiment_platform.workbench.state import (  # noqa: E402
    WorkbenchState,
    WorkbenchView,
    forget_board_entry,
    record_board_stage,
    show_board,
)


NOW = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)


def _experiment(experiment_id: str, status: str) -> Experiment:
    return Experiment(
        id=experiment_id,
        hypothesis="learning_rate를 0.05에서 0.03으로 낮춘다.",
        status=status,
        metric_summary=None,
        agent_session_id=None,
        created_at=NOW - timedelta(minutes=5),
        updated_at=NOW,
    )


def test_stage_table_covers_the_seven_executor_init_containers() -> None:
    """이 표가 틀리면 진행률이 통째로 어긋난다.

    executor Job의 init 컨테이너 순서가 곧 진행 단계이고, 로그 수집기가 붙이는
    `log_type`이 이 이름들이다.
    """
    assert [name for name, _ in EXECUTOR_STAGES] == [
        "branch-token-minter",
        "branch-creator",
        "clone-token-minter",
        "workspace-preparer",
        "codex-worker",
        "candidate-verifier",
        "push-token-minter",
    ]


def test_kubectl_init_count_maps_to_the_running_stage() -> None:
    """`Init:N/7`은 "N개 완료"라 N번째(0-based)가 실행 중이다.

    팀이 `kubectl`로 보던 숫자와 화면이 어긋나면 보드를 믿을 수 없다.
    """
    assert stage_index("codex-worker") == 4  # Init:4/7
    assert stage_index("workspace-preparer") == 3  # Init:3/7
    assert stage_index("candidate-verifier") == 5  # Init:5/7


def test_unknown_log_type_is_not_forced_into_the_stage_table() -> None:
    """에이전트가 만든 임의 `log_type`을 7단계 어딘가로 우겨넣지 않는다."""
    assert stage_index("codex-stdout") is None
    assert stage_label("codex-stdout") is None


def test_board_cursor_does_not_touch_the_detail_log_cursor() -> None:
    """보드가 상세 화면의 로그 위치를 밀면 원본 로그 탭이 구간을 건너뛴다.

    두 화면이 같은 실험을 동시에 볼 수 있으므로 cursor는 반드시 분리돼야 한다
    (spec 결정 4).
    """
    state = WorkbenchState()
    state.log_cursor = "detail-cursor"

    record_board_stage(state, "exp-1", cursor="board-cursor", log_type="codex-worker")

    assert state.log_cursor == "detail-cursor"
    assert state.board_log_cursors["exp-1"] == "board-cursor"
    assert state.board_stages["exp-1"] == "codex-worker"


def test_stage_survives_a_page_without_any_stage_log() -> None:
    """단계가 아닌 로그만 새로 왔다고 카드가 "대기 중"으로 되돌아가면 안 된다."""
    state = WorkbenchState()
    record_board_stage(state, "exp-1", cursor="c1", log_type="codex-worker")

    record_board_stage(state, "exp-1", cursor="c2", log_type=None)

    assert state.board_stages["exp-1"] == "codex-worker"
    assert state.board_log_cursors["exp-1"] == "c2"


def test_finished_experiments_are_dropped_from_board_state() -> None:
    """안 버리면 두 dict가 실험 수만큼 무한히 자란다."""
    state = WorkbenchState()
    record_board_stage(state, "exp-1", cursor="c1", log_type="codex-worker")

    forget_board_entry(state, "exp-1")

    assert state.board_log_cursors == {}
    assert state.board_stages == {}
    # 없는 실험을 지우는 것도 조용히 성공해야 한다 — 갱신 루프가 매번 부른다.
    forget_board_entry(state, "exp-1")


def test_show_board_switches_the_view() -> None:
    state = WorkbenchState()

    show_board(state)

    assert state.view is WorkbenchView.BOARD


def test_card_shows_the_submission_time_in_kst_not_an_elapsed_guess() -> None:
    """경과 시간은 `created_at` 기준이라 슬롯 대기 시간까지 포함한다.

    카드에서 "얼마나 돌고 있나"로 읽히면 팀이 보던 `kubectl` pod AGE와 체계적으로
    어긋난다. 시작 시각을 담은 필드가 없으므로 있는 사실(제출 시각)만 적는다.
    """
    assert format_short_time(NOW) == "08-11 09:00"  # UTC 00:00 → KST 09:00


def test_board_splits_running_waiting_and_done() -> None:
    """`CREATED`는 비종료지만 아직 시작하지 않았다 — 실행 중과 갈라 놓는다."""
    experiments = [
        _experiment("running", "RUNNING"),
        _experiment("evaluating", "EVALUATING"),
        _experiment("waiting", "CREATED"),
        _experiment("passed", "PASSED"),
        _experiment("error", "ERROR"),
    ]

    running = [e for e in experiments if e.status in BOARD_RUNNING_STATUSES]
    waiting = [e for e in experiments if e.status in BOARD_WAITING_STATUSES]
    done = [
        e
        for e in experiments
        if e.status not in BOARD_RUNNING_STATUSES | BOARD_WAITING_STATUSES
    ]

    assert [e.id for e in running] == ["running", "evaluating"]
    assert [e.id for e in waiting] == ["waiting"]
    assert [e.id for e in done] == ["passed", "error"]


def test_passed_is_not_running_even_though_it_is_not_terminal() -> None:
    """`POLLING_STATUSES`를 보드에 그대로 쓰면 `PASSED`가 "실행 중"에 섞인다.

    `PASSED`는 `PROMOTED`로 가는 간선이 남아 있어 종료 상태가 아니지만, executor는
    이미 끝났다. 보드가 묻는 것은 "지금 도는가"이므로 다른 분류가 필요하다(#671).
    """
    assert "PASSED" in POLLING_STATUSES
    assert "PASSED" not in BOARD_RUNNING_STATUSES
    assert "PASSED" not in BOARD_WAITING_STATUSES
