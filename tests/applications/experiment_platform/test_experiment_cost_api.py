"""실험 실행 비용의 파생·환산·조회 계약을 검증한다.

전체 파이프라인에서 executor가 남긴 로그와 시각 기록이 워크벤치가 읽는 실행 비용으로
바뀌는 구간의 도메인·service·HTTP 경계를 검증한다. 화면 배치는
`tests/applications/experiment_platform/test_agent_orchestration_ui_report.py`와 같은 UI 테스트가 담당한다.

**이 파일이 지키는 것은 "만들어 낸 숫자를 보이지 않는다"이다.** 과금 구분이 없는
실험에 정가를 매기면 실제보다 몇 배 큰 금액이 화면에서 사실처럼 보인다.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from applications.experiment_platform.api import main as main_module
from applications.experiment_platform.api.config import ServiceSettings
from applications.experiment_platform.api.database import Base
from applications.experiment_platform.api.experiments.cost import (
    COMPUTE_HOURLY_USD,
    build_experiment_cost,
    parse_stage_tokens,
)
from applications.experiment_platform.api.experiments.models import (
    Experiment,
    ExperimentEvent,
    ExperimentLog,
    ExperimentStatus,
)

API_TOKEN = "a" * 32
AUTH_HEADERS = {"X-Orch-Token": API_TOKEN}


def _usage_line(
    stage: str,
    *,
    input_tokens: int,
    cached: int,
    output: int,
    reasoning: int = 0,
) -> str:
    """executor가 남기는 구조화 사용량 줄을 그대로 만든다(#742)."""
    return (
        f"codex token usage stage={stage} available=1 turns=1 "
        f"input={input_tokens} cached_input={cached} "
        f"fresh_input={input_tokens - cached} output={output} "
        f"reasoning={reasoning} total={input_tokens + output}\n"
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """일반 API 토큰 경계로 비용 조회 endpoint를 SQLite에서 실행한다.

    데이터 준비는 `client.app.state.experiment_session_factory`를 거친다 —
    `tests/applications/experiment_platform/test_experiment_report_api.py`와 같은 이유로 별도 engine을 만들지 않는다.
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
        gh_timeout_sec=30,
        issue_daily_limit=20,
    )
    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    monkeypatch.setattr(main_module, "ensure_schema", lambda *_args: None)
    monkeypatch.setattr(main_module, "create_database_engine", lambda *_args: engine)

    app = main_module.create_app()
    with TestClient(app) as test_client:
        yield test_client
    Base.metadata.drop_all(engine)
    engine.dispose()


def _seed_experiment(
    client: TestClient,
    *,
    log_contents: list[str],
    ran_for: timedelta | None = timedelta(minutes=30),
) -> uuid.UUID:
    """완주한 실험 하나와 그 로그를 적재한다."""
    started_at = datetime(2026, 8, 11, 3, 0, tzinfo=UTC)
    factory = client.app.state.experiment_session_factory
    with factory() as session:
        experiment = Experiment(
            hypothesis="규제만 추가해 전반적 개선을 얻는다",
            status=ExperimentStatus.PASSED.value,
            executor_job_created_at=started_at if ran_for is not None else None,
        )
        session.add(experiment)
        session.flush()
        experiment_id = experiment.id
        if ran_for is not None:
            session.add(
                ExperimentEvent(
                    experiment_id=experiment_id,
                    idempotency_key="event-1",
                    request_fingerprint="f" * 64,
                    from_status=ExperimentStatus.EVALUATING.value,
                    to_status=ExperimentStatus.PASSED.value,
                    created_at=started_at + ran_for,
                )
            )
        for order, content in enumerate(log_contents):
            session.add(
                ExperimentLog(
                    experiment_id=experiment_id,
                    idempotency_key=f"log-{order}",
                    request_fingerprint="f" * 64,
                    log_type="codex-worker",
                    content=content,
                    created_at=started_at + timedelta(seconds=order),
                )
            )
        session.commit()
    return experiment_id


def test_stage_tokens_are_read_per_stage_and_summed() -> None:
    """stage가 여러 번 나오면 합산하고, stage끼리는 나눠 둔다."""
    contents = [
        _usage_line("codex-worker", input_tokens=1000, cached=800, output=50),
        _usage_line("codex-worker", input_tokens=500, cached=400, output=10),
        _usage_line("experiment-report", input_tokens=300, cached=200, output=90),
    ]

    stages = parse_stage_tokens(contents)

    assert [stage.stage for stage in stages] == ["codex-worker", "experiment-report"]
    worker = stages[0]
    assert worker.input_tokens == 1500
    assert worker.cached_input_tokens == 1200
    assert worker.fresh_input_tokens == 300
    assert worker.output_tokens == 60


def test_usage_line_split_across_log_chunks_is_recovered() -> None:
    """수집기가 8000자 경계에서 자른 줄도 이어 붙이면 읽혀야 한다.

    이 파싱이 깨지면 사용량이 조용히 사라지고, 화면은 "기록 없음"을 사실처럼 보인다.
    """
    line = _usage_line("codex-worker", input_tokens=1000, cached=900, output=20)
    joined = line[:40] + line[40:]

    stages = parse_stage_tokens([joined])

    assert len(stages) == 1
    assert stages[0].input_tokens == 1000


def test_cache_discount_is_applied_to_the_cached_share_only() -> None:
    """캐시 적중분과 신규 입력분은 서로 다른 단가로 매겨야 한다."""
    contents = [_usage_line("codex-worker", input_tokens=1_000_000, cached=900_000, output=0)]

    cost = build_experiment_cost(wall_clock_seconds=3600, log_contents=contents)

    assert cost.breakdown_available is True
    # 신규 100k × $0.20/1M + 캐시 900k × $0.02/1M
    assert cost.token_usd == pytest.approx(0.02 + 0.018)
    assert cost.token_usd_without_cache == pytest.approx(0.20)
    assert cost.compute_usd == pytest.approx(COMPUTE_HOURLY_USD)


def test_legacy_total_yields_no_money_rather_than_a_full_price_guess() -> None:
    """분해가 없는 실험에 정가를 매겨 몇 배 큰 금액을 보이면 안 된다."""
    cost = build_experiment_cost(
        wall_clock_seconds=1800, log_contents=["tokens used\n75,049\n"]
    )

    assert cost.breakdown_available is False
    assert cost.total_tokens == 75049
    assert cost.token_usd is None
    assert cost.token_usd_without_cache is None


def test_unknown_wall_clock_is_none_rather_than_zero() -> None:
    """아직 실행되지 않은 실험의 컴퓨트 비용은 0이 아니라 '모름'이다."""
    cost = build_experiment_cost(wall_clock_seconds=None, log_contents=[])

    assert cost.wall_clock_seconds is None
    assert cost.compute_usd is None
    assert cost.total_tokens == 0


def test_usage_endpoint_returns_the_stage_breakdown(
    client: TestClient,
) -> None:
    """endpoint가 벽시계·stage 분해·환산액을 함께 돌려준다."""
    experiment_id = _seed_experiment(
        client,
        log_contents=[
            _usage_line("codex-worker", input_tokens=94_393, cached=84_954, output=4_968),
            _usage_line("experiment-report", input_tokens=36_832, cached=33_149, output=1_938),
        ],
    )

    response = client.get(f"/experiments/{experiment_id}/usage", headers=AUTH_HEADERS)

    assert response.status_code == 200
    payload = response.json()
    assert payload["wall_clock_seconds"] == pytest.approx(1800.0)
    assert payload["breakdown_available"] is True
    assert [stage["stage"] for stage in payload["stages"]] == [
        "codex-worker",
        "experiment-report",
    ]
    assert payload["stages"][0]["fresh_input_tokens"] == 94_393 - 84_954
    assert payload["token_usd"] > 0
    assert payload["token_usd_without_cache"] > payload["token_usd"]


def test_usage_endpoint_requires_authentication(
    client: TestClient,
) -> None:
    """비용도 다른 실험 조회와 같은 인증 경계 뒤에 있어야 한다."""
    experiment_id = _seed_experiment(client, log_contents=[])

    assert client.get(f"/experiments/{experiment_id}/usage").status_code == 401


def test_usage_endpoint_reports_a_missing_experiment_as_not_found(
    client: TestClient,
) -> None:
    """실험이 없는 것과 기록이 없는 것은 다르다."""
    response = client.get(f"/experiments/{uuid.uuid4()}/usage", headers=AUTH_HEADERS)

    assert response.status_code == 404
