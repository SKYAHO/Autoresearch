from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto_research_issue_branch import (  # noqa: E402
    branch_name_for,
    is_descendant,
    parse_issue_input,
)


def valid_body(
    *,
    minimum_primary_delta: str = "+0.002",
    test_size: str = "0.2",
    val_size: str = "0.2",
    seeds: str = "42, 43, 44",
    seed_count: str = "3",
    allowed_scope: str = "- [ ] prod 모델 계약(`src/features/model_contract.py`) 수정을 허용한다",
) -> str:
    return f"""## 연구 가설
비율 피처가 ROC-AUC를 높인다.

## 변경할 피처 · 모델
- 추가 피처: views_per_day = views / (days + 1)

## 성공 기준 — 주 지표 1개와 수치 임계
held-out test ROC-AUC, baseline 대비 {minimum_primary_delta} 이상

## 보조 관측 지표
PR-AUC

## 비교 대상
동일 조건 baseline 재학습 (권장)

## 재현 조건 고정값
- test_size / val_size: {test_size} / {val_size}
- 시드 목록: {seeds}
- 반복 시드 수: {seed_count}

## 대상 데이터 · 기간
- 데이터셋 / 경로: data/train.csv
- 기간 (KST YYYY-MM-DD ~ YYYY-MM-DD): 2026-07-01 ~ 2026-07-31

## 스냅샷 재사용
허용 (진행하되 실제로 쓴 데이터를 결과에 명시)

## 허용 범위
{allowed_scope}

## 결과 (에이전트가 채웁니다)
- 판정 (지지/기각):
"""


def test_parse_issue_input_rejects_nan_delta() -> None:
    with pytest.raises(ValueError, match="minimum_primary_delta"):
        parse_issue_input(449, "[AR] metric", valid_body(minimum_primary_delta="NaN"))


def test_parse_issue_input_reads_delta_after_metric_name_with_number() -> None:
    body = valid_body().replace(
        "held-out test ROC-AUC, baseline 대비 +0.002 이상",
        "held-out test F1-score, baseline 대비 +0.002 이상",
    )

    assert parse_issue_input(449, "[AR] metric", body).minimum_primary_delta == 0.002


def test_parse_issue_input_rejects_nan_after_metric_name_with_number() -> None:
    body = valid_body(minimum_primary_delta="NaN").replace(
        "held-out test ROC-AUC, baseline 대비 NaN 이상",
        "held-out test F1-score, baseline 대비 NaN 이상",
    )

    with pytest.raises(ValueError, match="minimum_primary_delta"):
        parse_issue_input(449, "[AR] metric", body)


def test_parse_issue_input_rejects_delta_that_overflows_float() -> None:
    with pytest.raises(ValueError, match="minimum_primary_delta"):
        parse_issue_input(449, "[AR] metric", valid_body(minimum_primary_delta="1" + "0" * 400))


def test_branch_name_is_single_issue_coordinate() -> None:
    assert branch_name_for(449, "[AR] CTR ratio") == "exp/449-ctr-ratio"


def test_branch_name_uses_deterministic_ascii_fallback_for_korean_title() -> None:
    assert branch_name_for(449, "[AR] 비율 피처 실험") == "exp/449-issue-09a97f67112d"


@pytest.mark.parametrize("minimum_primary_delta", ["Infinity", "-Infinity"])
def test_parse_issue_input_rejects_non_finite_primary_delta(
    minimum_primary_delta: str,
) -> None:
    with pytest.raises(ValueError, match="minimum_primary_delta"):
        parse_issue_input(449, "[AR] metric", valid_body(minimum_primary_delta=minimum_primary_delta))


def test_parse_issue_input_reads_required_headings_and_identifiers() -> None:
    issue_input = parse_issue_input(449, "[AR] CTR ratio", valid_body())

    assert issue_input.issue_branch == "exp/449-ctr-ratio"
    assert issue_input.success_criteria == "held-out test ROC-AUC, baseline 대비 +0.002 이상"
    assert issue_input.minimum_primary_delta == 0.002
    assert issue_input.test_size == 0.2
    assert issue_input.val_size == 0.2
    assert issue_input.seeds == (42, 43, 44)
    assert issue_input.allowed_scope == ()
    assert len(issue_input.criteria_id) == 64
    assert len(issue_input.reproducibility_id) == 64


