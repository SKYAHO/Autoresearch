"""실험 워크벤치 FastAPI Router의 HTTP·인증·OpenAPI 계약을 검증한다.

전체 파이프라인에서 Streamlit과 Agent가 호출하는 실험 HTTP 경계를 검증한다. ORM
transaction과 상태 전이 자체는 service 단위 테스트가 담당하며, 이 모듈은 실제 FastAPI
dependency·예외 변환·Swagger 노출을 테스트한다.
"""

from __future__ import annotations

from collections.abc import Iterator
import uuid

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from agent_orchestration.app import main as main_module
from agent_orchestration.app.config import ServiceSettings
from agent_orchestration.app.database import Base, get_db_session
from agent_orchestration.app.experiments.issue_authoring import ExperimentDefaults


API_TOKEN = "test-orchestration-token"
AUTH_HEADERS = {"X-Orch-Token": API_TOKEN}


@pytest.fixture
def experiment_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """chat 초기화는 mock하고 실험 endpoint는 SQLite Session으로 실행한다."""
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def register_uuid_function(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)

    Base.metadata.create_all(engine)
    settings = ServiceSettings(
        openai_api_key=None,
        openai_model="gpt-5.3-codex-spark",
        openai_max_tokens=1024,
        openai_timeout_sec=60,
        database_url="postgresql://orch:pw@localhost:5432/orch",
        interactions_table="chat_interactions",
        api_token=API_TOKEN,
        github_token="x" * 40,
        github_repository="SKYAHO/Autoresearch",
        gh_timeout_sec=30,
        issue_daily_limit=20,
        experiment_defaults=ExperimentDefaults(
            dataset_source="feast://feast_offline_store/ctr_training_v1",
            training_config_ref="configs/train/x.yaml@abc",
        ),
    )
    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(main_module, "ensure_schema", lambda *_args: None)
    monkeypatch.setattr(main_module, "create_database_engine", lambda *_args: engine)

    app = main_module.create_app()
    with TestClient(app) as client:
        yield client
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_get_db_session_returns_503_when_factory_not_ready() -> None:
    """settings는 채워졌지만 experiment_session_factory가 아직 없는 startup 창에서
    AttributeError로 인한 500 대신 503을 낸다."""

    class _StubState:
        pass

    class _StubApp:
        state = _StubState()

    class _StubRequest:
        app = _StubApp()

    generator = get_db_session(_StubRequest())  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as exc_info:
        next(generator)

    assert exc_info.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


def test_experiment_router_supports_full_workbench_lifecycle(
    experiment_client: TestClient,
) -> None:
    """10개 endpoint가 생성·polling·수동 승격까지 승인된 HTTP 계약을 지킨다."""
    created = experiment_client.post(
        "/experiments",
        headers=AUTH_HEADERS,
        json={
            "hypothesis": "ranking feature improves CTR",
            "agent_session_id": "agent-448",
            "metadata": {"branch": "feat/448"},
        },
    )
    assert created.status_code == status.HTTP_201_CREATED
    experiment_id = created.json()["id"]

    listed = experiment_client.get("/experiments?limit=1&offset=0", headers=AUTH_HEADERS)
    detail = experiment_client.get(f"/experiments/{experiment_id}", headers=AUTH_HEADERS)
    assert listed.status_code == status.HTTP_200_OK
    assert listed.json()["total"] == 1
    assert detail.status_code == status.HTTP_200_OK

    running = experiment_client.patch(
        f"/experiments/{experiment_id}/status",
        headers=AUTH_HEADERS,
        json={"status": "RUNNING", "reason": "runner accepted work"},
    )
    evaluating = experiment_client.post(
        f"/experiments/{experiment_id}/events",
        headers=AUTH_HEADERS,
        json={"idempotency_key": "event-evaluating", "to_status": "EVALUATING"},
    )
    events = experiment_client.get(
        f"/experiments/{experiment_id}/events?limit=100",
        headers=AUTH_HEADERS,
    )
    assert running.status_code == status.HTTP_200_OK
    assert evaluating.status_code == status.HTTP_201_CREATED
    assert events.status_code == status.HTTP_200_OK
    assert len(events.json()["items"]) == 3

    appended_log = experiment_client.post(
        f"/experiments/{experiment_id}/logs",
        headers=AUTH_HEADERS,
        json={"idempotency_key": "log-0001", "log_type": "stdout", "content": "epoch=1"},
    )
    logs = experiment_client.get(
        f"/experiments/{experiment_id}/logs?limit=100",
        headers=AUTH_HEADERS,
    )
    metadata = experiment_client.get(f"/experiments/{experiment_id}/metadata", headers=AUTH_HEADERS)
    assert appended_log.status_code == status.HTTP_201_CREATED
    assert logs.status_code == status.HTTP_200_OK
    assert logs.json()["items"][0]["idempotency_key"] == "log-0001"
    assert metadata.json() == {"entries": {"branch": "feat/448"}}

    passed = experiment_client.post(
        f"/experiments/{experiment_id}/events",
        headers=AUTH_HEADERS,
        json={"idempotency_key": "event-passed", "to_status": "PASSED"},
    )
    promoted = experiment_client.post(
        f"/experiments/{experiment_id}/promote",
        headers=AUTH_HEADERS,
        json={
            "idempotency_key": "promote-0001",
            "reason": "PR merged and deployment verified",
            "deployment_metadata": {"pr": 461},
        },
    )
    assert passed.status_code == status.HTTP_201_CREATED
    assert promoted.status_code == status.HTTP_200_OK
    assert promoted.json()["status"] == "PROMOTED"


