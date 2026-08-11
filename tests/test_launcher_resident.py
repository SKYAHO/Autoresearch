"""상주 프로세스 공용 루프의 수명·로깅 계약을 검증한다(#689).

[파이프라인] launcher 이미지로 도는 상주 프로세스들(로그 수집기 #559, PR 생성기
#689)이 공유하는 tick 루프를 담당한다. 각 프로세스가 tick 안에서 무엇을 하는지는
다루지 않는다.

[비책임] 수집·PR 생성 로직, DB·GitHub 호출은 각 모듈의 테스트가 다룬다.
"""

from __future__ import annotations

import logging
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_orchestration.launcher.resident import run_forever  # noqa: E402


def test_a_tick_exception_does_not_kill_the_loop() -> None:
    """죽으면 워크로드가 재시작하지만 그 사이가 비고, 끊긴 것을 알 경로가 없다."""
    calls: list[int] = []

    def tick() -> list[str]:
        calls.append(1)
        raise RuntimeError("boom")

    run_forever(
        tick,
        should_stop=lambda: len(calls) >= 2,
        sleep=lambda _seconds: None,
        interval_sec=1,
        label="pull request opener",
    )

    assert len(calls) == 2


def test_the_label_names_the_process_in_logs(caplog) -> None:
    """두 상주 프로세스가 같은 이미지·namespace에서 돈다.

    라벨이 없으면 PR 생성기의 실패가 "log collector"로 남아, 운영자가 엉뚱한
    컴포넌트를 뒤진다.
    """
    caplog.set_level(logging.WARNING)

    def tick() -> list[str]:
        return ["pull_request_forbidden"]

    run_forever(
        tick,
        should_stop=lambda: True,
        sleep=lambda _seconds: None,
        interval_sec=1,
        label="pull request opener",
    )

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "pull request opener" in messages
    assert "log collector" not in messages
    assert "pull_request_forbidden" in messages


def test_a_failing_tick_is_labelled_too(caplog) -> None:
    caplog.set_level(logging.WARNING)

    def tick() -> list[str]:
        raise RuntimeError("boom")

    run_forever(
        tick,
        should_stop=lambda: True,
        sleep=lambda _seconds: None,
        interval_sec=1,
        label="pull request opener",
    )

    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "pull request opener" in messages
    assert "reason=tick_failed" in messages


def test_it_does_not_sleep_before_shutting_down() -> None:
    """종료 신호를 받은 뒤 주기만큼 더 기다리면 그만큼 종료가 늦어진다."""
    slept: list[float] = []

    run_forever(
        lambda: [],
        should_stop=lambda: True,
        sleep=slept.append,
        interval_sec=7,
        label="test",
    )

    assert slept == []
