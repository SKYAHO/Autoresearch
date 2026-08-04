"""Agent Orchestration 실험 워크벤치의 SQLAlchemy 모델.

전체 파이프라인에서 실험 API가 PostgreSQL에 저장하는 상태·event·log·metadata 구조를
담당한다. 상태 전이 검증과 HTTP 요청 처리는 각각 service와 router의 책임이다.

Alembic 초기 migration과 동일한 table, server default, FK, index와 unique constraint를
SQLAlchemy 2.x declarative model로 제공한다. 단, `Experiment.updated_at`의
`onupdate=func.now()`는 이 ORM을 거치는 UPDATE에만 적용되는 애플리케이션 레벨 동작이며
DB 트리거가 아니다 — migration에는 대응하는 트리거가 없어 `psql` 직접 UPDATE 등
ORM을 우회하는 쓰기에는 적용되지 않는다.

`Experiment.issue_body`/`issue_number`/`issue_branch`/`issue_published_at`은
`0002_experiment_issue_lineage`
revision이 nullable로 추가한 발행 lineage다. `issue_body`는 발행 **전**에, 나머지 셋은
발행 성공 후에 채워진다. `issue_published_at`은 일일 발행 상한 질의 전용이며
`ExperimentResponse`에 노출되지 않는다.
"""

from __future__ import annotations

from datetime import datetime
import enum
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from agent_orchestration.app.database import Base


class ExperimentStatus(str, enum.Enum):
    """실험 생명주기의 공개 상태 값."""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    EVALUATING = "EVALUATING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"
    PROMOTED = "PROMOTED"


TERMINAL_STATUSES = frozenset(
    {
        ExperimentStatus.FAILED,
        ExperimentStatus.ERROR,
        ExperimentStatus.PROMOTED,
    }
)

ALLOWED_TRANSITIONS: dict[ExperimentStatus, frozenset[ExperimentStatus]] = {
    ExperimentStatus.CREATED: frozenset({ExperimentStatus.RUNNING}),
    ExperimentStatus.RUNNING: frozenset(
        {ExperimentStatus.EVALUATING, ExperimentStatus.ERROR}
    ),
    ExperimentStatus.EVALUATING: frozenset(
        {
            ExperimentStatus.PASSED,
            ExperimentStatus.FAILED,
            ExperimentStatus.ERROR,
        }
    ),
    ExperimentStatus.PASSED: frozenset({ExperimentStatus.PROMOTED}),
    ExperimentStatus.FAILED: frozenset(),
    ExperimentStatus.ERROR: frozenset(),
    ExperimentStatus.PROMOTED: frozenset(),
}

_STATUS_CHECK_SQL = "status IN (" + ", ".join(
    f"'{status.value}'" for status in ExperimentStatus
) + ")"
_JSON_OBJECT = JSON().with_variant(JSONB(), "postgresql")


class Experiment(Base):
    """가설 한 건과 최신 상태·지표를 보관한다."""

    __tablename__ = "experiments"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=ExperimentStatus.CREATED.value,
        server_default=text("'CREATED'"),
    )
    metric_summary: Mapped[dict | None] = mapped_column(_JSON_OBJECT, nullable=True)
    agent_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 발행 전에 커밋되는 본문. 재시도가 LLM을 다시 부르지 않고 같은 본문으로 발행하게
    # 해 criteria_id/reproducibility_id가 흔들리지 않도록 한다(#516).
    issue_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issue_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 일일 발행 상한 질의 전용. `updated_at`은 `onupdate=func.now()`라 발행과 무관한
    # UPDATE에도 갱신되어 며칠 전 발행분을 "오늘 발행"으로 잘못 센다.
    issue_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        # ORM UPDATE에만 적용되는 애플리케이션 레벨 갱신 — DB 트리거 아님 (모듈 docstring 참고)
        onupdate=func.now(),
        nullable=False,
    )

    events: Mapped[list[ExperimentEvent]] = relationship(
        back_populates="experiment",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ExperimentEvent.created_at",
    )
    logs: Mapped[list[ExperimentLog]] = relationship(
        back_populates="experiment",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ExperimentLog.created_at",
    )
    metadata_entries: Mapped[list[ExperimentMetadata]] = relationship(
        back_populates="experiment",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(_STATUS_CHECK_SQL, name="ck_experiment_status_valid"),
        Index("ix_experiments_status", "status"),
        Index("ix_experiments_issue_number", "issue_number"),
    )


class ExperimentEvent(Base):
    """상태 전이와 당시의 판단 근거를 기록한다."""

    __tablename__ = "experiment_events"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    metric_snapshot: Mapped[dict | None] = mapped_column(_JSON_OBJECT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    experiment: Mapped[Experiment] = relationship(back_populates="events")

    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "idempotency_key",
            name="uq_experiment_events_idempotency",
        ),
        Index("ix_events_experiment_created", "experiment_id", "created_at"),
    )


class ExperimentLog(Base):
    """상태와 독립적으로 누적되는 원본 실행 로그를 보관한다."""

    __tablename__ = "experiment_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    log_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="stdout", server_default=text("'stdout'")
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    experiment: Mapped[Experiment] = relationship(back_populates="logs")

    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "idempotency_key",
            name="uq_experiment_logs_idempotency",
        ),
        Index("ix_logs_experiment_created", "experiment_id", "created_at"),
    )


class ExperimentMetadata(Base):
    """실험의 feature·model·branch key-value metadata를 보관한다."""

    __tablename__ = "experiment_metadata"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    experiment: Mapped[Experiment] = relationship(back_populates="metadata_entries")

    __table_args__ = (
        UniqueConstraint(
            "experiment_id",
            "key",
            name="uq_experiment_metadata_key",
        ),
    )
