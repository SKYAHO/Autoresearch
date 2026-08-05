"""Experiment 기준 dev SHA의 선커밋과 API 노출 계약을 검증한다.

전체 파이프라인에서 사전등록 본문을 GitHub 이슈로 발행하기 직전에 `dev` tip을 한 번
읽어 Experiment에 봉인하는 구간을 담당한다. installation token 발급과 Git ref REST
세부 동작, launcher/executor 실행은 각각 전용 테스트의 책임이다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy import Engine, create_engine, event, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.dml import Update

from agent_orchestration.app.database import Base
from agent_orchestration.app.experiments.github_issues import GitHubIssueError, IssueRef
from agent_orchestration.app.experiments.issue_authoring import ExperimentDefaults
from agent_orchestration.app.experiments.models import Experiment
from agent_orchestration.app.experiments.schemas import (
    ExperimentCreate,
    ExperimentResponse,
    IssuePublicationRequest,
    IssuePublicationResponse,
)
from agent_orchestration.app.experiments.service import (
    create_experiment,
    publish_experiment_issue,
)


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


def _request() -> IssuePublicationRequest:
    """호출자가 제출하는 완전한 사전등록 요청을 만든다."""
    return IssuePublicationRequest.model_validate({"fields": SUBMISSION})


@dataclass(frozen=True)
class _Settings:
    github_token: str = "x" * 40
    github_repository: str = "SKYAHO/Autoresearch"
    gh_timeout_sec: int = 5
    issue_daily_limit: int = 20
    experiment_defaults: ExperimentDefaults = field(
        default_factory=lambda: ExperimentDefaults(
            dataset_source="feast://feast_offline_store/ctr_training_v1",
            training_config_ref="configs/train/lgbm-v1.yaml@abc1234",
        )
    )


@pytest.fixture
def db_session() -> Iterator[Session]:
    """실제 ORM commit 경계를 관찰하는 SQLite Session."""
    engine: Engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def register_uuid_function(dbapi_connection, _record) -> None:
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: uuid.uuid4().hex)

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def _no_marker_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _absent(_settings: object, *, marker: str) -> None:
        return None

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.find_issue_by_marker", _absent
    )


def test_publish_retry_reuses_frozen_sha_after_dev_moves(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """첫 외부 발행이 실패해도 재시도는 저장된 SHA만 사용해야 한다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="ratio"))
    resolver = AsyncMock(side_effect=["a" * 40, "b" * 40])
    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.resolve_dev_sha",
        resolver,
        raising=False,
    )

    async def failing_create_issue(_settings, *, title, body, labels):
        with Session(db_session.bind) as observer:
            stored = observer.get(Experiment, experiment.id)
            assert stored is not None
            assert stored.base_dev_sha == "a" * 40
        raise GitHubIssueError("network_error")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", failing_create_issue
    )
    with pytest.raises(GitHubIssueError, match="network_error"):
        asyncio.run(
            publish_experiment_issue(
                db_session, _Settings(), experiment.id, _request()
            )
        )

    async def succeeding_create_issue(_settings, *, title, body, labels):
        return IssueRef(
            number=546,
            url="https://github.com/SKYAHO/Autoresearch/issues/546",
        )

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", succeeding_create_issue
    )
    result = asyncio.run(
        publish_experiment_issue(db_session, _Settings(), experiment.id, _request())
    )

    assert result.base_dev_sha == "a" * 40
    assert resolver.await_count == 1


def test_baseline_lookup_releases_read_transaction_before_github(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub 대기 중에는 앞선 read transaction이 connection을 점유하지 않아야 한다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="ratio"))

    async def resolve_without_open_transaction(_settings: object) -> str:
        assert db_session.in_transaction() is False
        return "a" * 40

    async def create_issue(_settings, *, title, body, labels):
        return IssueRef(
            number=546,
            url="https://github.com/SKYAHO/Autoresearch/issues/546",
        )

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.resolve_dev_sha",
        resolve_without_open_transaction,
    )
    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue",
        create_issue,
    )

    result = asyncio.run(
        publish_experiment_issue(db_session, _Settings(), experiment.id, _request())
    )

    assert result.base_dev_sha == "a" * 40


