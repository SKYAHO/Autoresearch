"""Experiment API 응답을 Streamlit 표시 모델로 변환한다.

[파이프라인]
Experiment API와 Streamlit 화면 사이의 읽기 경계에서 JSON 응답을 안정적인 화면 모델로
정규화한다. HTTP 호출과 Streamlit session state는 담당하지 않는다.

[기능]
Experiment, Event, Log 불변 모델과 ISO timestamp 변환, 상태별 사용자 문구와 색상을
제공한다.

[비책임]
API 인증, cursor polling, UI 컴포넌트 렌더링, Agent 상태 기록.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


POLLING_STATUSES = frozenset({"CREATED", "RUNNING", "EVALUATING", "PASSED"})
TERMINAL_STATUSES = frozenset({"FAILED", "ERROR", "PROMOTED"})

_STATUS_LABELS = {
    "CREATED": "실행 대기",
    "RUNNING": "에이전트 실행 중",
    "EVALUATING": "지표와 유의성 평가 중",
    "PASSED": "판정 통과, 승격 대기",
    "FAILED": "실험 가설 미통과",
    "ERROR": "실행 또는 인프라 오류",
    "PROMOTED": "prod 승격 완료",
}

_STATUS_COLORS = {
    "CREATED": "#6B7280",
    "RUNNING": "#2563EB",
    "EVALUATING": "#C26A16",
    "PASSED": "#16724A",
    "FAILED": "#B42318",
    "ERROR": "#7F1D1D",
    "PROMOTED": "#0F766E",
}


def _timestamp(value: object) -> datetime:
    """API ISO timestamp를 timezone-aware datetime으로 변환한다."""
    if not isinstance(value, str):
        raise ValueError("Experiment API timestamp must be a string.")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Experiment API timestamp must include a timezone.")
    return parsed


def _mapping(value: object, field_name: str) -> dict[str, object] | None:
    """선택 JSON object를 화면 모델용 dict로 정규화한다."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"Experiment API field '{field_name}' must be an object.")
    return {str(key): item for key, item in value.items()}


@dataclass(frozen=True)
class Experiment:
    """현재 상태를 포함한 Experiment 화면 모델."""

    id: str
    hypothesis: str
    status: str
    metric_summary: dict[str, object] | None
    agent_session_id: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "Experiment":
        """Experiment API JSON을 검증해 모델로 변환한다."""
        return cls(
            id=str(payload["id"]),
            hypothesis=str(payload["hypothesis"]),
            status=str(payload["status"]),
            metric_summary=_mapping(payload.get("metric_summary"), "metric_summary"),
            agent_session_id=(
                str(payload["agent_session_id"])
                if payload.get("agent_session_id") is not None
                else None
            ),
            created_at=_timestamp(payload["created_at"]),
            updated_at=_timestamp(payload["updated_at"]),
        )


@dataclass(frozen=True)
class Event:
    """상태 전이 Event 화면 모델."""

    id: str
    experiment_id: str
    from_status: str | None
    to_status: str
    reason: str | None
    metric_snapshot: dict[str, object] | None
    created_at: datetime

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "Event":
        """Event API JSON을 검증해 모델로 변환한다."""
        return cls(
            id=str(payload["id"]),
            experiment_id=str(payload["experiment_id"]),
            from_status=(
                str(payload["from_status"])
                if payload.get("from_status") is not None
                else None
            ),
            to_status=str(payload["to_status"]),
            reason=str(payload["reason"]) if payload.get("reason") is not None else None,
            metric_snapshot=_mapping(payload.get("metric_snapshot"), "metric_snapshot"),
            created_at=_timestamp(payload["created_at"]),
        )


@dataclass(frozen=True)
class Log:
    """원본 실행 Log 화면 모델."""

    id: str
    experiment_id: str
    log_type: str
    content: str
    created_at: datetime

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "Log":
        """Log API JSON을 검증해 모델로 변환한다."""
        return cls(
            id=str(payload["id"]),
            experiment_id=str(payload["experiment_id"]),
            log_type=str(payload["log_type"]),
            content=str(payload["content"]),
            created_at=_timestamp(payload["created_at"]),
        )


def status_label(status: str) -> str:
    """상태 코드를 사람이 읽을 수 있는 한국어 문구로 반환한다."""
    return _STATUS_LABELS.get(status, status)


def status_color(status: str) -> str:
    """상태 코드를 배지 색상으로 반환한다."""
    return _STATUS_COLORS.get(status, "#334155")
