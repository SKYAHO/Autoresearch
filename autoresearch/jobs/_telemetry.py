"""Action-log 공개 batch process의 안전한 stdout telemetry 설정."""

from __future__ import annotations

import json
import logging
import sys


ACTION_LOG_TELEMETRY_LOGGERS = (
    "autoresearch.action_logs.pipeline",
    "autoresearch.action_logs.llm_generator",
)
_TELEMETRY_HANDLER_MARKER = "_autoresearch_action_log_stdout"
_SENSITIVE_TELEMETRY_FIELDS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "content",
        "judgments",
        "messages",
        "password",
        "persona",
        "persona_id",
        "prompt",
        "raw_prompt",
        "raw_request",
        "raw_response",
        "refresh_token",
        "request_body",
        "request_payload",
        "response_body",
        "response_payload",
        "secret",
        "token",
        "user",
        "user_id",
    }
)


def _contains_sensitive_telemetry_field(value: object) -> bool:
    """JSON value 안에 금지된 민감 필드가 있는지 재귀적으로 검사한다."""

    if isinstance(value, dict):
        for key, nested_value in value.items():
            if str(key).casefold() in _SENSITIVE_TELEMETRY_FIELDS:
                return True
            if _contains_sensitive_telemetry_field(nested_value):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_telemetry_field(item) for item in value)
    return False


class _ActionLogTelemetryFilter(logging.Filter):
    """한 줄 JSON object이며 민감 필드가 없는 action-log event만 허용한다."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            payload = json.loads(record.getMessage())
        except (TypeError, ValueError):
            return False
        if not isinstance(payload, dict):
            return False
        if not isinstance(payload.get("event"), str) or not payload["event"]:
            return False
        if _contains_sensitive_telemetry_field(payload):
            return False
        record.msg = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        record.args = ()
        return True


def configure_action_log_telemetry_logging() -> None:
    """Action-log JSON event를 prefix 없이 stdout으로 전달한다."""

    for logger_name in ACTION_LOG_TELEMETRY_LOGGERS:
        telemetry_logger = logging.getLogger(logger_name)
        telemetry_logger.setLevel(logging.INFO)
        telemetry_logger.propagate = False

        configured_handlers = [
            candidate
            for candidate in telemetry_logger.handlers
            if getattr(candidate, _TELEMETRY_HANDLER_MARKER, False)
        ]
        if configured_handlers:
            handler = configured_handlers[0]
            if handler.stream is not sys.stdout:
                try:
                    handler.setStream(sys.stdout)
                except ValueError:
                    # pytest capture처럼 이전 stream이 이미 닫힌 경우에도 재설정한다.
                    handler.stream = sys.stdout
        else:
            handler = logging.StreamHandler(sys.stdout)
            setattr(handler, _TELEMETRY_HANDLER_MARKER, True)
            handler.addFilter(_ActionLogTelemetryFilter())

        for existing_handler in list(telemetry_logger.handlers):
            telemetry_logger.removeHandler(existing_handler)
            if (
                existing_handler is not handler
                and getattr(existing_handler, _TELEMETRY_HANDLER_MARKER, False)
            ):
                existing_handler.close()
        telemetry_logger.addHandler(handler)