def test_parse_issue_input_rejects_missing_required_heading() -> None:
    body = valid_body().replace("## 비교 대상\n동일 조건 baseline 재학습 (권장)\n\n", "")

    with pytest.raises(ValueError, match="comparison"):
        parse_issue_input(449, "[AR] metric", body)


@pytest.mark.parametrize(
    ("test_size", "val_size"),
    [("0", "0.2"), ("0.2", "0"), ("1", "0.2"), ("0.7", "0.3")],
)
def test_parse_issue_input_rejects_invalid_split_sizes(test_size: str, val_size: str) -> None:
    with pytest.raises(ValueError, match="test_size / val_size"):
        parse_issue_input(449, "[AR] metric", valid_body(test_size=test_size, val_size=val_size))


def test_parse_issue_input_rejects_seed_count_mismatch() -> None:
    with pytest.raises(ValueError, match="seed_count"):
        parse_issue_input(449, "[AR] metric", valid_body(seeds="42, 43", seed_count="3"))


def test_parse_issue_input_rejects_duplicate_reproducibility_key() -> None:
    body = valid_body().replace(
        "- 반복 시드 수: 3",
        "- 반복 시드 수: 3\n- 반복 시드 수: 999",
    )

    with pytest.raises(ValueError, match="reproducibility"):
        parse_issue_input(449, "[AR] metric", body)


def test_parse_issue_input_rejects_unknown_reproducibility_row() -> None:
    body = valid_body().replace(
        "- 반복 시드 수: 3",
        "- 반복 시드 수: 3\n- 알 수 없는 고정값: 1",
    )

    with pytest.raises(ValueError, match="reproducibility"):
        parse_issue_input(449, "[AR] metric", body)


def test_parse_issue_input_rejects_duplicate_or_non_integer_seed() -> None:
    with pytest.raises(ValueError, match="seeds"):
        parse_issue_input(449, "[AR] metric", valid_body(seeds="42, 42, three"))


def test_parse_issue_input_rejects_unknown_selected_guardrail() -> None:
    body = valid_body(
        allowed_scope="- [x] 배포 워크플로우를 수정한다"
    )

    with pytest.raises(ValueError, match="allowed_scope"):
        parse_issue_input(449, "[AR] metric", body)


def test_identifiers_are_deterministic_and_bound_to_their_contracts() -> None:
    original = parse_issue_input(449, "[AR] metric", valid_body())
    changed_criteria = parse_issue_input(449, "[AR] metric", valid_body(minimum_primary_delta="+0.003"))
    changed_reproducibility = parse_issue_input(449, "[AR] metric", valid_body(seeds="45, 46, 47"))

    assert parse_issue_input(449, "[AR] metric", valid_body()).criteria_id == original.criteria_id
    assert changed_criteria.criteria_id != original.criteria_id
    assert changed_reproducibility.criteria_id == original.criteria_id
    assert changed_reproducibility.reproducibility_id != original.reproducibility_id
    assert changed_criteria.reproducibility_id == original.reproducibility_id


@pytest.mark.parametrize(
    ("compare_status", "expected"),
    [("ahead", True), ("identical", True), ("behind", False), ("diverged", False)],
)
def test_is_descendant_accepts_only_ancestor_safe_statuses(
    compare_status: str, expected: bool
) -> None:
    assert is_descendant(compare_status) is expected


def test_cli_writes_only_issue_contract_outputs(tmp_path: Path) -> None:
    body_path = tmp_path / "issue.md"
    output_path = tmp_path / "github-output.txt"
    body_path.write_text(valid_body(), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "tools/auto_research_issue_branch.py",
            "--issue-number",
            "449",
            "--issue-title",
            "[AR] CTR ratio",
            "--issue-body-file",
            str(body_path),
        ],
        check=False,
        capture_output=True,
        cwd=PROJECT_ROOT,
        env={**os.environ, "GITHUB_OUTPUT": str(output_path)},
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert output_path.read_text(encoding="utf-8").splitlines() == [
        "issue_branch=exp/449-ctr-ratio",
        "criteria_id=" + parse_issue_input(449, "[AR] CTR ratio", valid_body()).criteria_id,
        "reproducibility_id="
        + parse_issue_input(449, "[AR] CTR ratio", valid_body()).reproducibility_id,
    ]
