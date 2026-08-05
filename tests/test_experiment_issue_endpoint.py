"""발행 endpoint의 HTTP 계약을 고정한다.

전체 파이프라인에서 발행 요청의 인증·상태 코드·응답 형태만 검증한다. 발행 절차 자체는
test_experiment_issue_publication이 담당한다.
"""

from __future__ import annotations

from collections.abc import Iterator
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from agent_orchestration.app import main as main_module
from agent_orchestration.app.config import ServiceSettings
from agent_orchestration.app.database import Base
from agent_orchestration.app.experiments.exceptions import IssuePublicationLimitError
from agent_orchestration.app.experiments.github_issues import IssueRef
from agent_orchestration.app.experiments.issue_authoring import ExperimentDefaults
from agent_orchestration.app.llm import LLMBackendError
from agent_orchestration.contracts import LLMResult

API_TOKEN = "test-orchestration-token"

# test_experiment_issue_publication의 고정값과 동일한 형식 — issue_authoring 파서가
# 요구하는 필드를 모두 채워야 build_issue_body가 실패하지 않는다.
LLM_RESPONSE = json.dumps(
    {
        "title": "views per day ratio feature",
        "hypothesis": "비율 피처가 ROC-AUC를 높인다.",
        "change": "- 추가 피처: views_per_day = views / (days + 1)",
        "primary_metric_name": "roc_auc",
        "primary_metric_direction": "higher_is_better",
        "minimum_primary_delta": "0.002",
        "guardrail_metric_name": "없음",
        "guardrail_metric_direction": "not_applicable",
        "maximum_guardrail_regression": "없음",
        "secondary_metrics": "pr_auc",
    },
    ensure_ascii=False,
)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """chat 초기화·`gh` 호출·LLM 호출을 스텁으로 대체하고 실험 endpoint만 SQLite로 실행한다.

    이 테스트는 발행 endpoint의 HTTP 계약만 검증하므로, 실제 GitHub·LLM 네트워크는
    타지 않는다.
    """
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
        gh_timeout_sec=5,
        issue_daily_limit=20,
        experiment_defaults=ExperimentDefaults(
            dataset_source="feast://feast_offline_store/ctr_training_v1",
            training_config_ref="configs/train/lgbm-v1.yaml@abc1234",
        ),
    )
    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(main_module, "ensure_schema", lambda *_args: None)
    monkeypatch.setattr(main_module, "create_database_engine", lambda *_args: engine)

    async def fake_generate_response(
        _settings: ServiceSettings, _prompt: str
    ) -> LLMResult:
        return LLMResult(text=LLM_RESPONSE, model="stub-llm", token_count=None)

    async def fake_find_issue_by_marker(_settings: object, *, marker: str) -> None:
        return None

    issued_numbers = iter(range(601, 700))

    async def fake_create_issue(
        _settings: object, *, title: str, body: str, labels: tuple[str, ...]
    ) -> IssueRef:
        number = next(issued_numbers)
        return IssueRef(
            number=number,
            url=f"https://github.com/SKYAHO/Autoresearch/issues/{number}",
        )

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.router.generate_response",
        fake_generate_response,
    )
    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.find_issue_by_marker",
        fake_find_issue_by_marker,
    )
    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", fake_create_issue
    )

    app = main_module.create_app()
    with TestClient(app) as test_client:
        yield test_client
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def authorized_headers() -> dict[str, str]:
    """공유 오케스트레이션 API 토큰 헤더."""
    return {"X-Orch-Token": API_TOKEN}


def test_publication_requires_the_orchestration_token(client: TestClient) -> None:
    """토큰 없이 이슈를 발행할 수 있으면 안 된다."""
    response = client.post(
        "/experiments/3f2a1c9d-8b7e-4a1f-9c2d-5e6f7a8b9c0d/issue", json={}
    )

    assert response.status_code == 401


def test_publication_returns_the_issue_coordinates(
    client: TestClient, authorized_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """응답에 이슈 번호·URL·브랜치가 실려야 UI가 링크를 만들 수 있다."""
    created = client.post(
        "/experiments", json={"hypothesis": "ratio"}, headers=authorized_headers
    ).json()

    response = client.post(
        f"/experiments/{created['id']}/issue",
        json={"allowed_scope": ["prod_model_contract"]},
        headers=authorized_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["issue_number"] > 0
    assert body["issue_branch"].startswith("exp/")


def test_daily_limit_maps_to_429(
    client: TestClient, authorized_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """상한 초과는 서버 오류가 아니라 호출자가 조절할 신호다."""
    async def raise_limit(*_args: object, **_kwargs: object) -> None:
        raise IssuePublicationLimitError(20)

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.router.publish_experiment_issue", raise_limit
    )
    created = client.post(
        "/experiments", json={"hypothesis": "ratio"}, headers=authorized_headers
    ).json()

    response = client.post(
        f"/experiments/{created['id']}/issue", json={}, headers=authorized_headers
    )

    assert response.status_code == 429


def test_llm_backend_failure_maps_to_502_not_500(
    client: TestClient, authorized_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM 호출 실패는 서버 결함(500)이 아니라 502로 알려야 호출자가 원인을 구분한다."""
    created = client.post(
        "/experiments", json={"hypothesis": "ratio"}, headers=authorized_headers
    ).json()

    async def failing_generate_response(
        _settings: ServiceSettings, _prompt: str
    ) -> LLMResult:
        raise LLMBackendError("boom")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.router.generate_response",
        failing_generate_response,
    )

    response = client.post(
        f"/experiments/{created['id']}/issue", json={}, headers=authorized_headers
    )

    assert response.status_code == 502


def test_body_assembly_failure_maps_to_502_not_500(
    client: TestClient, authorized_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """LLM 출력 드리프트(가장 흔한 실패)는 500이 아니라 502여야 재생성 여지를 안다.

    1회 재시도 후에도 계속 산문을 내는 상황을 스텁으로 만들어, 조립 단계의
    `ValueError`가 서버 결함이 아니라 502로 도달함을 확인한다.
    """
    created = client.post(
        "/experiments", json={"hypothesis": "ratio"}, headers=authorized_headers
    ).json()

    async def prose_generate_response(
        _settings: ServiceSettings, _prompt: str
    ) -> LLMResult:
        return LLMResult(text="이것은 산문입니다.", model="stub-llm", token_count=None)

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.router.generate_response",
        prose_generate_response,
    )

    response = client.post(
        f"/experiments/{created['id']}/issue", json={}, headers=authorized_headers
    )

    assert response.status_code == 502
