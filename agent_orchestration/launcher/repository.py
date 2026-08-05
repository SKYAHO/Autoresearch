"""실험 launcher의 PostgreSQL 선점·생성 확인 저장 경계.

[파이프라인] 이슈 좌표와 기준 SHA가 봉인된 `CREATED` Experiment를 executor Job 생성
직전에 `RUNNING`으로 선점하고, Kubernetes에서 Job 존재를 확인한 뒤 내부 시각을 기록하는
구간을 담당한다.

[기능] advisory transaction lock, 미확인 선점 복구 조회, 전역 상한을 적용한
`FOR UPDATE SKIP LOCKED` 선점과 결정론적 이름·상태 event의 단일 transaction 저장을
제공한다.

[비책임] Kubernetes active Job 계산·manifest 생성·API 호출(`launcher.jobs`/`main`)과
Job 완료·실패에 따른 Experiment 상태 회수는 담당하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import uuid

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from agent_orchestration.app.experiments.models import Experiment, ExperimentStatus
from agent_orchestration.app.experiments.service import (
    transition_experiment_in_transaction,
)


ADVISORY_LOCK_STATEMENT = select(func.pg_try_advisory_xact_lock(546, 1))

_COMPLETE_COORDINATES = (
    Experiment.issue_number.is_not(None),
    Experiment.issue_branch.is_not(None),
    Experiment.base_dev_sha.is_not(None),
)

RECOVERABLE_CLAIM_STATEMENT: Select[tuple[Experiment]] = (
    select(Experiment)
    .where(
        Experiment.status == ExperimentStatus.RUNNING.value,
        Experiment.executor_job_name.is_not(None),
        Experiment.executor_job_created_at.is_(None),
        *_COMPLETE_COORDINATES,
    )
    .order_by(Experiment.updated_at.asc(), Experiment.id.asc())
)

CREATED_CLAIM_STATEMENT: Select[tuple[Experiment]] = (
    select(Experiment)
    .where(
        Experiment.status == ExperimentStatus.CREATED.value,
        Experiment.executor_job_name.is_(None),
        *_COMPLETE_COORDINATES,
    )
    .order_by(Experiment.created_at.asc(), Experiment.id.asc())
    .with_for_update(skip_locked=True)
)


@dataclass(frozen=True)
class ClaimedExperiment:
    """executor Job을 동일 입력으로 재구성하기 위한 봉인 좌표."""

    experiment_id: uuid.UUID
    issue_number: int
    issue_branch: str
    base_dev_sha: str
    job_name: str


class ClaimStateError(RuntimeError):
    """Job 존재 확인을 기록하려는 Experiment가 선점 계약과 어긋난다."""


def _try_advisory_lock(session: Session) -> bool:
    """현재 transaction에서 launcher 전역 advisory lock 획득 여부를 반환한다."""
    return bool(session.scalar(ADVISORY_LOCK_STATEMENT))


def _claimed_experiment(experiment: Experiment) -> ClaimedExperiment:
    """완전한 DB 행을 Job 생성용 불변 좌표로 복사한다."""
    if (
        experiment.issue_number is None
        or experiment.issue_branch is None
        or experiment.base_dev_sha is None
        or experiment.executor_job_name is None
    ):
        raise ClaimStateError(f"incomplete claim: {experiment.id}")
    return ClaimedExperiment(
        experiment_id=experiment.id,
        issue_number=experiment.issue_number,
        issue_branch=experiment.issue_branch,
        base_dev_sha=experiment.base_dev_sha,
        job_name=experiment.executor_job_name,
    )


def _job_name(experiment_id: uuid.UUID) -> str:
    """Experiment UUID에서 DNS label 길이 안의 결정론적 Job 이름을 만든다."""
    return f"ar-branch-{experiment_id.hex}"


def claim_experiments(
    session: Session,
    *,
    active_jobs: int,
    max_concurrency: int,
) -> list[ClaimedExperiment]:
    """미확인 선점을 먼저 반환하고 남은 전역 슬롯만큼 새 행을 선점한다.

    PostgreSQL advisory lock을 얻지 못하면 정상적으로 빈 목록을 반환한다. 미확인 선점은
    active 상한과 무관하게 먼저 반환하되 한 tick의 상한 개수까지만 조회한다. Kubernetes에
    Job이 없는 미확인 선점은 caller가 남은 슬롯 안에서만 생성한다. 반환한 미확인 선점은
    새 슬롯 계산에서 보수적으로 차감한다.
    """
    if active_jobs < 0:
        raise ValueError("active_jobs must be non-negative")
    if max_concurrency <= 0:
        raise ValueError("max_concurrency must be positive")

    with session.begin():
        if not _try_advisory_lock(session):
            return []

        available_slots = max(0, max_concurrency - active_jobs)
        recoverable_rows = list(
            session.scalars(
                RECOVERABLE_CLAIM_STATEMENT.limit(max_concurrency)
            ).all()
        )
        recoverable_claims = [_claimed_experiment(row) for row in recoverable_rows]
        available_slots = max(0, available_slots - len(recoverable_claims))
        if available_slots == 0:
            return recoverable_claims

        created_rows = list(
            session.scalars(
                CREATED_CLAIM_STATEMENT.limit(available_slots)
            ).all()
        )
        created_claims: list[ClaimedExperiment] = []
        for experiment in created_rows:
            job_name = _job_name(experiment.id)
            experiment.executor_job_name = job_name
            experiment.executor_job_created_at = None
            transition_experiment_in_transaction(
                session,
                experiment.id,
                requested=ExperimentStatus.RUNNING,
                reason=f"executor job claimed: {job_name}",
                metric_snapshot=None,
                idempotency_key=f"launcher-claim:{experiment.id}",
                check_idempotency=True,
            )
            created_claims.append(_claimed_experiment(experiment))

        return [*recoverable_claims, *created_claims]


def record_job_created(
    session: Session,
    claim: ClaimedExperiment,
    *,
    created_at: datetime,
) -> None:
    """동일 이름 Job 존재를 확인한 UTC 시각을 한 번만 기록한다."""
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("created_at must be timezone-aware")
    with session.begin():
        experiment = session.scalar(
            select(Experiment)
            .where(Experiment.id == claim.experiment_id)
            .with_for_update()
        )
        if (
            experiment is None
            or experiment.status != ExperimentStatus.RUNNING.value
            or experiment.executor_job_name != claim.job_name
        ):
            raise ClaimStateError(f"claim state changed: {claim.experiment_id}")
        if experiment.executor_job_created_at is None:
            experiment.executor_job_created_at = created_at.astimezone(UTC)
