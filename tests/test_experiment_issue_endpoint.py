"""발행 endpoint의 HTTP 계약을 고정한다.

전체 파이프라인에서 발행 요청의 인증·상태 코드·응답 형태만 검증한다. 발행 절차 자체는
test_experiment_issue_publication이 담당한다.
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
from agent_orchestration.app.experiments.exceptions import IssuePublicationLimitError
from agent_orchestration.app.experiments.github_issues import IssueRef
from agent_orchestration.app.experiments.issue_authoring import ExperimentDefaults

API_TOKEN = "test-orchestration-token"

# test_experiment_issue_publication의 고정값과 동일한 형식 — 파서가 요구하는 필드를
# 모두 채워야 build_issue_body가 실패하지 않는다.
SUBMISSION = {
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
}


def _payload(**overrides: object) -> dict[str, object]:
    """발행 요청 body. `overrides`는 사전등록 필드 쪽에 적용된다."""
    fields = dict(SUBMISSION)
    fields.update(overrides)
    return {"fields": fields}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """chat 초기화와 `gh` 호출을 스텁으로 대체하고 실험 endpoint만 SQLite로 실행한다.

    이 테스트는 발행 endpoint의 HTTP 계약만 검증하므로 실제 GitHub 네트워크는 타지
    않는다. LLM 스텁은 없다 — 발행 경로가 LLM을 부르지 않는다(#536).
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

    async def fake_resolve_dev_sha(_settings: object) -> str:
        return "a" * 40

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.resolve_dev_sha",
        fake_resolve_dev_sha,
        raising=False,
    )

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
        "/experiments/3f2a1c9d-8b7e-4a1f-9c2d-5e6f7a8b9c0d/issue", json=_payload()
    )

    assert response.status_code == 401


def test_publication_returns_the_issue_coordinates(
    client: TestClient, authorized_headers: dict[str, str]
) -> None:
    """응답에 이슈 번호·URL·브랜치가 실려야 UI가 링크를 만들 수 있다."""
    created = client.post(
        "/experiments", json={"hypothesis": "ratio"}, headers=authorized_headers
    ).json()

    response = client.post(
        f"/experiments/{created['id']}/issue",
        json={**_payload(), "allowed_scope": ["prod_model_contract"]},
        headers=authorized_headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["issue_number"] > 0
    assert body["issue_branch"].startswith("exp/")
    assert body["base_dev_sha"] == "a" * 40
    assert "executor_job_created_at" not in body


def test_publication_labels_the_issue_for_the_branch_workflow(
    client: TestClient, authorized_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`auto-experiment` label이 빠지면 브랜치 생성 job이 흔적 없이 skip된다.

    `gh issue create`가 Issue Form을 우회하므로 label 자동 적용을 받지 못한다.
    CONTRIBUTING.md의 "Form을 우회해 API로 발행하면 직접 부여해야 한다"가 근거다.
    """
    seen: list[tuple[str, ...]] = []

    async def recording_create_issue(
        _settings: object, *, title: str, body: str, labels: tuple[str, ...]
    ) -> IssueRef:
        seen.append(labels)
        return IssueRef(number=610, url="https://github.com/SKYAHO/Autoresearch/issues/610")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", recording_create_issue
    )
    created = client.post(
        "/experiments", json={"hypothesis": "ratio"}, headers=authorized_headers
    ).json()

    client.post(
        f"/experiments/{created['id']}/issue",
        json=_payload(),
        headers=authorized_headers,
    )

    assert "auto-experiment" in seen[0]


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
        f"/experiments/{created['id']}/issue", json=_payload(), headers=authorized_headers
    )

    assert response.status_code == 429


@pytest.mark.parametrize(
    "overrides",
    [
        {"primary_metric_direction": "maximize"},
        {"minimum_primary_delta": "약간"},
        {"primary_metric_name": "roc auc"},
        {"hypothesis": "요약\n### 연구 가설\n(설명)"},
        {"guardrail_metric_name": "logloss"},  # 방향·악화폭 없이 이름만
    ],
)
def test_field_violations_map_to_422_before_any_issue_is_opened(
    client: TestClient,
    authorized_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
) -> None:
    """형식 위반은 이슈가 열리기 전에 422로 끊긴다.

    LLM이 값을 만들던 때에는 같은 위반이 502였고, 조립을 통과한 값은 이슈가 **발행된
    뒤** 워크플로 파서에서만 걸렸다. 호출자가 값을 주는 지금은 요청 검증이 그 자리를
    대신한다.
    """

    async def unexpected_create_issue(
        _settings: object, *, title: str, body: str, labels: tuple[str, ...]
    ) -> IssueRef:
        raise AssertionError("요청 검증에서 끊겼어야 한다")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", unexpected_create_issue
    )
    created = client.post(
        "/experiments", json={"hypothesis": "ratio"}, headers=authorized_headers
    ).json()

    response = client.post(
        f"/experiments/{created['id']}/issue",
        json=_payload(**overrides),
        headers=authorized_headers,
    )

    assert response.status_code == 422


def test_missing_fields_are_rejected(
    client: TestClient, authorized_headers: dict[str, str]
) -> None:
    """`fields`가 없으면 발행할 값이 없다 — 서버가 대신 만들지 않는다."""
    created = client.post(
        "/experiments", json={"hypothesis": "ratio"}, headers=authorized_headers
    ).json()

    response = client.post(
        f"/experiments/{created['id']}/issue", json={}, headers=authorized_headers
    )

    assert response.status_code == 422
