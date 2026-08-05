"""사전등록 필드가 `[AR]` 이슈가 되는 절차와 멱등성을 고정한다.

전체 파이프라인에서 본문 조립·저장과 발행 사이의 순서·재시도 의미만 검증한다. 본문
형식은 test_issue_authoring, gh 호출은 test_github_issues가 담당한다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sys
import uuid

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from agent_orchestration.app.database import Base
from agent_orchestration.app.experiments.exceptions import IssuePublicationLimitError
from agent_orchestration.app.experiments.github_issues import GitHubIssueError, IssueRef
from agent_orchestration.app.experiments.issue_authoring import ExperimentDefaults
from agent_orchestration.app.experiments.models import ExperimentStatus
from agent_orchestration.app.experiments.schemas import (
    ExperimentCreate,
    IssuePublicationRequest,
    StatusUpdateRequest,
)
from agent_orchestration.app.experiments.service import (
    create_experiment,
    publish_experiment_issue,
    update_experiment_status,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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


def _request(**overrides: object) -> IssuePublicationRequest:
    """호출자가 제출하는 사전등록 필드를 실은 발행 요청을 만든다."""
    fields = dict(SUBMISSION)
    fields.update(overrides)
    return IssuePublicationRequest.model_validate({"fields": fields})


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
    """marker 조회가 실제 GitHub으로 나가지 않게 한다.

    이 조회를 검증하는 테스트는 각자 다시 monkeypatch한다.
    """

    async def _absent(_settings: object, *, marker: str) -> None:
        return None

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.find_issue_by_marker", _absent
    )


def test_publication_stores_body_before_creating_the_issue(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """본문이 발행 전에 커밋되어야 재시도가 결정론적이다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="ratio"))
    seen: list[str] = []

    async def fake_create_issue(_settings, *, title, body, labels):
        seen.append(body)
        return IssueRef(number=520, url="https://github.com/SKYAHO/Autoresearch/issues/520")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", fake_create_issue
    )

    result = asyncio.run(
        publish_experiment_issue(
            db_session,
            _Settings(),
            experiment.id,
            _request(),
        )
    )

    assert result.issue_number == 520
    assert result.issue_body == seen[0]
    assert result.issue_branch.startswith("exp/520-")


def test_submission_field_violation_is_rejected_before_publication(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """형식 위반은 요청 검증에서 끊긴다 — 이슈가 열린 뒤 워크플로에서 실패하면 안 된다."""

    async def unexpected_create_issue(_settings, *, title, body, labels):
        raise AssertionError("요청 검증에서 끊겼어야 한다")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", unexpected_create_issue
    )

    with pytest.raises(ValueError):
        _request(primary_metric_direction="maximize")


def test_retry_after_publish_failure_reuses_the_stored_body(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`대상 데이터 · 기간`이 발행 시점 날짜로 계산되므로 재발행은 저장된 본문을 써야 한다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="ratio"))
    attempts: list[str] = []
    titles: list[str] = []

    async def failing_create_issue(_settings, *, title, body, labels):
        attempts.append(body)
        titles.append(title)
        raise GitHubIssueError("network_error")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", failing_create_issue
    )
    with pytest.raises(GitHubIssueError):
        asyncio.run(
            publish_experiment_issue(
                db_session, _Settings(), experiment.id,
                _request(),
            )
        )

    async def succeeding_create_issue(_settings, *, title, body, labels):
        attempts.append(body)
        titles.append(title)
        return IssueRef(number=521, url="https://github.com/SKYAHO/Autoresearch/issues/521")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", succeeding_create_issue
    )
    asyncio.run(
        publish_experiment_issue(
            db_session, _Settings(), experiment.id,
            _request(),
        )
    )

    assert attempts[0] == attempts[1], "같은 본문으로 재발행해야 한다"
    assert titles[0] == titles[1], "같은 제목으로 재발행해야 한다 — 본문에서 복원하면 갈릴 수 있다"


