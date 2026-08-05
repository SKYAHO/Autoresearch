"""Streamlit의 Experiment API 서버 측 HTTP client를 제공한다.

[파이프라인]
Streamlit workbench가 FastAPI Experiment API에 접근하는 경계다. API 토큰은 이 모듈이
서버 측 HTTP header에만 넣으며, browser session으로 전달하지 않는다.

[기능]
Experiment 생성·조회, 사전등록 필드의 `[AR]` 이슈 발행 요청, Event/Log cursor 조회,
metadata 조회와 API 오류의 안전한 분류를 제공한다.

[비책임]
Streamlit 화면 렌더링, session state, Agent 실행, 상태·Event·Log 쓰기. 이슈 본문 조립과
`gh` 호출은 API 서버가 한다 — 이 모듈은 발행을 **요청**할 뿐이다.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from typing import Any, Callable, TypeVar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agent_orchestration.ui.models import (
    Event,
    Experiment,
    IssuePublication,
    Log,
    Step,
)


ParsedModel = TypeVar("ParsedModel")

# 서버 `GET /steps`의 limit 상한과 같은 값이다. 이보다 크게 보내면 422다.
STEP_PAGE_SIZE = 100
# 한 갱신에서 이어 받을 최대 페이지 수. 폭주한 실험이 1초 polling을 무한 요청으로 바꾸지
# 않도록 두는 상한이며, 여기에 걸리면 가장 오래된 STEP_PAGE_SIZE * STEP_PAGE_BUDGET개까지만
# 표시된다.
STEP_PAGE_BUDGET = 20


class ExperimentApiError(RuntimeError):
    """Experiment API 호출 실패의 공통 기반 예외."""


class ApiConfigurationError(ExperimentApiError):
    """Streamlit 서버의 Experiment API 연결 설정 오류."""


class ApiUnauthorizedError(ExperimentApiError):
    """API 토큰 오류."""


class ApiNotFoundError(ExperimentApiError):
    """선택한 Experiment를 찾을 수 없음."""


class ApiValidationError(ExperimentApiError):
    """API 요청 검증 실패."""


class ApiUnavailableError(ExperimentApiError):
    """네트워크 또는 API 서버 오류."""


class ExperimentClient:
    """Experiment API v0의 Streamlit 전용 client."""

    def __init__(self, base_url: str, api_token: str, timeout_sec: float = 10.0) -> None:
        normalized_url = base_url.strip().rstrip("/")
        if not normalized_url:
            raise ApiConfigurationError("ORCH_UI_API_BASE_URL을 설정해 주세요.")
        if not api_token.strip():
            raise ApiConfigurationError("ORCH_UI_API_TOKEN을 설정해 주세요.")
        self._base_url = normalized_url
        self._api_token = api_token.strip()
        self._timeout_sec = timeout_sec

    @classmethod
    def from_environment(cls) -> "ExperimentClient":
        """UI 환경 변수에서 client 설정을 읽는다."""
        return cls(
            os.getenv("ORCH_UI_API_BASE_URL", "http://127.0.0.1:8000"),
            os.getenv("ORCH_UI_API_TOKEN", ""),
        )

    def create_experiment(self, hypothesis: str) -> Experiment:
        """가설 한 줄로 v0 Experiment를 생성한다."""
        stripped = hypothesis.strip()
        if not stripped:
            raise ApiValidationError("가설을 입력해 주세요.")
        payload = self._request_json(
            "POST",
            "/experiments",
            {"hypothesis": stripped, "metadata": {}},
        )
        return self._parse_model(payload, Experiment.from_json)

    def publish_issue(
        self,
        experiment_id: str,
        fields: dict[str, str],
        allowed_scope: Sequence[str],
    ) -> IssuePublication:
        """사전등록 필드를 `[AR]` 이슈로 발행하고 그 좌표를 받는다.

        서버가 지표 방향·소수 형식·guardrail 동반 선언을 검증하므로 여기서 다시 검사하지
        않는다. 위반은 422로 돌아오며 그 시점에 이슈는 아직 열리지 않았다.
        """
        payload = self._request_json(
            "POST",
            f"/experiments/{experiment_id}/issue",
            {"fields": fields, "allowed_scope": list(allowed_scope)},
        )
        return self._parse_model(payload, IssuePublication.from_json)

    def list_experiments(self, *, limit: int = 50) -> list[Experiment]:
        """최근 Experiment 목록을 반환한다."""
        payload = self._request_json("GET", f"/experiments?{urlencode({'limit': limit, 'offset': 0})}")
        page = self._object(payload)
        items = page.get("items")
        if not isinstance(items, list):
            raise ApiUnavailableError("Experiment API returned an invalid list response.")
        return [self._parse_model(item, Experiment.from_json) for item in items]

    def get_experiment(self, experiment_id: str) -> Experiment:
        """선택 Experiment의 최신 상태를 조회한다."""
        return self._parse_model(
            self._request_json("GET", f"/experiments/{experiment_id}"),
            Experiment.from_json,
        )

    def get_events(
        self,
        experiment_id: str,
        after_id: str | None,
    ) -> tuple[list[Event], str | None]:
        """cursor 이후 Event와 다음 cursor를 조회한다."""
        path = self._cursor_path(f"/experiments/{experiment_id}/events", after_id)
        page = self._object(self._request_json("GET", path))
        items = page.get("items")
        if not isinstance(items, list):
            raise ApiUnavailableError("Experiment API returned an invalid event response.")
        cursor = page.get("next_cursor")
        return (
            [self._parse_model(item, Event.from_json) for item in items],
            str(cursor) if cursor is not None else None,
        )

    def get_logs(
        self,
        experiment_id: str,
        after_id: str | None,
    ) -> tuple[list[Log], str | None]:
        """cursor 이후 Log와 다음 cursor를 조회한다."""
        path = self._cursor_path(f"/experiments/{experiment_id}/logs", after_id)
        page = self._object(self._request_json("GET", path))
        items = page.get("items")
        if not isinstance(items, list):
            raise ApiUnavailableError("Experiment API returned an invalid log response.")
        cursor = page.get("next_cursor")
        return (
            [self._parse_model(item, Log.from_json) for item in items],
            str(cursor) if cursor is not None else None,
        )

    def get_steps(self, experiment_id: str) -> tuple[list[Step], bool]:
        """실험의 Step 전체와 예산 초과 여부를 조회한다.

        **cursor를 한 번의 갱신 안에서만 쓴다.** Step은 PATCH로 갱신되는 mutable 리소스라,
        cursor를 갱신과 갱신 사이에 들고 가면 이미 받은 Step의 상태 변화를 영원히 관측하지
        못한다. 그래서 매 갱신은 항상 처음부터 다시 읽되, 한 번에 `limit` 상한만 오므로
        cursor로 나머지 페이지를 이어 받는다 — 그러지 않으면 화면이 **가장 오래된 100개에
        고정**되어 최신 진행 상황이 보이지 않는다.

        두 번째 반환값은 `STEP_PAGE_BUDGET`에 걸려 **뒷부분을 못 읽었는지**를 알린다.
        호출자는 이를 화면에 드러내야 한다 — 조용히 버리면 상한값만 커진 채 같은 문제가
        남는다.
        """
        steps: list[Step] = []
        after_id: str | None = None
        for _page in range(STEP_PAGE_BUDGET):
            page_steps, next_cursor = self._get_step_page(experiment_id, after_id)
            steps.extend(page_steps)
            if len(page_steps) < STEP_PAGE_SIZE or next_cursor is None or next_cursor == after_id:
                return steps, False
            after_id = next_cursor
        return steps, True

    def _get_step_page(
        self,
        experiment_id: str,
        after_id: str | None,
    ) -> tuple[list[Step], str | None]:
        """Step 한 페이지와 다음 cursor를 조회한다."""
        query: dict[str, object] = {"limit": STEP_PAGE_SIZE}
        if after_id is not None:
            query["after_id"] = after_id
        path = f"/experiments/{experiment_id}/steps?{urlencode(query)}"
        page = self._object(self._request_json("GET", path))
        items = page.get("items")
        if not isinstance(items, list):
            raise ApiUnavailableError("Experiment API returned an invalid step response.")
        cursor = page.get("next_cursor")
        return (
            [self._parse_model(item, Step.from_json) for item in items],
            str(cursor) if cursor is not None else None,
        )

    def get_metadata(self, experiment_id: str) -> dict[str, str]:
        """선택 Experiment metadata를 조회한다."""
        payload = self._object(
            self._request_json("GET", f"/experiments/{experiment_id}/metadata")
        )
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            raise ApiUnavailableError("Experiment API returned invalid metadata.")
        return {str(key): str(value) for key, value in entries.items()}

    def _cursor_path(self, base_path: str, after_id: str | None) -> str:
        if after_id is None:
            return base_path
        return f"{base_path}?{urlencode({'after_id': after_id})}"

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> Any:
        """인증된 HTTP 요청을 보내고 JSON body를 반환한다."""
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Accept": "application/json",
            "X-Orch-Token": self._api_token,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self._base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self._timeout_sec) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            self._raise_http_error(error)
        except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiUnavailableError("Experiment API에 연결할 수 없습니다.") from error
        raise AssertionError("HTTP error handling must raise.")

    def _raise_http_error(self, error: HTTPError) -> None:
        if error.code == 401:
            raise ApiUnauthorizedError("Experiment API 토큰이 없거나 올바르지 않습니다.") from error
        if error.code == 404:
            raise ApiNotFoundError("선택한 실험을 찾을 수 없습니다.") from error
        if error.code == 422:
            raise ApiValidationError("Experiment API 요청 형식이 올바르지 않습니다.") from error
        raise ApiUnavailableError("Experiment API가 일시적으로 응답하지 않습니다.") from error

    @staticmethod
    def _object(value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ApiUnavailableError("Experiment API returned an invalid JSON response.")
        return value

    @classmethod
    def _parse_model(
        cls,
        value: object,
        parser: Callable[[dict[str, Any]], ParsedModel],
    ) -> ParsedModel:
        """API 모델 필드 검증 오류를 UI용 API 오류로 정규화한다."""
        try:
            return parser(cls._object(value))
        except (KeyError, TypeError, ValueError) as error:
            raise ApiUnavailableError("Experiment API 응답 형식이 올바르지 않습니다.") from error
