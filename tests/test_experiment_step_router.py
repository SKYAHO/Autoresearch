"""실험 Step endpoint의 HTTP·인증·OpenAPI 계약을 검증한다.

전체 파이프라인에서 Agent 실행기가 작업 단계를 기록하는 HTTP 경계를 검증한다. 멱등성
transaction 자체는 tests/test_experiment_step_service.py가, PostgreSQL 동시성은
tests/test_experiment_postgres.py가 담당한다.
"""

from __future__ import annotations

from collections.abc import Iterator
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from agent_orchestration.app import main as main_module
from agent_orchestration.app.config import ServiceSettings
from agent_orchestration.app.database import Base
from agent_orchestration.app.experiments.schemas import MAX_STEP_TARGET_BYTES


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
    )
    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(main_module, "ensure_schema", lambda *_args: None)
    monkeypatch.setattr(main_module, "create_database_engine", lambda *_args: engine)

    app = main_module.create_app()
    with TestClient(app) as client:
        yield client
    Base.metadata.drop_all(engine)
    engine.dispose()


def _create_experiment(client: TestClient) -> str:
    response = client.post(
        "/experiments",
        json={"hypothesis": "새 파생 피처가 CTR을 높인다"},
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 201
    return response.json()["id"]


def _step_payload(**overrides: object) -> dict:
    payload: dict = {
        "idempotency_key": "step-1",
        "step_kind": "FEATURE_ASSEMBLY",
        "step_type": "assemble_training_dataset",
    }
    payload.update(overrides)
    return payload


def test_post_step_returns_201_and_hides_fingerprint(experiment_client: TestClient) -> None:
    """생성 성공은 201이며 내부 fingerprint는 응답에 노출되지 않는다."""
    experiment_id = _create_experiment(experiment_client)

    response = experiment_client.post(
        f"/experiments/{experiment_id}/steps",
        json=_step_payload(message="피처 조립 중", target={"features": ["a"]}),
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "STARTED"
    assert body["step_kind"] == "FEATURE_ASSEMBLY"
    assert body["target"] == {"features": ["a"]}
    assert "request_fingerprint" not in body


def test_post_step_requires_token(experiment_client: TestClient) -> None:
    """토큰이 없으면 401이다."""
    experiment_id = _create_experiment(experiment_client)

    response = experiment_client.post(
        f"/experiments/{experiment_id}/steps", json=_step_payload()
    )

    assert response.status_code == 401


def test_post_step_for_missing_experiment_returns_404(experiment_client: TestClient) -> None:
    """없는 실험이면 404다."""
    response = experiment_client.post(
        f"/experiments/{uuid.uuid4()}/steps",
        json=_step_payload(),
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 404


def test_post_step_same_key_different_payload_returns_409(
    experiment_client: TestClient,
) -> None:
    """같은 key·다른 payload는 409다."""
    experiment_id = _create_experiment(experiment_client)
    experiment_client.post(
        f"/experiments/{experiment_id}/steps",
        json=_step_payload(),
        headers=AUTH_HEADERS,
    )

    response = experiment_client.post(
        f"/experiments/{experiment_id}/steps",
        json=_step_payload(step_type="train_candidate"),
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 409


def test_post_step_rejects_unknown_step_kind(experiment_client: TestClient) -> None:
    """서버가 모르는 step_kind는 422다 — 프론트 렌더 경로가 열린 집합이 되지 않게 한다."""
    experiment_id = _create_experiment(experiment_client)

    response = experiment_client.post(
        f"/experiments/{experiment_id}/steps",
        json=_step_payload(step_kind="DEPLOY"),
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 422


def test_post_step_rejects_oversized_target(experiment_client: TestClient) -> None:
    """4096 byte를 넘는 target은 422다."""
    experiment_id = _create_experiment(experiment_client)

    response = experiment_client.post(
        f"/experiments/{experiment_id}/steps",
        json=_step_payload(target={"blob": "x" * (MAX_STEP_TARGET_BYTES + 1)}),
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 422


def test_post_step_rejects_extra_fields(experiment_client: TestClient) -> None:
    """계약에 없는 필드는 422다."""
    experiment_id = _create_experiment(experiment_client)

    response = experiment_client.post(
        f"/experiments/{experiment_id}/steps",
        json=_step_payload(unexpected="value"),
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 422


def test_openapi_declares_step_endpoint_responses(experiment_client: TestClient) -> None:
    """Swagger가 생성 endpoint의 성공·인증·not-found·conflict 응답을 노출한다."""
    schema = experiment_client.get("/openapi.json").json()
    operation = schema["paths"]["/experiments/{experiment_id}/steps"]["post"]

    assert set(operation["responses"]) >= {"201", "401", "404", "409", "422"}
