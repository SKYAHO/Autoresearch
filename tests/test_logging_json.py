"""#352 구조화 JSON 로깅 계약 검증 — infra #359 수집단(ndjson parser)과의 계약:
한 줄 JSON, 대문자 log.level, RFC3339 @timestamp, traceback 단일 이벤트."""

from __future__ import annotations

import io
import json
import logging
import re

from autoresearch.logging_json import EcsJsonFormatter


def _make_handler() -> tuple[logging.Handler, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        EcsJsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={
                "asctime": "@timestamp",
                "levelname": "log.level",
                "name": "log.logger",
            },
        )
    )
    return handler, stream


def test_json_one_line_with_ecs_fields() -> None:
    handler, stream = _make_handler()
    logger = logging.getLogger("test.ecs.fields")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.error("boom %s", "detail")
    finally:
        logger.removeHandler(handler)

    lines = stream.getvalue().strip().split("\n")
    assert len(lines) == 1
    doc = json.loads(lines[0])
    # 계약: log.level은 대문자(levelname 그대로) — Kibana 검색이 대문자 기준.
    assert doc["log.level"] == "ERROR"
    assert doc["log.logger"] == "test.ecs.fields"
    assert doc["message"] == "boom detail"
    # 계약: @timestamp는 RFC3339 UTC(ms) — Filebeat이 그대로 이벤트 시각으로 쓴다.
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", doc["@timestamp"]
    )


def test_traceback_single_event_in_error_stack_trace() -> None:
    handler, stream = _make_handler()
    logger = logging.getLogger("test.ecs.exc")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        try:
            raise ValueError("original cause")
        except ValueError:
            logger.exception("failed")
    finally:
        logger.removeHandler(handler)

    lines = stream.getvalue().strip().split("\n")
    # traceback이 줄 단위로 쪼개지지 않고 한 이벤트의 한 필드에 담긴다.
    assert len(lines) == 1
    doc = json.loads(lines[0])
    assert "Traceback" in doc["error.stack_trace"]
    assert "original cause" in doc["error.stack_trace"]
    assert doc["error.type"] == "ValueError"
    assert "exc_info" not in doc
