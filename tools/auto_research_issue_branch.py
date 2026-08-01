"""Auto Research 이슈 입력을 실행 전 계약으로 정규화하는 순수 도구입니다.

이 모듈은 Autoresearch 일일 폐루프의 자율 실험 진입점에서 GitHub Issue Form
본문을 fail-closed 방식으로 검증하고, 이슈별 실험 브랜치와 판정·재현 계약
식별자를 만듭니다. 후보 스냅샷 생성, 실험 실행 context, GitHub workflow
제어, Issue Form 변경 및 champion 승격은 이 모듈의 책임이 아닙니다.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Sequence


_HEADING_NAMES = {
    "연구 가설": "hypothesis",
    "변경할 피처 · 모델": "change",
    "성공 기준 — 주 지표 1개와 수치 임계": "success_criteria",
    "보조 관측 지표": "secondary_metrics",
    "비교 대상": "comparison",
    "재현 조건 고정값": "reproducibility",
    "대상 데이터 · 기간": "dataset",
    "스냅샷 재사용": "snapshot_reuse",
    "허용 범위": "allowed_scope",
    "결과 (에이전트가 채웁니다)": "result",
}
_REQUIRED_SECTIONS = frozenset(_HEADING_NAMES) - {"보조 관측 지표", "결과 (에이전트가 채웁니다)"}
_COMPARISONS = frozenset(
    {
        "동일 조건 baseline 재학습 (권장)",
        "champion (ctr-model@champion)",
        "둘 다",
    }
)
_SNAPSHOT_REUSE = frozenset(
    {
        "허용 (진행하되 실제로 쓴 데이터를 결과에 명시)",
        "불허 (정규 조립 경로 실패 시 중단)",
    }
)
_SCOPE_LABELS = {
    "prod 모델 계약(`src/features/model_contract.py`) 수정을 허용한다": "prod_model_contract",
    "Feast 정의(`feature_repo/`) 수정을 허용한다": "feast_definition",
    "실험 결과를 champion으로 승격하는 것까지 검토한다": "promotion",
}
_SECTION_PATTERN = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_DECIMAL_PATTERN = re.compile(r"[+-]?(?:NaN|Infinity|\d+(?:\.\d*)?|\.\d+)")
_CHECKBOX_PATTERN = re.compile(r"^- \[([ xX])\]\s+(.+)$")


@dataclass(frozen=True)
class IssueInput:
    """검증된 Auto Research 이슈의 실행 전 불변 계약입니다."""

    issue_number: int
    issue_branch: str
    hypothesis: str
    change: str
    success_criteria: str
    secondary_metrics: str
    comparison: str
    minimum_primary_delta: float
    test_size: float
    val_size: float
    seeds: tuple[int, ...]
    dataset: str
    snapshot_reuse: str
    allowed_scope: tuple[str, ...]
    criteria_id: str
    reproducibility_id: str


def branch_name_for(issue_number: int, issue_title: str) -> str:
    """이슈 번호와 제목에서 이슈 하나에만 대응하는 Git branch 이름을 만듭니다."""
    if issue_number <= 0:
        raise ValueError("issue_number must be positive")

    title_without_prefix = re.sub(r"^\s*\[AR\]\s*", "", issue_title, flags=re.IGNORECASE)
    slug = re.sub(r"[^a-z0-9]+", "-", title_without_prefix.lower()).strip("-")
    if not slug:
        raise ValueError("issue_title must contain an ASCII branch slug")
    return f"exp/{issue_number}-{slug}"


def is_descendant(compare_status: str) -> bool:
    """GitHub compare 상태가 기준 SHA를 포함한 안전한 후손인지 판정합니다."""
    return compare_status in {"ahead", "identical"}


def parse_issue_input(issue_number: int, issue_title: str, issue_body: str) -> IssueInput:
    """Issue Form 본문을 파싱하고 실행에 필요한 계약만 fail-closed로 반환합니다."""
    sections = _parse_sections(issue_body)
    _validate_required_sections(sections)

    hypothesis = _required_content(sections, "연구 가설")
    change = _required_content(sections, "변경할 피처 · 모델")
    success_criteria = _required_content(sections, "성공 기준 — 주 지표 1개와 수치 임계")
    comparison = _required_content(sections, "비교 대상")
    reproducibility = _required_content(sections, "재현 조건 고정값")
    dataset = _required_content(sections, "대상 데이터 · 기간")
    snapshot_reuse = _required_content(sections, "스냅샷 재사용")
    allowed_scope_text = _required_content(sections, "허용 범위")

    if comparison not in _COMPARISONS:
        raise ValueError("comparison must be an Issue Form option")
    if snapshot_reuse not in _SNAPSHOT_REUSE:
        raise ValueError("snapshot_reuse must be an Issue Form option")

    minimum_primary_delta = _minimum_primary_delta(success_criteria)
    test_size, val_size, seeds = _parse_reproducibility(reproducibility)
    allowed_scope = _parse_allowed_scope(allowed_scope_text)
    issue_branch = branch_name_for(issue_number, issue_title)
    secondary_metrics = sections.get("보조 관측 지표", "").strip()

    criteria_id = _identifier(
        {
            "issue_number": issue_number,
            "success_criteria": success_criteria,
            "comparison": comparison,
            "minimum_primary_delta": _decimal_text(minimum_primary_delta),
        }
    )
    reproducibility_id = _identifier(
        {
            "issue_number": issue_number,
            "test_size": _decimal_text(test_size),
            "val_size": _decimal_text(val_size),
            "seeds": seeds,
            "dataset": dataset,
            "snapshot_reuse": snapshot_reuse,
        }
    )
    return IssueInput(
        issue_number=issue_number,
        issue_branch=issue_branch,
        hypothesis=hypothesis,
        change=change,
        success_criteria=success_criteria,
        secondary_metrics=secondary_metrics,
        comparison=comparison,
        minimum_primary_delta=float(minimum_primary_delta),
        test_size=float(test_size),
        val_size=float(val_size),
        seeds=seeds,
        dataset=dataset,
        snapshot_reuse=snapshot_reuse,
        allowed_scope=allowed_scope,
        criteria_id=criteria_id,
        reproducibility_id=reproducibility_id,
    )


def _parse_sections(issue_body: str) -> dict[str, str]:
    """최상위 Markdown heading 사이의 본문을 Form label로 인덱싱합니다."""
    matches = list(_SECTION_PATTERN.finditer(issue_body))
    if not matches:
        raise ValueError("issue_body must contain Issue Form headings")

    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        if heading not in _HEADING_NAMES:
            raise ValueError(f"unknown Issue Form heading: {heading}")
        if heading in sections:
            raise ValueError(f"duplicate Issue Form heading: {heading}")
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(issue_body)
        sections[heading] = issue_body[match.end() : next_start].strip()
    return sections


def _validate_required_sections(sections: dict[str, str]) -> None:
    """필수 Issue Form heading이 하나도 누락되지 않았는지 확인합니다."""
    for heading in sorted(_REQUIRED_SECTIONS):
        if heading not in sections:
            raise ValueError(f"missing required section: {_HEADING_NAMES[heading]}")


def _required_content(sections: dict[str, str], heading: str) -> str:
    """필수 section의 비어 있지 않은 본문을 반환합니다."""
    content = sections[heading].strip()
    if not content:
        raise ValueError(f"{_HEADING_NAMES[heading]} must not be empty")
    return content


def _minimum_primary_delta(success_criteria: str) -> Decimal:
    """성공 기준에서 유한하고 양수인 최소 주 지표 개선폭을 추출합니다."""
    match = _DECIMAL_PATTERN.search(success_criteria)
    if match is None:
        raise ValueError("minimum_primary_delta is required")
    value = _finite_decimal(match.group(), "minimum_primary_delta")
    if value <= 0:
        raise ValueError("minimum_primary_delta must be positive")
    return value


def _parse_reproducibility(reproducibility: str) -> tuple[Decimal, Decimal, tuple[int, ...]]:
    """재현 조건의 split과 시드 목록·개수를 교차 검증합니다."""
    split_match = re.search(
        r"^-\s*test_size\s*/\s*val_size:\s*(\S+)\s*/\s*(\S+)\s*$",
        reproducibility,
        flags=re.MULTILINE,
    )
    if split_match is None:
        raise ValueError("test_size / val_size is required")
    test_size = _finite_decimal(split_match.group(1), "test_size / val_size")
    val_size = _finite_decimal(split_match.group(2), "test_size / val_size")
    if not 0 < test_size < 1 or not 0 < val_size < 1 or test_size + val_size >= 1:
        raise ValueError("test_size / val_size must leave training data")

    seeds_match = re.search(r"^-\s*시드 목록:\s*(.+?)\s*$", reproducibility, flags=re.MULTILINE)
    if seeds_match is None:
        raise ValueError("seeds are required")
    seed_tokens = [token.strip() for token in seeds_match.group(1).split(",")]
    if not seed_tokens or any(not token.isdecimal() for token in seed_tokens):
        raise ValueError("seeds must be comma-separated non-negative integers")
    seeds = tuple(int(token) for token in seed_tokens)
    if len(set(seeds)) != len(seeds):
        raise ValueError("seeds must be unique")

    seed_count_match = re.search(r"^-\s*반복 시드 수:\s*(\d+)\s*$", reproducibility, flags=re.MULTILINE)
    if seed_count_match is None:
        raise ValueError("seed_count is required")
    if int(seed_count_match.group(1)) != len(seeds):
        raise ValueError("seed_count must match seeds")
    return test_size, val_size, seeds


def _finite_decimal(value: str, field_name: str) -> Decimal:
    """유한한 Decimal 문자열만 받아 숫자 경계 검증에 사용합니다."""
    try:
        decimal = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{field_name} must be a finite decimal") from error
    if not decimal.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal")
    return decimal


def _parse_allowed_scope(allowed_scope_text: str) -> tuple[str, ...]:
    """명시적으로 체크된 알려진 변경 경계만 실행 허용 범위로 반환합니다."""
    selected_scopes: list[str] = []
    checkbox_count = 0
    for line in allowed_scope_text.splitlines():
        match = _CHECKBOX_PATTERN.match(line.strip())
        if match is None:
            raise ValueError("allowed_scope must contain only Issue Form checkboxes")
        checkbox_count += 1
        label = match.group(2).strip()
        scope = _SCOPE_LABELS.get(label)
        if scope is None:
            raise ValueError("allowed_scope contains an unknown guardrail")
        if match.group(1).lower() == "x":
            selected_scopes.append(scope)
    if checkbox_count == 0:
        raise ValueError("allowed_scope must contain Issue Form checkboxes")
    return tuple(selected_scopes)


def _identifier(contract: dict[str, object]) -> str:
    """정렬된 계약 JSON의 SHA-256 식별자를 계산합니다."""
    canonical_contract = json.dumps(
        contract,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical_contract.encode("utf-8")).hexdigest()


def _decimal_text(value: Decimal) -> str:
    """식별자 입력에 사용할 Decimal의 안정적인 일반 표기를 반환합니다."""
    return format(value, "f")


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """CLI에서 이슈 메타데이터와 본문 파일 경로를 읽습니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--issue-title", required=True)
    parser.add_argument("--issue-body-file", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """검증 결과의 최소 GitHub Actions output contract를 기록합니다."""
    arguments = _parse_arguments(argv)
    issue_body = arguments.issue_body_file.read_text(encoding="utf-8")
    issue_input = parse_issue_input(arguments.issue_number, arguments.issue_title, issue_body)
    lines = (
        f"issue_branch={issue_input.issue_branch}",
        f"criteria_id={issue_input.criteria_id}",
        f"reproducibility_id={issue_input.reproducibility_id}",
    )
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with Path(github_output).open("a", encoding="utf-8") as output_file:
            output_file.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
