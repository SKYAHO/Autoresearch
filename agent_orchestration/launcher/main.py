"""CronJob launcher의 단일 tick 실행 경계.

[파이프라인] 이슈 좌표와 기준 SHA가 봉인된 Experiment를 DB에서 선점한 뒤 executor가 exp
branch를 만들도록 Kubernetes Job을 제출하고, Job 존재 확인 시각을 DB에 기록하는 구간을
담당한다.

[기능] label 기반 active Job 수집, 미완료 생성 복구·신규 선점, GET/create/409 확인과
UTC 생성 확인 기록을 한 번 실행한다. 처리하지 않은 예외는 CronJob 실패로 전파한다.

[비책임] 주기·concurrencyPolicy와 RBAC/Secret 배포(Autoresearch-infra), Job 완료 상태
회수, executor의 GitHub ref 생성은 담당하지 않는다.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import logging

from kubernetes import client, config
from sqlalchemy.orm import Session

from agent_orchestration.app.database import (
    create_database_engine,
    create_session_factory,
)
from agent_orchestration.launcher.config import LauncherSettings
from agent_orchestration.launcher.jobs import (
    BRANCH_BOOTSTRAP_LABEL_SELECTOR,
    JobClient,
    KubernetesJobs,
    ensure_branch_job,
)
from agent_orchestration.launcher.repository import (
    ClaimedExperiment,
    claim_experiments,
    record_job_created,
)


_LOGGER = logging.getLogger(__name__)


def _utc_now() -> datetime:
    """timezone-aware 현재 UTC 시각을 반환한다."""
    return datetime.now(UTC)


def run_tick(
    session: Session,
    kubernetes: JobClient,
    settings: LauncherSettings,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> list[ClaimedExperiment]:
    """전역 상한 안에서 복구·선점한 Job을 확인하고 생성 시각을 기록한다."""
    active_jobs = kubernetes.count_active(
        settings.job_namespace,
        BRANCH_BOOTSTRAP_LABEL_SELECTOR,
    )
    available_slots = max(
        0,
        settings.max_concurrent_experiments - active_jobs,
    )
    confirmed_claims: list[ClaimedExperiment] = []
    first_pass = True
    while first_pass or available_slots > 0:
        first_pass = False
        claims = claim_experiments(
            session,
            active_jobs=(
                settings.max_concurrent_experiments - available_slots
            ),
            max_concurrency=settings.max_concurrent_experiments,
        )
        if not claims:
            break
        for claim in claims:
            if kubernetes.get(
                settings.job_namespace,
                claim.job_name,
            ) is not None:
                record_job_created(session, claim, created_at=clock())
                confirmed_claims.append(claim)
                continue
            if available_slots == 0:
                continue
            ensure_branch_job(kubernetes, claim, settings, job_absent=True)
            available_slots -= 1
            record_job_created(session, claim, created_at=clock())
            confirmed_claims.append(claim)
    return confirmed_claims


def main() -> int:
    """in-cluster 설정으로 launcher tick을 한 번 실행한다."""
    settings = LauncherSettings.from_environment()
    config.load_incluster_config()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    try:
        with session_factory() as session:
            claims = run_tick(
                session,
                KubernetesJobs(client.BatchV1Api()),
                settings,
            )
        _LOGGER.info("experiment launcher tick complete claims=%s", len(claims))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
