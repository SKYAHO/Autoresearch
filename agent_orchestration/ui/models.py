"""Experiment API 응답을 Streamlit 표시 모델로 변환한다.

[파이프라인]
Experiment API와 Streamlit 화면 사이의 읽기 경계에서 JSON 응답을 안정적인 화면 모델로
정규화한다. HTTP 호출과 Streamlit session state는 담당하지 않는다.

[기능]
Experiment, Event, Log 불변 모델과 ISO timestamp 변환, 상태별 사용자 문구와 색상을
제공한다. 사전등록 제출 폼이 API에 보내는 값(지표 방향, 허용 범위 키)과 그 한국어
표시 문구도 여기서 정의한다.

[비책임]
API 인증, cursor polling, UI 컴포넌트 렌더링, Agent 상태 기록.

UI 이미지는 `issue_authoring.py`를 포함하지 않으므로(`deploy/agent_orchestration/
ui.Dockerfile`) 옵션 값을 import하지 않고 여기에 둔다. 값은 표시 문구가 아니라 **HTTP
계약**이며, 서버 상수와의 동일성은 `tests/test_ui_submission_form.py`가 고정한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent_orchestration.app.experiments.models import (
    TERMINAL_STATUSES as API_TERMINAL_STATUSES,
    ExperimentStatus,
)


TERMINAL_STATUSES = frozenset(status.value for status in API_TERMINAL_STATUSES)
POLLING_STATUSES = frozenset(
    status.value for status in ExperimentStatus if status not in API_TERMINAL_STATUSES
)

_STATUS_LABELS = {
    "CREATED": "실행 대기",
    "RUNNING": "에이전트 실행 중",
    "EVALUATING": "지표와 유의성 평가 중",
    "PASSED": "판정 통과, 승격 대기",
    "FAILED": "실험 가설 미통과",
    "ERROR": "실행 또는 인프라 오류",
    "PROMOTED": "prod 승격 완료",
}

_STEP_KIND_LABELS = {
    "FEATURE_ASSEMBLY": "피처 조립",
    "FEATURE_DERIVE": "파생 피처 생성",
    "TRAIN": "학습",
    "EVALUATE": "평가",
    "OTHER": "기타",
}

_STEP_STATUS_COLORS = {
    "STARTED": "#6B7280",
    "PROGRESS": "#2563EB",
    "COMPLETED": "#16724A",
    "FAILED": "#B42318",
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


# 사전등록 제출 폼이 API에 보내는 값. 왼쪽이 화면 문구, 오른쪽이 계약 값이다.
METRIC_DIRECTIONS = {
    "높을수록 좋음": "higher_is_better",
    "낮을수록 좋음": "lower_is_better",
}
NOT_APPLICABLE = "not_applicable"
NONE_VALUE = "없음"
SCOPE_CHOICES = {
    "prod_model_contract": "prod 모델 계약(src/features/model_contract.py) 수정 허용",
    "feast_definition": "Feast 정의(feature_repo/) 수정 허용",
    "promotion": "champion 승격까지 검토",
}


@dataclass(frozen=True)
class Submission:
    """제출 폼이 모은 사전등록 값.

    `allowed_scope`만 API 요청의 최상위로 가고 나머지는 `fields`로 들어간다 — 서버의
    `IssuePublicationRequest`가 그 형태를 요구한다.
    """

    title: str
    hypothesis: str
    related_work: str
    change: str
    primary_metric_name: str
    primary_metric_direction: str
    minimum_primary_delta: str
    guardrail_metric_name: str
    guardrail_metric_direction: str
    maximum_guardrail_regression: str
    secondary_metrics: str
    allowed_scope: tuple[str, ...]

    def missing_required(self) -> list[str]:
        """비어 있으면 안 되는 항목의 화면 이름을 반환한다.

        형식 검증은 서버가 하지만, 빈 칸은 왕복 없이 화면에서 바로 알려준다.
        """
        required = {
            "실험 제목": self.title,
            "연구 가설": self.hypothesis,
            "변경할 피처 · 모델": self.change,
            "주 지표 이름": self.primary_metric_name,
            "최소 개선폭": self.minimum_primary_delta,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        # guardrail은 세 값이 함께 선언돼야 한다. 이름만 채우고 제출하면 서버가 422로
        # 거부하므로, 왕복하지 않고 여기서 잡는다.
        if self.guardrail_metric_name not in ("", NONE_VALUE) and (
            not self.maximum_guardrail_regression.strip()
            or self.maximum_guardrail_regression == NONE_VALUE
        ):
            missing.append("최대 악화폭 (Guardrail 지표를 선언했습니다)")
        return missing

    def to_fields(self) -> dict[str, str]:
        """API `fields`에 실을 값으로 변환한다."""
        return {
            "title": self.title,
            "hypothesis": self.hypothesis,
            "change": self.change,
            "primary_metric_name": self.primary_metric_name,
            "primary_metric_direction": self.primary_metric_direction,
            "minimum_primary_delta": self.minimum_primary_delta,
            "guardrail_metric_name": self.guardrail_metric_name,
            "guardrail_metric_direction": self.guardrail_metric_direction,
            "maximum_guardrail_regression": self.maximum_guardrail_regression,
            "secondary_metrics": self.secondary_metrics,
            "related_work": self.related_work,
        }


@dataclass(frozen=True)
class IssuePublication:
    """발행된 `[AR]` 이슈의 좌표."""

    issue_number: int
    issue_url: str
    issue_branch: str

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "IssuePublication":
        number = payload.get("issue_number")
        url = payload.get("issue_url")
        branch = payload.get("issue_branch")
        if not isinstance(number, int) or not isinstance(url, str) or not isinstance(branch, str):
            raise ValueError("Experiment API returned an invalid issue publication response.")
        return cls(issue_number=number, issue_url=url, issue_branch=branch)


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


@dataclass(frozen=True)
class Step:
    """에이전트 작업 단계 화면 모델."""

    id: str
    experiment_id: str
    step_kind: str
    step_type: str
    status: str
    message: str | None
    target: dict[str, object] | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "Step":
        """Step API JSON을 검증해 모델로 변환한다."""
        return cls(
            id=str(payload["id"]),
            experiment_id=str(payload["experiment_id"]),
            step_kind=str(payload["step_kind"]),
            step_type=str(payload["step_type"]),
            status=str(payload["status"]),
            message=(
                str(payload["message"]) if payload.get("message") is not None else None
            ),
            target=_mapping(payload.get("target"), "target"),
            created_at=_timestamp(payload["created_at"]),
            updated_at=_timestamp(payload["updated_at"]),
        )

    @property
    def display_line(self) -> str:
        """한 줄 표시 문구.

        `message`는 선택이고 PATCH 전체 교체로 `null`이 될 수 있으므로, 없으면
        `step_kind`·`step_type` 라벨로 대신한다 — 표시가 비는 경우는 없다.
        """
        if self.message:
            return self.message
        return f"{step_kind_label(self.step_kind)} · {self.step_type}"


def step_kind_label(step_kind: str) -> str:
    """Step 대분류를 사람이 읽을 수 있는 한국어 문구로 반환한다."""
    return _STEP_KIND_LABELS.get(step_kind, step_kind)


def step_status_color(step_status: str) -> str:
    """Step 진행 상태를 배지 색상으로 반환한다."""
    return _STEP_STATUS_COLORS.get(step_status, "#334155")


def status_label(status: str) -> str:
    """상태 코드를 사람이 읽을 수 있는 한국어 문구로 반환한다."""
    return _STATUS_LABELS.get(status, status)


def status_color(status: str) -> str:
    """상태 코드를 배지 색상으로 반환한다."""
    return _STATUS_COLORS.get(status, "#334155")
