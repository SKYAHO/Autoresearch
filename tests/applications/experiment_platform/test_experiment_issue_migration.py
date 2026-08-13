"""0002 revision이 모델과 같은 lineage 컬럼을 만드는지 고정한다.

전체 파이프라인에서 실험 영속화 schema의 migration-모델 정합성만 검증한다. 발행 절차와
HTTP 계약은 이 모듈의 범위가 아니다.
"""

from __future__ import annotations

from pathlib import Path
import re

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REVISION = (
    PROJECT_ROOT
    / "applications" / "experiment_platform"
    / "migrations"
    / "versions"
    / "0003_experiment_issue_lineage.py"
)
MODELS = (
    PROJECT_ROOT / "applications" / "experiment_platform" / "api" / "experiments" / "models.py"
)

LINEAGE_COLUMNS = (
    "issue_body",
    "issue_title",
    "issue_number",
    "issue_branch",
    "issue_published_at",
)


def test_revision_chains_to_the_step_revision() -> None:
    """revision 체인이 끊기면 배포 시 마이그레이션이 적용되지 않는다."""
    text = REVISION.read_text(encoding="utf-8")

    assert 'revision = "0003_experiment_issue_lineage"' in text
    assert 'down_revision = "0002_experiment_steps"' in text


def test_upgrade_and_downgrade_are_symmetric() -> None:
    """downgrade가 upgrade가 만든 것을 모두 되돌린다."""
    text = REVISION.read_text(encoding="utf-8")
    added = set(re.findall(r'op\.add_column\(\s*"experiments",\s*sa\.Column\(\s*"(\w+)"', text))
    dropped = set(re.findall(r'op\.drop_column\(\s*"experiments",\s*"(\w+)"', text))

    assert added == set(LINEAGE_COLUMNS)
    assert added == dropped
    assert 'op.create_index("ix_experiments_issue_number"' in text
    assert 'op.drop_index("ix_experiments_issue_number"' in text


def test_model_declares_the_same_lineage_columns() -> None:
    """migration만 바뀌고 모델이 남는 드리프트를 잡는다."""
    text = MODELS.read_text(encoding="utf-8")

    for column in LINEAGE_COLUMNS:
        assert f"{column}: Mapped[" in text
    assert 'Index("ix_experiments_issue_number", "issue_number")' in text
