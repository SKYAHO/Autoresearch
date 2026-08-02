"""실험 워크벤치의 상태 전이 도메인 계약을 검증한다.

전체 파이프라인에서 실험 실행·평가·승격 상태가 잘못 역행하거나 건너뛰지 않도록 순수
검증 경계를 담당한다. DB transaction과 HTTP 상태 코드 매핑은 이 모듈의 검증 범위가
아니다.
"""

from __future__ import annotations

from itertools import product

import pytest

from agent_orchestration.app.experiments.models import ExperimentStatus
from agent_orchestration.app.experiments.transition_service import (
    InvalidTransitionError,
    validate_transition,
)


_ALLOWED_TRANSITIONS = (
    (ExperimentStatus.CREATED, ExperimentStatus.RUNNING),
    (ExperimentStatus.RUNNING, ExperimentStatus.EVALUATING),
    (ExperimentStatus.RUNNING, ExperimentStatus.ERROR),
    (ExperimentStatus.EVALUATING, ExperimentStatus.PASSED),
    (ExperimentStatus.EVALUATING, ExperimentStatus.FAILED),
    (ExperimentStatus.EVALUATING, ExperimentStatus.ERROR),
    (ExperimentStatus.PASSED, ExperimentStatus.PROMOTED),
)
_ALLOWED_TRANSITION_SET = frozenset(_ALLOWED_TRANSITIONS)
_REJECTED_TRANSITIONS = tuple(
    (current, requested)
    for current, requested in product(ExperimentStatus, repeat=2)
    if (current, requested) not in _ALLOWED_TRANSITION_SET
)


@pytest.mark.parametrize(("current", "requested"), _ALLOWED_TRANSITIONS)
def test_validate_transition_accepts_each_defined_transition(
    current: ExperimentStatus,
    requested: ExperimentStatus,
) -> None:
    """정의된 7개 전이 중 하나가 빠지는 회귀를 잡는다."""
    validate_transition(current, requested)


@pytest.mark.parametrize(("current", "requested"), _REJECTED_TRANSITIONS)
def test_validate_transition_rejects_every_undefined_transition(
    current: ExperimentStatus,
    requested: ExperimentStatus,
) -> None:
    """self-transition·역행·건너뛰기·터미널 재전이 중 하나가 열리는 회귀를 잡는다."""
    with pytest.raises(InvalidTransitionError) as error:
        validate_transition(current, requested)

    assert error.value.current is current
    assert error.value.requested is requested