def test_experiment_router_maps_auth_not_found_conflict_and_validation_errors(
    experiment_client: TestClient,
) -> None:
    """공개 오류 계약이 401·404·409·422와 안전한 detail을 반환한다."""
    unauthenticated = experiment_client.get("/experiments")
    unknown = experiment_client.get(
        "/experiments/00000000-0000-0000-0000-000000000448",
        headers=AUTH_HEADERS,
    )
    invalid_body = experiment_client.post(
        "/experiments",
        headers=AUTH_HEADERS,
        json={"hypothesis": "valid", "unknown_field": True},
    )
    created = experiment_client.post(
        "/experiments",
        headers=AUTH_HEADERS,
        json={"hypothesis": "invalid transition"},
    )
    experiment_id = created.json()["id"]
    conflict = experiment_client.patch(
        f"/experiments/{experiment_id}/status",
        headers=AUTH_HEADERS,
        json={"status": "PASSED"},
    )
    first_event = experiment_client.post(
        f"/experiments/{experiment_id}/events",
        headers=AUTH_HEADERS,
        json={"idempotency_key": "conflict-key", "to_status": "RUNNING"},
    )
    idempotency_conflict = experiment_client.post(
        f"/experiments/{experiment_id}/events",
        headers=AUTH_HEADERS,
        json={"idempotency_key": "conflict-key", "to_status": "ERROR"},
    )

    assert unauthenticated.status_code == status.HTTP_401_UNAUTHORIZED
    assert unauthenticated.json() == {"detail": "Invalid orchestration API token."}
    assert unknown.status_code == status.HTTP_404_NOT_FOUND
    assert invalid_body.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert conflict.status_code == status.HTTP_409_CONFLICT
    assert "Invalid transition" in conflict.json()["detail"]
    assert first_event.status_code == status.HTTP_201_CREATED
    assert idempotency_conflict.status_code == status.HTTP_409_CONFLICT


def test_experiment_router_enforces_polling_limit_bounds(
    experiment_client: TestClient,
) -> None:
    """Event limit은 1~200, Log limit은 1~100으로 실제 HTTP 계약에서 거부된다."""
    created = experiment_client.post(
        "/experiments",
        headers=AUTH_HEADERS,
        json={"hypothesis": "limit bounds"},
    )
    experiment_id = created.json()["id"]

    events_limit_zero = experiment_client.get(
        f"/experiments/{experiment_id}/events?limit=0", headers=AUTH_HEADERS
    )
    events_limit_201 = experiment_client.get(
        f"/experiments/{experiment_id}/events?limit=201", headers=AUTH_HEADERS
    )
    events_limit_200 = experiment_client.get(
        f"/experiments/{experiment_id}/events?limit=200", headers=AUTH_HEADERS
    )
    logs_limit_zero = experiment_client.get(
        f"/experiments/{experiment_id}/logs?limit=0", headers=AUTH_HEADERS
    )
    logs_limit_101 = experiment_client.get(
        f"/experiments/{experiment_id}/logs?limit=101", headers=AUTH_HEADERS
    )
    logs_limit_100 = experiment_client.get(
        f"/experiments/{experiment_id}/logs?limit=100", headers=AUTH_HEADERS
    )

    assert events_limit_zero.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert events_limit_201.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert events_limit_200.status_code == status.HTTP_200_OK
    assert logs_limit_zero.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert logs_limit_101.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert logs_limit_100.status_code == status.HTTP_200_OK


def test_experiment_router_rejects_cursor_that_does_not_exist(
    experiment_client: TestClient,
) -> None:
    """존재하지 않는 after_id는 빈 목록이 아니라 404로 명시적으로 실패한다."""
    created = experiment_client.post(
        "/experiments",
        headers=AUTH_HEADERS,
        json={"hypothesis": "stale cursor over http"},
    )
    experiment_id = created.json()["id"]
    stale_cursor = str(uuid.uuid4())

    events = experiment_client.get(
        f"/experiments/{experiment_id}/events?after_id={stale_cursor}", headers=AUTH_HEADERS
    )
    logs = experiment_client.get(
        f"/experiments/{experiment_id}/logs?after_id={stale_cursor}", headers=AUTH_HEADERS
    )

    assert events.status_code == status.HTTP_404_NOT_FOUND
    assert logs.status_code == status.HTTP_404_NOT_FOUND


def test_experiment_router_openapi_documents_all_endpoints_and_errors() -> None:
    """Swagger가 인증 헤더와 각 endpoint의 도메인 오류를 숨기지 않는다."""
    schema = main_module.create_app().openapi()
    expected_operations = {
        "/experiments": {"get", "post"},
        "/experiments/{experiment_id}": {"get"},
        "/experiments/{experiment_id}/status": {"patch"},
        "/experiments/{experiment_id}/events": {"get", "post"},
        "/experiments/{experiment_id}/logs": {"get", "post"},
        "/experiments/{experiment_id}/metadata": {"get"},
        "/experiments/{experiment_id}/promote": {"post"},
    }

    for path, methods in expected_operations.items():
        assert methods <= set(schema["paths"][path])
        for method in methods:
            operation = schema["paths"][path][method]
            token_parameters = [
                parameter
                for parameter in operation["parameters"]
                if parameter["name"] == "X-Orch-Token" and parameter["in"] == "header"
            ]
            assert token_parameters == [
                {
                    "name": "X-Orch-Token",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string"},
                    "description": "공유 오케스트레이션 API 토큰",
                }
            ]
            assert "401" in operation["responses"]
            assert "422" in operation["responses"]

    assert "404" in schema["paths"]["/experiments/{experiment_id}"]["get"]["responses"]
    assert "409" in schema["paths"]["/experiments/{experiment_id}/status"]["patch"]["responses"]
    assert "409" in schema["paths"]["/experiments/{experiment_id}/promote"]["post"]["responses"]
