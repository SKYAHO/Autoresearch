"""executor가 띄운 subprocess의 출력을 로그에 실을 형태로 다듬는 경계(#636).

[파이프라인] 학습(`training.py`)과 채점(`measurement.py`)이 workspace 코드를 subprocess로
실행한 뒤, 실패 사유를 컨테이너 로그로 내보내기 직전 구간을 담당한다. 그 로그는 수집기가
`experiment_logs`로 옮겨 워크벤치가 읽는다(#559).

[기능] `TimeoutExpired`가 싣는 bytes 출력을 문자열로 바꾸고, 로그에 실을 만큼만 출력의
뒤를 남긴다.

[비책임] subprocess 실행과 실패 판정(`training.py`·`measurement.py`), credential 형식
감지(`safety.py`), Codex 출력 처리(`codex_worker.py`의 링버퍼)는 담당하지 않는다.
"""

from __future__ import annotations

import logging
from typing import Final


# 진단용 출력 tail 상한. `codex_worker._PIPE_RING_BUFFER_BYTES`와 같은 값이다(#612) —
# 같은 목적(실패 원인을 읽을 만큼만 남긴다)이라 두 경로가 다른 크기를 쓸 이유가 없다.
OUTPUT_TAIL_BYTES: Final = 64 * 1024


def decode_output(output: str | bytes | None) -> str:
    """subprocess 출력을 로그에 실을 문자열로 만든다.

    `text=True`를 줘도 **`TimeoutExpired`가 싣는 출력만은 bytes다.** 그대로 찍으면
    `b'…'` 형태가 되어 읽을 수 없다. 출력이 아예 없으면 `None`이 온다.

    `errors="replace"`인 이유는 tail을 자를 때와 같다 — 강제 종료된 프로세스의 마지막
    구간은 multi-byte 문자 중간에서 끊길 수 있다.
    """
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output


def output_tail(text: str) -> str:
    """로그에 실을 만큼만 출력의 **뒤**를 남긴다.

    앞이 아니라 뒤인 이유는 오류가 출력의 끝에 오기 때문이다 — 트레이스백은 항상
    마지막에 찍힌다. 상한이 없으면 학습 진행 로그가 실패 한 건에 수 MB씩 실리고,
    수집기가 그것을 8000자 청크로 쪼개 DB에 적재한다(#559).

    자르는 자리가 multi-byte 문자 중간일 수 있어 `errors="replace"`로 복구한다.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= OUTPUT_TAIL_BYTES:
        return text
    return encoded[-OUTPUT_TAIL_BYTES:].decode("utf-8", errors="replace")


def log_command_streams(
    logger: logging.Logger,
    *,
    event: str,
    stage: str,
    stdout: str | bytes | None,
    stderr: str | bytes | None,
) -> None:
    """실패한 명령의 두 스트림을 각각 한 줄씩 남긴다.

    **stdout도 남기는 이유**는 진행 상황이 거기 있기 때문이다. `autoresearch/model_training/train.py`는
    단계를 `print()`로 내므로 `[Step 1]`~`[Step 8]`이 stdout에 쌓인다. 실험 #633 진단의
    절반이 "8단계는 다 통과했고 마지막 ONNX 패키징에서 죽었다"였고, 그 출처가 여기다.

    **비어 있어도 한 줄을 남기는 이유**는 "출력이 없었다"와 "로깅이 깨졌다"를 구분하기
    위해서다 — `codex_worker`의 출력을 찍는 `phase2._log_codex_output`과 같은 판단이다.
    """
    for name, raw in (("stdout", stdout), ("stderr", stderr)):
        text = output_tail(decode_output(raw))
        if text:
            logger.error(
                "%s stage=%s stream=%s bytes=%d\n%s",
                event,
                stage,
                name,
                len(text.encode("utf-8")),
                text,
            )
        else:
            logger.error("%s stage=%s stream=%s bytes=0", event, stage, name)