def test_competing_baseline_freeze_reuses_atomic_cas_winner(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CAS 경쟁에서 진 발행은 DB 승자의 SHA를 읽어 재사용해야 한다.

    SQLite의 동시 transaction 동작을 검증하지 않는다. 실제 service가 발행한 Core
    UPDATE를 PostgreSQL dialect로 기록하고, DB가 `rowcount=0`을 반환하는 순간을
    주입해 조건과 패배 분기를 함께 검증한다.
    """
    winner_sha = "a" * 40
    losing_sha = "b" * 40
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="ratio"))
    experiment.issue_body = "stored issue body"
    experiment.issue_title = "[AR] stored issue title"
    db_session.commit()

    resolver = AsyncMock(return_value=losing_sha)
    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.resolve_dev_sha",
        resolver,
        raising=False,
    )

    async def create_issue_after_winner_is_visible(
        _settings: object,
        *,
        title: str,
        body: str,
        labels: tuple[str, ...],
    ) -> IssueRef:
        with Session(db_session.bind) as observer:
            stored = observer.get(Experiment, experiment.id)
            assert stored is not None
            assert stored.base_dev_sha == winner_sha
        return IssueRef(
            number=547,
            url="https://github.com/SKYAHO/Autoresearch/issues/547",
        )

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue",
        create_issue_after_winner_is_visible,
    )

    conditional_update_sql: list[str] = []
    with Session(db_session.bind, expire_on_commit=False) as competing_session:
        original_execute = competing_session.execute

        def execute_with_lost_cas(statement, *args, **kwargs):
            if isinstance(statement, Update) and statement.table.name == "experiments":
                conditional_update_sql.append(
                    str(statement.compile(dialect=postgresql.dialect()))
                )
                competing_session.connection().execute(
                    update(Experiment)
                    .where(Experiment.id == experiment.id)
                    .values(base_dev_sha=winner_sha)
                )
                return SimpleNamespace(rowcount=0)
            return original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(competing_session, "execute", execute_with_lost_cas)
        result = asyncio.run(
            publish_experiment_issue(
                competing_session,
                _Settings(),
                experiment.id,
                _request(),
            )
        )

    assert len(conditional_update_sql) == 1
    assert "experiments.id =" in conditional_update_sql[0]
    assert "experiments.base_dev_sha IS NULL" in conditional_update_sql[0]
    assert result.base_dev_sha == winner_sha
    assert resolver.await_count == 1


def test_legacy_definition_write_reuses_atomic_cas_winner(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """기준 SHA만 있는 legacy 행에서도 최초 본문·제목만 저장해야 한다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="ratio"))
    experiment.base_dev_sha = "a" * 40
    db_session.commit()
    winner_body = "winner issue body"
    winner_title = "[AR] winner issue title"

    async def create_issue_with_winner_definition(
        _settings: object,
        *,
        title: str,
        body: str,
        labels: tuple[str, ...],
    ) -> IssueRef:
        assert title == winner_title
        assert body == winner_body
        return IssueRef(
            number=548,
            url="https://github.com/SKYAHO/Autoresearch/issues/548",
        )

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue",
        create_issue_with_winner_definition,
    )

    conditional_update_sql: list[str] = []
    with Session(db_session.bind, expire_on_commit=False) as competing_session:
        original_execute = competing_session.execute

        def execute_with_lost_definition_cas(statement, *args, **kwargs):
            if isinstance(statement, Update) and statement.table.name == "experiments":
                compiled = str(statement.compile(dialect=postgresql.dialect()))
                if "experiments.issue_body IS NULL" in compiled:
                    conditional_update_sql.append(compiled)
                    competing_session.connection().execute(
                        update(Experiment)
                        .where(Experiment.id == experiment.id)
                        .values(issue_body=winner_body, issue_title=winner_title)
                    )
                    return SimpleNamespace(rowcount=0)
            return original_execute(statement, *args, **kwargs)

        monkeypatch.setattr(competing_session, "execute", execute_with_lost_definition_cas)
        result = asyncio.run(
            publish_experiment_issue(
                competing_session,
                _Settings(),
                experiment.id,
                _request(),
            )
        )

    assert len(conditional_update_sql) == 1
    assert result.issue_body == winner_body
    assert result.issue_title == winner_title


def test_response_contract_exposes_safe_branch_coordinates_only() -> None:
    """공개 응답에는 기준 SHA와 Job 이름만 있고 내부 생성 확인 시각은 없어야 한다."""
    assert "base_dev_sha" in ExperimentResponse.model_fields
    assert "executor_job_name" in ExperimentResponse.model_fields
    assert "executor_job_created_at" not in ExperimentResponse.model_fields
    response = IssuePublicationResponse(
        issue_number=546,
        issue_url="https://github.com/SKYAHO/Autoresearch/issues/546",
        issue_branch="exp/546-ratio",
        base_dev_sha="a" * 40,
    )
    assert response.base_dev_sha == "a" * 40
    assert "executor_job_created_at" not in response.model_dump()
