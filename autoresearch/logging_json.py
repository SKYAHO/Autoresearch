"""구조화(JSON 한 줄) 로깅 설정 — serving과 batch job의 공용 진입점 (#352).

파드 stdout 로그를 Filebeat ndjson parser(infra #359)가 최상위 필드로
전개하므로, Kibana에서 필드 기반 검색이 되도록 ECS 필드명으로 내보낸다.

인프라 수집단과의 계약(infra #359 리뷰에서 확정):
- ``log.level`` 값은 대문자(levelname 그대로) — Kibana 저장 검색이 대문자 기준.
- 최상위 예약 object 키(log/error/host/event/service/agent/kubernetes)를
  스칼라로 찍지 않는다. ECS dotted key(``log.level`` 등)는 수집단
  ``expand_keys``가 계층으로 전개하므로 안전.
- ``@timestamp``는 wall-clock RFC3339(UTC)만.
"""

from __future__ import annotations

import logging
import logging.config
import os
import time

_CONFIGURED = False


def setup_json_logging(level: str = "INFO") -> None:
    """root logger를 JSON 한 줄 stdout으로 구성한다. 멱등.

    ``AUTORESEARCH_JSON_LOGS=0``이면 아무것도 하지 않는다(로컬 개발용 —
    표준 평문 로깅 유지. 수집단은 비JSON 라인도 원문 보존하므로 안전).
    """
    global _CONFIGURED
    if _CONFIGURED or os.environ.get("AUTORESEARCH_JSON_LOGS") == "0":
        return
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {
                    "()": "autoresearch.logging_json.EcsJsonFormatter",
                    "fmt": "%(asctime)s %(levelname)s %(name)s %(message)s",
                    "rename_fields": {
                        "asctime": "@timestamp",
                        "levelname": "log.level",
                        "name": "log.logger",
                    },
                }
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {"handlers": ["stdout"], "level": level},
            # uvicorn은 자체 핸들러(색상 평문)를 다는데, serving에서는
            # app 모듈 import가 uvicorn 로깅 구성 이후라 여기서 걷어내고
            # root JSON 핸들러로 전파시킨다. access 로그의 log.logger가
            # "uvicorn.access"로 남아 Kibana 저장 검색과 맞는다.
            "loggers": {
                "uvicorn": {"handlers": [], "propagate": True},
                "uvicorn.error": {"handlers": [], "propagate": True},
                "uvicorn.access": {"handlers": [], "propagate": True},
            },
        }
    )
    _CONFIGURED = True


def _import_json_formatter():
    # python-json-logger 3.x는 pythonjsonlogger.json, 2.x는 .jsonlogger.
    try:
        from pythonjsonlogger.json import JsonFormatter
    except ImportError:  # pragma: no cover - 구버전 폴백
        from pythonjsonlogger.jsonlogger import JsonFormatter
    return JsonFormatter


class EcsJsonFormatter(_import_json_formatter()):
    """@timestamp를 RFC3339(UTC, ms)로, traceback을 error.stack_trace로."""

    converter = time.gmtime

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        base = time.strftime("%Y-%m-%dT%H:%M:%S", self.converter(record.created))
        return f"{base}.{int(record.msecs):03d}Z"

    def add_fields(self, log_record, record, message_dict) -> None:
        super().add_fields(log_record, record, message_dict)
        # 기본 exc_info 문자열 필드 대신 ECS error.stack_trace 한 필드로 —
        # traceback이 한 이벤트에 담겨 Kibana에서 줄 단위로 쪼개지지 않는다.
        stack = log_record.pop("exc_info", None)
        if stack:
            log_record["error.stack_trace"] = stack
            log_record.setdefault(
                "error.type",
                record.exc_info[0].__name__ if record.exc_info else "Exception",
            )
