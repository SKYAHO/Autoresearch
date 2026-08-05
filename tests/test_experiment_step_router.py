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
from agent_orchestration.app.experiments.issue_authoring import (
    ExperimentDefaults,
)
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


def _create_step(client: TestClient, experiment_id: str) -> str:
    response = client.post(
        f"/experiments/{experiment_id}/steps",
        json=_step_payload(message="조립 중", target={"features": ["a"]}),
        headers=AUTH_HEADERS,
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_patch_step_replaces_omitted_fields_with_null(experiment_client: TestClient) -> None:
    """PATCH 전체 교체 — 생략된 message/target은 null이 된다."""
    experiment_id = _create_experiment(experiment_client)
    step_id = _create_step(experiment_client, experiment_id)

    response = experiment_client.patch(
        f"/experiments/{experiment_id}/steps/{step_id}",
        json={"status": "PROGRESS"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PROGRESS"
    assert body["message"] is None
    assert body["target"] is None


def test_patch_terminal_step_identical_retry_returns_200(
    experiment_client: TestClient,
) -> None:
    """확정된 Step에 같은 payload를 다시 보내면 200이다."""
    experiment_id = _create_experiment(experiment_client)
    step_id = _create_step(experiment_client, experiment_id)
    payload = {"status": "COMPLETED", "message": "완료"}
    experiment_client.patch(
        f"/experiments/{experiment_id}/steps/{step_id}", json=payload, headers=AUTH_HEADERS
    )

    response = experiment_client.patch(
        f"/experiments/{experiment_id}/steps/{step_id}", json=payload, headers=AUTH_HEADERS
    )

    assert response.status_code == 200
    assert response.json()["status"] == "COMPLETED"


def test_patch_terminal_step_different_payload_returns_409(
    experiment_client: TestClient,
) -> None:
    """확정된 결과를 다른 값으로 덮으려 하면 409다."""
    experiment_id = _create_experiment(experiment_client)
    step_id = _create_step(experiment_client, experiment_id)
    experiment_client.patch(
        f"/experiments/{experiment_id}/steps/{step_id}",
        json={"status": "COMPLETED", "message": "완료"},
        headers=AUTH_HEADERS,
    )

    response = experiment_client.patch(
        f"/experiments/{experiment_id}/steps/{step_id}",
        json={"status": "FAILED", "message": "실패"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 409


def test_patch_missing_step_returns_404(experiment_client: TestClient) -> None:
    """없는 Step은 404다."""
    experiment_id = _create_experiment(experiment_client)

    response = experiment_client.patch(
        f"/experiments/{experiment_id}/steps/{uuid.uuid4()}",
        json={"status": "PROGRESS"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 404


def test_patch_step_rejects_oversized_target(experiment_client: TestClient) -> None:
    """갱신 경로에도 4096 byte 제한이 걸린다."""
    experiment_id = _create_experiment(experiment_client)
    step_id = _create_step(experiment_client, experiment_id)

    response = experiment_client.patch(
        f"/experiments/{experiment_id}/steps/{step_id}",
        json={
            "status": "PROGRESS",
            "target": {"blob": "x" * (MAX_STEP_TARGET_BYTES + 1)},
        },
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 422


def test_openapi_declares_step_patch_responses(experiment_client: TestClient) -> None:
    """Swagger가 갱신 endpoint의 응답을 노출한다."""
    schema = experiment_client.get("/openapi.json").json()
    operation = schema["paths"]["/experiments/{experiment_id}/steps/{step_id}"]["patch"]

    assert set(operation["responses"]) >= {"200", "401", "404", "409", "422"}


def _create_steps(client: TestClient, experiment_id: str, count: int) -> list[str]:
    ids = []
    for index in range(count):
        response = client.post(
            f"/experiments/{experiment_id}/steps",
            json=_step_payload(
                idempotency_key=f"step-{index}",
                step_kind="TRAIN" if index % 2 else "FEATURE_ASSEMBLY",
                step_type=f"stage-{index}",
            ),
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 201
        ids.append(response.json()["id"])
    return ids


def test_get_steps_returns_all_rows_without_cursor(experiment_client: TestClient) -> None:
    """cursor 없이 조회하면 그 실험의 Step을 모두 돌려주고 next_cursor를 채운다.

    **순서와 cursor 전진은 여기서 검증하지 않는다.** SQLite의 `CURRENT_TIMESTAMP`는 초
    단위이고, SQLAlchemy가 파싱한 datetime을 다시 바인딩하면 `.000000`이 붙어 저장 문자열과
    동등 비교가 성립하지 않는다. 그래서 keyset의 tie-breaker 분기가 SQLite에서는 절대
    매치되지 않는다. cursor 계약은 실제 timestamp를 쓰는
    `tests/test_experiment_postgres.py`가 검증한다.
    """
    experiment_id = _create_experiment(experiment_client)
    step_ids = _create_steps(experiment_client, experiment_id, 3)

    response = experiment_client.get(
        f"/experiments/{experiment_id}/steps", headers=AUTH_HEADERS
    )

    assert response.status_code == 200
    body = response.json()
    assert {item["id"] for item in body["items"]} == set(step_ids)
    assert body["next_cursor"] == body["items"][-1]["id"]


def test_get_steps_filters_by_step_kind(experiment_client: TestClient) -> None:
    """step_kind 필터는 해당 대분류만 돌려준다."""
    experiment_id = _create_experiment(experiment_client)
    _create_steps(experiment_client, experiment_id, 4)

    response = experiment_client.get(
        f"/experiments/{experiment_id}/steps",
        params={"step_kind": "TRAIN"},
        headers=AUTH_HEADERS,
    )

    kinds = {item["step_kind"] for item in response.json()["items"]}
    assert kinds == {"TRAIN"}


def test_get_steps_rejects_unknown_step_kind_filter(experiment_client: TestClient) -> None:
    """서버가 모르는 step_kind 필터는 422다."""
    experiment_id = _create_experiment(experiment_client)

    response = experiment_client.get(
        f"/experiments/{experiment_id}/steps",
        params={"step_kind": "DEPLOY"},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 422


def test_get_steps_unknown_cursor_returns_404(experiment_client: TestClient) -> None:
    """이 실험에 없는 after_id는 404다."""
    experiment_id = _create_experiment(experiment_client)

    response = experiment_client.get(
        f"/experiments/{experiment_id}/steps",
        params={"after_id": str(uuid.uuid4())},
        headers=AUTH_HEADERS,
    )

    assert response.status_code == 404


def test_get_steps_respects_limit(experiment_client: TestClient) -> None:
    """limit을 넘겨 받지 않는다."""
    experiment_id = _create_experiment(experiment_client)
    _create_steps(experiment_client, experiment_id, 3)

    response = experiment_client.get(
        f"/experiments/{experiment_id}/steps",
        params={"limit": 2},
        headers=AUTH_HEADERS,
    )

    assert len(response.json()["items"]) == 2


def test_get_steps_for_missing_experiment_returns_404(experiment_client: TestClient) -> None:
    """없는 실험이면 404다."""
    response = experiment_client.get(
        f"/experiments/{uuid.uuid4()}/steps", headers=AUTH_HEADERS
    )

    assert response.status_code == 404