def test_second_call_after_success_does_not_publish_again(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """멱등성 1차 방어 — issue_number가 있으면 발행하지 않는다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="ratio"))
    calls = 0

    async def fake_create_issue(_settings, *, title, body, labels):
        nonlocal calls
        calls += 1
        return IssueRef(number=520, url="https://github.com/SKYAHO/Autoresearch/issues/520")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", fake_create_issue
    )
    for _ in range(2):
        asyncio.run(
            publish_experiment_issue(
                db_session, _Settings(), experiment.id,
                _request(),
            )
        )

    assert calls == 1


def test_stored_body_wins_over_a_changed_resubmission(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """발행 실패 후 다른 값으로 재호출해도 저장된 본문이 발행된다.

    본문이 커밋된 뒤에는 실험 정의가 봉인된 것으로 본다. 이것이 뚫리면 `criteria_id`가
    호출자의 재시도만으로 조용히 바뀐다. 정의를 바꾸려면 새 실험을 만들어야 한다.
    """
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="ratio"))
    published: list[str] = []

    async def failing_create_issue(_settings, *, title, body, labels):
        raise GitHubIssueError("network_error")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", failing_create_issue
    )
    with pytest.raises(GitHubIssueError):
        asyncio.run(
            publish_experiment_issue(
                db_session, _Settings(), experiment.id,
                _request(),
            )
        )
    stored = experiment.issue_body

    async def succeeding_create_issue(_settings, *, title, body, labels):
        published.append(body)
        return IssueRef(number=522, url="https://github.com/SKYAHO/Autoresearch/issues/522")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", succeeding_create_issue
    )
    asyncio.run(
        publish_experiment_issue(
            db_session, _Settings(), experiment.id,
            _request(minimum_primary_delta="0.999"),
        )
    )

    assert published[0] == stored
    assert "0.999" not in published[0]


def test_daily_limit_blocks_publication(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """호출 주체가 생겼으므로 폭주 방지를 여기에 둔다(#490 결정)."""

    async def fake_create_issue(_settings, *, title, body, labels):
        return IssueRef(number=520, url="https://github.com/SKYAHO/Autoresearch/issues/520")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", fake_create_issue
    )
    first = create_experiment(db_session, ExperimentCreate(hypothesis="one"))
    asyncio.run(
        publish_experiment_issue(
            db_session, _Settings(issue_daily_limit=1), first.id,
            _request(),
        )
    )

    second = create_experiment(db_session, ExperimentCreate(hypothesis="two"))
    with pytest.raises(IssuePublicationLimitError):
        asyncio.run(
            publish_experiment_issue(
                db_session, _Settings(issue_daily_limit=1), second.id,
                _request(),
            )
        )


def test_daily_limit_ignores_unrelated_updated_at_bumps(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`updated_at`은 상태 전이 등 발행과 무관한 UPDATE에도 갱신된다.

    `issue_published_at`이 아니라 `updated_at`으로 상한을 세면, 며칠 전 발행된
    실험이 오늘 상태 전이만 겪어도 "오늘 발행"으로 잡혀 정당한 새 발행을 막는다.
    """

    async def fake_create_issue(_settings, *, title, body, labels):
        return IssueRef(number=520, url="https://github.com/SKYAHO/Autoresearch/issues/520")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", fake_create_issue
    )
    old = create_experiment(db_session, ExperimentCreate(hypothesis="old"))
    asyncio.run(
        publish_experiment_issue(
            db_session, _Settings(issue_daily_limit=1), old.id,
            _request(),
        )
    )

    # 발행 시각은 25시간 전으로 되돌리고, 상태 전이로 `updated_at`만 지금으로 갱신한다.
    old.issue_published_at = datetime.now(UTC) - timedelta(hours=25)
    db_session.commit()
    update_experiment_status(
        db_session, old.id, StatusUpdateRequest(status=ExperimentStatus.RUNNING)
    )

    new = create_experiment(db_session, ExperimentCreate(hypothesis="new"))
    result = asyncio.run(
        publish_experiment_issue(
            db_session, _Settings(issue_daily_limit=1), new.id,
            _request(),
        )
    )

    assert result.issue_number == 520


def test_marker_lookup_recovers_a_lost_publication(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gh는 성공했는데 응답이 소실된 경우 중복 이슈를 만들면 안 된다."""
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="ratio"))

    async def fake_find(_settings, *, marker):
        return IssueRef(number=530, url="https://github.com/SKYAHO/Autoresearch/issues/530")

    async def unexpected_create(_settings, *, title, body, labels):
        raise AssertionError("이미 발행된 이슈를 다시 만들면 안 된다")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.find_issue_by_marker", fake_find
    )
    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", unexpected_create
    )

    result = asyncio.run(
        publish_experiment_issue(
            db_session, _Settings(), experiment.id,
            _request(),
        )
    )

    assert result.issue_number == 530


def test_marker_lookup_failure_does_not_publish(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """조회 실패는 "발행 안 됨"이 아니라 "알 수 없음"이다.

    모르는 상태로 발행하면 멱등성 3중 방어의 3번째 층이 사라진다.
    """
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="ratio"))

    async def failing_find(_settings, *, marker):
        raise GitHubIssueError("authentication_failed")

    async def unexpected_create(_settings, *, title, body, labels):
        raise AssertionError("조회 실패 시 발행으로 넘어가면 안 된다")

    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.find_issue_by_marker", failing_find
    )
    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.create_issue", unexpected_create
    )

    with pytest.raises(GitHubIssueError, match="authentication_failed"):
        asyncio.run(
            publish_experiment_issue(
                db_session, _Settings(), experiment.id,
                _request(),
            )
        )

    assert experiment.issue_body is not None, "재시도 결정성을 위해 본문은 남아 있어야 한다"
    assert experiment.issue_number is None


@pytest.mark.parametrize(
    "title",
    [
        "[AR] views per day ratio feature",
        "[AR] 비율 피처 실험",
        "no prefix ascii title",
        "[AR]    공백만    ",
        "접두어 없는 한글 제목",
    ],
)
def test_branch_name_matches_the_workflow_rule(title: str) -> None:
    """표시용 브랜치 이름이 워크플로가 만들 이름과 같아야 한다."""
    from agent_orchestration.app.experiments.service import _branch_name_for
    from tools.auto_research_issue_branch import branch_name_for

    assert _branch_name_for(520, title) == branch_name_for(520, title)


def test_branch_name_matches_the_workflow_rule_for_an_empty_title() -> None:
    """prefix를 떼고 남은 것이 공백뿐이면 양쪽 모두 거부해야 한다."""
    from agent_orchestration.app.experiments.service import _branch_name_for
    from tools.auto_research_issue_branch import branch_name_for

    with pytest.raises(ValueError):
        _branch_name_for(520, "[AR]    ")
    with pytest.raises(ValueError):
        branch_name_for(520, "[AR]    ")
