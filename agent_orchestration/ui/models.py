"""Experiment API 응답을 Streamlit 표시 모델로 변환한다.

[파이프라인]
Experiment API와 Streamlit 화면 사이의 읽기 경계에서 JSON 응답을 안정적인 화면 모델로
정규화한다. HTTP 호출과 Streamlit session state는 담당하지 않는다.

[기능]
Experiment, Event, Log 불변 모델과 ISO timestamp 변환, 상태별 사용자 문구와 색상을
제공한다. 사전등록 제출 폼이 API에 보내는 값(`Submission`)도 여기서 정의한다.

[비책임]
API 인증, cursor polling, UI 컴포넌트 렌더링, Agent 상태 기록.

UI 이미지는 `issue_authoring.py`를 포함하지 않으므로(`deploy/agent_orchestration/
ui.Dockerfile`) 서버 모델을 import하지 않고 보낼 값을 여기서 조립한다. `to_fields()`가
내는 것은 표시 문구가 아니라 **HTTP 계약**이며, 서버가 받아들이는지는
`tests/test_ui_submission_form.py`가 고정한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any

from agent_orchestration.app.experiments.models import (
    TERMINAL_STATUSES as API_TERMINAL_STATUSES,
    ExperimentStatus,
)


TERMINAL_STATUSES = frozenset(status.value for status in API_TERMINAL_STATUSES)
POLLING_STATUSES = frozenset(
    status.value for status in ExperimentStatus if status not in API_TERMINAL_STATUSES
)
# 리포트 본문을 가질 수 있는 상태다. `record_experiment_result`가 `report_markdown`의
# 유일한 기록자이고 도달 상태로 `PASSED`를 하드코딩하며,
# `ALLOWED_TRANSITIONS[PASSED] = {PROMOTED}`라 PASSED에서 FAILED로 가는 간선이 없다.
# 그 두 사실이 이 집합의 근거이므로, 전이 그래프가 바뀌면 여기도 함께 넓힌다.
REPORT_STATUSES = frozenset(
    {ExperimentStatus.PASSED.value, ExperimentStatus.PROMOTED.value}
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

# 사이드바 목록이 쓰는 Streamlit 마크다운 색 이름. 위젯 라벨에는 HTML을 넣을 수
# 없어 `:green[...]` 문법으로만 색을 줄 수 있으므로 `_STATUS_COLORS`의 hex와 별도로
# 둔다. 두 표는 같은 의미를 가리키므로 상태를 추가하면 함께 넓힌다.
_STATUS_TONES = {
    "CREATED": "gray",
    "RUNNING": "blue",
    "EVALUATING": "orange",
    "PASSED": "green",
    "FAILED": "red",
    "ERROR": "red",
    "PROMOTED": "violet",
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


# 지표 방향·guardrail sentinel·허용 범위 선택지는 #570에서 화면에서 사라졌다. UI가
# 그 값을 보내지 않으므로 여기에 복제해 둘 이유도 없어졌다 — 서버의 `IssueSubmission`이
# 기본값을 소유한다. 화면에 다시 노출할 때 서버 상수와 함께 되살린다.

# `issue_authoring._HEADING_LINE_PATTERN`과 같은 규칙이다. 서버가 거부할 값을 첫 요청
# 전에 알려주기 위한 복제이며, 판정 자체는 여전히 서버가 소유한다.
_H3_LINE_PATTERN = re.compile(r"^### ", re.MULTILINE)


@dataclass(frozen=True)
class Submission:
    """제출 폼이 모은 사전등록 값.

    값은 `fields`로 들어간다 — 서버의 `IssuePublicationRequest`가 그 형태를 요구한다.

    #570에서 지표·변경 내용·허용 범위 입력칸을 없애고 `hypothesis` 마크다운 한
    덩어리로 합쳤다. 서버가 선택으로 받는 값은 여기서 보내지 않는다 —
    `IssueSubmission`이 `extra="forbid"`라 보내지 않은 값은 기본값이 된다.
    """

    title: str
    hypothesis: str

    def missing_required(self) -> list[str]:
        """비어 있으면 안 되는 항목의 화면 이름을 반환한다.

        형식 검증은 서버가 하지만, 빈 칸은 왕복 없이 화면에서 바로 알려준다.
        """
        required = {
            "실험 제목": self.title,
            "가설": self.hypothesis,
        }
        return [name for name, value in required.items() if not value.strip()]

    def blocking_problems(self) -> list[str]:
        """제출을 막아야 하는 이유를 사람이 읽는 문장으로 반환한다.

        `### `를 여기서 잡는 이유는 실패 시점 때문이다. 제출은 Experiment 생성과 이슈
        발행 두 번의 요청이고, 이 값은 **생성이 끝난 뒤** 발행에서 422가 된다. #572가
        재시도 화면을 추가해 그 상태에서 빠져나올 수는 있지만, 본문을 고치기 전에는
        재시도해도 같은 422다. 마크다운을 자유롭게 쓰게 한 이상 `### `는 드문 입력이
        아니므로, 실패가 확실한 값은 첫 요청을 보내기 전에 끊는다.
        """
        problems = [f"{name}을(를) 채워 주세요." for name in self.missing_required()]
        if _H3_LINE_PATTERN.search(self.hypothesis):
            problems.append(
                "가설 본문에 `### `로 시작하는 줄이 있습니다. 이슈 본문에서 항목을 "
                "나누는 표시와 겹쳐 발행할 수 없습니다. `#`, `##`, `####`는 그대로 "
                "쓸 수 있습니다."
            )
        return problems

    def to_fields(self) -> dict[str, str]:
        """API `fields`에 실을 값으로 변환한다."""
        return {
            "title": self.title,
            "hypothesis": self.hypothesis,
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


def status_tone(status: str) -> str:
    """상태 코드를 Streamlit 마크다운 색 이름으로 반환한다."""
    return _STATUS_TONES.get(status, "gray")
