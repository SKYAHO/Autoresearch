"""Candidate SHA migration과 ORM 제약의 정합성을 검증한다.

전체 파이프라인에서 executor가 원격 검증한 candidate SHA를 실험 lineage로 영속화하는
schema 경계만 검증한다. SHA 검증 요청 처리와 상태 전이는 service·router의 책임이다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest
from sqlalchemy import Column


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REVISION_ROOT = PROJECT_ROOT / "applications" / "experiment_platform" / "migrations" / "versions"
MODELS = PROJECT_ROOT / "applications" / "experiment_platform" / "api" / "experiments" / "models.py"


def load_revision(name: str) -> ModuleType:
    """파일명으로 Alembic revision 모듈을 격리 로드한다."""
    path = REVISION_ROOT / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"tests.migrations.{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load migration revision: {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MigrationRecorder:
    """Alembic operation 호출과 순서를 DB 없이 기록한다."""

    def __init__(self) -> None:
        self.added_columns: list[tuple[str, str, str, bool]] = []
        self.created_constraints: list[tuple[str, str, str]] = []
        self.dropped_constraints: list[tuple[str, str, str]] = []
        self.dropped_columns: list[tuple[str, str]] = []

    def _add_column(self, table: str, column: Column[object]) -> None:
        self.added_columns.append(
            (table, str(column.name), str(column.type), bool(column.nullable))
        )

    def _create_check_constraint(self, name: str, table: str, condition: str) -> None:
        self.created_constraints.append((name, table, condition))

    def _drop_constraint(self, name: str, table: str, type_: str) -> None:
        self.dropped_constraints.append((name, table, type_))

    def _drop_column(self, table: str, column: str) -> None:
        self.dropped_columns.append((table, column))

    def run(self, operation) -> None:
        """revision 함수를 실제 DB 없이 Alembic op 대역으로 실행한다."""
        with (
            patch("alembic.op.add_column", self._add_column),
            patch("alembic.op.create_check_constraint", self._create_check_constraint),
            patch("alembic.op.drop_constraint", self._drop_constraint),
            patch("alembic.op.drop_column", self._drop_column),
        ):
            operation()


@pytest.fixture
def recorder() -> MigrationRecorder:
    """각 migration 검증에 빈 recorder를 제공한다."""
    return MigrationRecorder()


def test_revision_chains_to_branch_bootstrap_revision() -> None:
    """revision chain이 끊기면 candidate SHA schema가 배포되지 않는다."""
    revision = load_revision("0005_experiment_candidate_sha")

    assert revision.revision == "0005_experiment_candidate_sha"
    assert revision.down_revision == "0004_experiment_branch_bootstrap"


def test_upgrade_adds_nullable_candidate_sha_and_postgresql_check(
    recorder: MigrationRecorder,
) -> None:
    """nullable 40자 SHA 컬럼과 PostgreSQL 형식 CHECK를 함께 만든다."""
    revision = load_revision("0005_experiment_candidate_sha")
    recorder.run(revision.upgrade)

    assert recorder.added_columns == [
        ("experiments", "candidate_sha", "VARCHAR(40)", True),
    ]
    assert recorder.created_constraints == [
        (
            "ck_experiment_candidate_sha_format",
            "experiments",
            "candidate_sha IS NULL OR candidate_sha ~ '^[0-9a-f]{40}$'",
        )
    ]


def test_downgrade_drops_check_before_candidate_sha_column(
    recorder: MigrationRecorder,
) -> None:
    """downgrade는 의존 제약을 먼저 제거한 뒤 컬럼을 제거한다."""
    revision = load_revision("0005_experiment_candidate_sha")
    recorder.run(revision.downgrade)

    assert recorder.dropped_constraints == [
        ("ck_experiment_candidate_sha_format", "experiments", "check"),
    ]
    assert recorder.dropped_columns == [("experiments", "candidate_sha")]


def test_model_declares_postgresql_only_candidate_sha_check() -> None:
    """SQLite 단위 테스트에는 나오지 않고 PostgreSQL DDL에만 CHECK가 들어간다."""
    text = MODELS.read_text(encoding="utf-8")

    assert "candidate_sha: Mapped[str | None]" in text
    assert 'name="ck_experiment_candidate_sha_format"' in text
    assert '.ddl_if(dialect="postgresql")' in text
