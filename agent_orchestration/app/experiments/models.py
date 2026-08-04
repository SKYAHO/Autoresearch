"""Agent Orchestration 실험 워크벤치의 SQLAlchemy 모델.

전체 파이프라인에서 실험 API가 PostgreSQL에 저장하는 상태·event·log·step·metadata
구조를 담당한다. 상태 전이 검증과 HTTP 요청 처리는 각각 service와 router의 책임이다.

Alembic migration과 동일한 table, server default, FK, index와 unique constraint를
SQLAlchemy 2.x declarative model로 제공한다. 단, `Experiment.updated_at`과
`ExperimentStep.updated_at`의 `onupdate=func.now()`는 **SQLAlchemy를 거치는 UPDATE**에만
적용되는 애플리케이션 레벨 동작이며 DB 트리거가 아니다 — migration에는 대응하는 트리거가
없어 `psql` 직접 UPDATE 등 SQLAlchemy를 우회하는 쓰기에는 적용되지 않는다.

여기서 "SQLAlchemy를 거치는"은 ORM flush와 Core `update()` 문을 **모두** 포함한다.
`onupdate`는 Column 수준 구성이라 컴파일 시 SET 절에 주입되므로, `.values()`에 명시하지
않아도 `SET ..., updated_at=now()`가 나간다. 대비 대상은 ORM이 아니라 SQLAlchemy 바깥의
직접 SQL이다.

Step은 실험 생명주기 상태(`ExperimentStatus`)와 독립적인 **작업 단계** 기록이다.
`experiments.status`를 변경하지 않으며, 전이 그래프 대신 터미널 확정 가드만 가진다.
계약 정본은 `docs/specs/2026-08-04-experiment-step-tracking-v0.md`다.
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


class StepKind(str, enum.Enum):
    """Step의 대분류. 프론트 렌더 경로를 서버가 강제하기 위한 닫힌 집합."""

    FEATURE_ASSEMBLY = "FEATURE_ASSEMBLY"
    FEATURE_DERIVE = "FEATURE_DERIVE"
    TRAIN = "TRAIN"
    EVALUATE = "EVALUATE"
    OTHER = "OTHER"


class StepStatus(str, enum.Enum):
    """Step의 진행 상태."""

    STARTED = "STARTED"
    PROGRESS = "PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


TERMINAL_STEP_STATUSES = frozenset({StepStatus.COMPLETED, StepStatus.FAILED})

_STATUS_CHECK_SQL = "status IN (" + ", ".join(
    f"'{status.value}'" for status in ExperimentStatus
) + ")"
_STEP_KIND_CHECK_SQL = "step_kind IN (" + ", ".join(
    f"'{kind.value}'" for kind in StepKind
) + ")"
_STEP_STATUS_CHECK_SQL = "status IN (" + ", ".join(
    f"'{status.value}'" for status in StepStatus
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
    steps: Mapped[list[ExperimentStep]] = relationship(
        back_populates="experiment",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="ExperimentStep.created_at",
    )
    metadata_entries: Mapped[list[ExperimentMetadata]] = relationship(
        back_populates="experiment",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint(_STATUS_CHECK_SQL, name="ck_experiment_status_valid"),
        Index("ix_experiments_status", "status"),
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


class ExperimentStep(Base):
    """에이전트가 실험 도중 수행하는 작업 단계와 그 진행 상태를 보관한다."""

    __tablename__ = "experiment_steps"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("experiments.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # 생성 시점 payload의 digest다. PATCH의 재시도 판정은 이 값이 아니라 현재 컬럼 값으로
    # 새로 계산한 digest를 쓴다 — key 집합이 다르기 때문이다(spec "정규화 비교의 정의").
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    step_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    step_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=StepStatus.STARTED.value,
        server_default=text("'STARTED'"),
    )
    message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    target: Mapped[dict | None] = mapped_column(_JSON_OBJECT, nullable=True)
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

    experiment: Mapped[Experiment] = relationship(back_populates="steps")

    __table_args__ = (
        CheckConstraint(_STEP_KIND_CHECK_SQL, name="ck_experiment_step_kind_valid"),
        CheckConstraint(_STEP_STATUS_CHECK_SQL, name="ck_experiment_step_status_valid"),
        UniqueConstraint(
            "experiment_id",
            "idempotency_key",
            name="uq_experiment_steps_idempotency",
        ),
        # events/logs의 2컬럼 인덱스와 달리 `id`를 포함한다 — cursor 쿼리의 keyset이
        # (created_at, id)라 3컬럼이라야 정확히 덮는다. 의도적 개선이며 기존 두 인덱스는
        # 이번 범위에서 바꾸지 않는다 (spec "인덱스는 3컬럼").
        Index("ix_steps_experiment_created", "experiment_id", "created_at", "id"),
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
