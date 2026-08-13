"""실험 branch bootstrap migration의 컬럼 대칭성을 검증한다.

전체 파이프라인에서 이슈 발행 전에 고정되는 기준 SHA와 launcher의 Job 생성 복구
좌표가 DB schema에 추가되는 경계만 담당한다. 발행 순서와 Job 실행은 검증하지 않는다.
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
    """Alembic op 호출을 실행 순서대로 기록하는 테스트 대역."""

    def __init__(self) -> None:
        self.added_columns: list[tuple[str, str, str, bool]] = []
        self.dropped_columns: list[tuple[str, str]] = []

    def _add_column(self, table: str, column: Column[object]) -> None:
        self.added_columns.append(
            (table, str(column.name), str(column.type), bool(column.nullable))
        )

    def _drop_column(self, table: str, column: str) -> None:
        self.dropped_columns.append((table, column))

    def run(self, operation) -> None:
        """revision 함수를 실제 DB 없이 Alembic op 대역으로 실행한다."""
        with (
            patch("alembic.op.add_column", self._add_column),
            patch("alembic.op.drop_column", self._drop_column),
        ):
            operation()


@pytest.fixture
def recorder() -> MigrationRecorder:
    return MigrationRecorder()


def test_upgrade_adds_branch_bootstrap_columns(recorder: MigrationRecorder) -> None:
    revision = load_revision("0004_experiment_branch_bootstrap")
    recorder.run(revision.upgrade)
    assert recorder.added_columns == [
        ("experiments", "base_dev_sha", "VARCHAR(40)", True),
        ("experiments", "executor_job_name", "VARCHAR(63)", True),
        ("experiments", "executor_job_created_at", "DATETIME", True),
    ]


def test_downgrade_removes_branch_bootstrap_columns_in_reverse_order(
    recorder: MigrationRecorder,
) -> None:
    revision = load_revision("0004_experiment_branch_bootstrap")
    recorder.run(revision.downgrade)
    assert recorder.dropped_columns == [
        ("experiments", "executor_job_created_at"),
        ("experiments", "executor_job_name"),
        ("experiments", "base_dev_sha"),
    ]
