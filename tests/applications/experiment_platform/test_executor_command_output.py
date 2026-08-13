"""subprocess 출력을 로그에 실을 형태로 다듬는 계약을 고정한다(#636).

`training`·`measurement`가 공유하는 순수 함수만 다룬다. 어느 실패에 무엇을 남기는지는
각 모듈의 테스트가 소유한다.
"""

from __future__ import annotations

import logging
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from applications.experiment_platform.executor.command_output import (  # noqa: E402
    OUTPUT_TAIL_BYTES,
    decode_output,
    log_command_streams,
    output_tail,
)


def test_timeout_output_arrives_as_bytes_and_is_decoded() -> None:
    """`TimeoutExpired`는 `text=True`여도 bytes를 싣는다 — 그대로 찍으면 `b'…'`가 된다."""
    assert decode_output(b"RecursionError\n") == "RecursionError\n"


def test_absent_output_becomes_an_empty_string() -> None:
    """자식이 그 스트림에 아무것도 안 쓰면 `None`이 온다."""
    assert decode_output(None) == ""


def test_output_within_the_cap_is_left_alone() -> None:
    assert output_tail("짧은 출력") == "짧은 출력"


def test_output_over_the_cap_keeps_the_end_not_the_beginning() -> None:
    """오류는 출력의 끝에 온다 — 트레이스백은 항상 마지막에 찍힌다."""
    text = "HEAD" + "x" * (OUTPUT_TAIL_BYTES * 2) + "TAIL"

    tail = output_tail(text)

    assert tail.endswith("TAIL")
    assert "HEAD" not in tail


def test_a_cut_through_a_multi_byte_character_does_not_raise() -> None:
    """상한 경계가 한글 중간을 지나도 터지지 않는다.

    `OUTPUT_TAIL_BYTES`(65536)는 3의 배수가 아니라 3바이트 문자 열을 자르면 경계가 반드시
    문자 중간에 떨어진다. `errors="replace"` 없이는 `UnicodeDecodeError`가 나고, 그러면
    실패 로그를 남기려다 로깅 자체가 죽는다.
    """
    text = "가" * (OUTPUT_TAIL_BYTES // 3 + 1000)

    tail = output_tail(text)

    assert tail.endswith("가")
    assert len(tail) < len(text)


def test_empty_stream_still_gets_a_line(caplog: pytest.LogCaptureFixture) -> None:
    """비어 있어도 한 줄을 남겨 "출력이 없었다"와 "로깅이 깨졌다"를 구분한다."""
    logger = logging.getLogger("test_command_output")

    with caplog.at_level(logging.ERROR, logger="test_command_output"):
        log_command_streams(
            logger, event="training output", stage="uv_sync", stdout="", stderr=None
        )

    assert "stream=stdout bytes=0" in caplog.text
    assert "stream=stderr bytes=0" in caplog.text
