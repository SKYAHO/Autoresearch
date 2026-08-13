"""launcher 이미지로 도는 상주 프로세스들의 공용 tick 루프.

[파이프라인] 로그 수집기(#559)와 PR 생성기(#689)가 같은 이미지·같은 namespace에서
서로 다른 진입점으로 돈다. 두 프로세스의 수명 규칙이 갈리지 않도록 루프를 한 곳에
둔다.

[기능] 어떤 예외로도 죽지 않는 tick 루프와, 어느 프로세스가 남긴 로그인지 구분되는
라벨을 제공한다.

[비책임] tick 안에서 무엇을 하는지(수집·PR 생성), DB·GitHub 호출, 종료 신호 등록은
각 진입점이 담당한다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable


_LOGGER = logging.getLogger(__name__)


def run_forever(
    tick: Callable[[], list[str]],
    *,
    should_stop: Callable[[], bool],
    sleep: Callable[[float], None],
    interval_sec: float,
    label: str,
) -> None:
    """tick을 주기적으로 돌린다.

    **어떤 예외로도 루프가 죽지 않는다.** 죽으면 워크로드가 재시작하지만 그 사이가
    비고, 관측이 끊긴 것을 알아챌 경로가 또 없다. tick 안의 단위 격리(Job·실험)는
    그 아래 층이고, 여기서는 목록 조회 실패나 세션 생성 실패처럼 tick 전체를
    무너뜨리는 것을 잡는다.

    `should_stop`이 참이면 **진행 중인 tick을 마친 뒤** 빠져나간다 — 쓰기 도중에
    끊지 않는다. 종료 신호를 받은 뒤에는 주기만큼 더 기다리지 않는다.

    `label`은 로그에서 어느 상주 프로세스인지 가르는 값이다. 두 프로세스가 같은
    이미지로 도는 만큼, 없으면 한쪽의 실패가 다른 쪽 이름으로 남아 운영자가 엉뚱한
    컴포넌트를 뒤진다.
    """
    while True:
        try:
            problems = tick()
        except Exception:
            _LOGGER.warning("%s tick failed reason=tick_failed", label, exc_info=True)
        else:
            if problems:
                _LOGGER.warning(
                    "%s tick completed with problems reasons=%s",
                    label,
                    ",".join(sorted(set(problems))),
                )
        if should_stop():
            return
        sleep(interval_sec)
