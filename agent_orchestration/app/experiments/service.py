"""Agent Orchestration 실험 생성·조회와 상태 쓰기 유스케이스를 제공한다.

전체 파이프라인에서 검증된 API 입력을 SQLAlchemy transaction으로 실험·event·metadata에
반영하는 구간을 담당한다. HTTP 인증·상태 코드 변환과 실제 학습 실행은 담당하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import uuid

from sqlalchemy.orm import Session

from agent_orchestration.app.experiments.exceptions import ExperimentNotFoundError
from agent_orchestration.app.experiments.models import (
    Experiment,
    ExperimentEvent,
    ExperimentMetadata,
    ExperimentStatus,
)
from agent_orchestration.app.experiments.repository import (
    find_experiment,
    find_experiment_metadata,
    find_experiments,
)
from agent_orchestration.app.experiments.schemas import ExperimentCreate


@dataclass(frozen=True)
class ExperimentPageResult:
    """목록 응답을 만들기 위한 현재 page와 전체 건수."""

    items: list[Experiment]
    total: int


def _request_fingerprint(payload: dict) -> str:
    """의미 있는 요청 payload의 canonical JSON SHA-256을 반환한다."""
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def create_experiment(session: Session, request: ExperimentCreate) -> Experiment:
    """실험, metadata와 최초 CREATED event를 원자적으로 생성한다."""
    experiment = Experiment(
        hypothesis=request.hypothesis,
        agent_session_id=request.agent_session_id,
    )
    with session.begin():
        session.add(experiment)
        session.flush()
        session.add_all(
            ExperimentMetadata(
                experiment_id=experiment.id,
                key=key,
                value=value,
            )
            for key, value in request.metadata.items()
        )
        initial_key = f"experiment-created:{experiment.id}"
        session.add(
            ExperimentEvent(
                experiment_id=experiment.id,
                idempotency_key=initial_key,
                request_fingerprint=_request_fingerprint(
                    {"experiment_id": str(experiment.id), "to_status": "CREATED"}
                ),
                from_status=None,
                to_status=ExperimentStatus.CREATED.value,
            )
        )
    return experiment


def get_experiment(session: Session, experiment_id: uuid.UUID) -> Experiment:
    """실험을 반환하거나 도메인 not-found 오류를 발생시킨다."""
    experiment = find_experiment(session, experiment_id)
    if experiment is None:
        raise ExperimentNotFoundError(experiment_id)
    return experiment


def list_experiments(
    session: Session,
    *,
    limit: int,
    offset: int,
    status: ExperimentStatus | None = None,
) -> ExperimentPageResult:
    """필터와 pagination을 적용한 실험 목록을 반환한다."""
    items, total = find_experiments(
        session,
        limit=limit,
        offset=offset,
        status=status,
    )
    return ExperimentPageResult(items=items, total=total)


def get_experiment_metadata(
    session: Session,
    experiment_id: uuid.UUID,
) -> dict[str, str]:
    """존재하는 실험의 metadata를 mapping으로 반환한다."""
    get_experiment(session, experiment_id)
    return find_experiment_metadata(session, experiment_id)
