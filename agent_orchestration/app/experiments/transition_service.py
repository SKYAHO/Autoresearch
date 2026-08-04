"""Agent Orchestration 실험 상태 전이를 검증한다.

전체 파이프라인에서 실험 실행·평가·승격 상태가 승인된 방향으로만 이동하도록 하는 순수
도메인 검증 구간을 담당한다. DB locking·상태 저장과 HTTP 오류 변환은 담당하지 않는다.

현재 상태와 요청 상태를 받아 허용되지 않은 조합에 `InvalidTransitionError`를 발생시키는
fail-closed 검증 함수를 제공한다.
"""

from __future__ import annotations

from agent_orchestration.app.experiments.models import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    ExperimentStatus,
)


class InvalidTransitionError(ValueError):
    """승인된 상태 그래프에 없는 전이를 요청했다."""

    def __init__(self, current: ExperimentStatus, requested: ExperimentStatus) -> None:
        self.current = current
        self.requested = requested
        super().__init__(f"Invalid transition: {current.value} -> {requested.value}")


def validate_transition(
    current: ExperimentStatus,
    requested: ExperimentStatus,
) -> None:
    """허용되지 않은 상태 전이를 `InvalidTransitionError`로 거부한다."""
    if current in TERMINAL_STATUSES:
        raise InvalidTransitionError(current, requested)
    if requested not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvalidTransitionError(current, requested)
