"""Auto Research 이슈의 구조화 실행 전 계약을 정규화하는 순수 도구입니다.

[파이프라인] 자율 실험 진입점에서 GitHub Issue Form 등록과 이슈별 실험 실행
사이 — 지표 판정과 데이터·seed·split·학습 설정 재현 조건을 검증하는 구간을
담당합니다.

[기능] 실제 Issue Form heading 본문을 fail-closed 방식으로 파싱하고, 이슈별
실험 브랜치 이름과 판정·재현 계약 식별자 및 GitHub Actions output을 만듭니다.
완료 event 후보의 versioned schema·계약 식별자·지표를 검증해 dev 병합 후보를
순수하게 선택합니다.

[비책임] 후보 스냅샷 생성·실험 실행 context·GitHub ref 제어·실제 dev 병합·
champion 승격은 후속 #449 workflow와 실험 실행 계층의 책임입니다.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
from typing import Mapping, Sequence


_HEADING_NAMES = {
    "연구 가설": "hypothesis",
    "변경할 피처 · 모델": "change",
    "주 지표 이름": "primary_metric_name",
    "주 지표 방향": "primary_metric_direction",
    "최소 주 지표 개선폭": "minimum_primary_delta",
    "Guardrail 지표 이름": "guardrail_metric_name",
    "Guardrail 지표 방향": "guardrail_metric_direction",
    "최대 Guardrail 악화폭": "maximum_guardrail_regression",
    "보조 관측 지표": "secondary_metrics",
    "비교 대상": "comparison",
    "데이터셋 스냅샷": "dataset_snapshot",
    "랜덤 시드 목록": "random_seeds",
    "Split 시드": "split_seed",
    "Test 비율": "test_size",
    "Validation 비율": "validation_size",
    "학습 설정 참조": "training_config_ref",
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
_METRIC_DIRECTIONS = frozenset({"higher_is_better", "lower_is_better"})
_NOT_APPLICABLE = "not_applicable"
_NONE_VALUE = "없음"
_SCOPE_LABELS = {
    "prod 모델 계약(`src/features/model_contract.py`) 수정을 허용한다": "prod_model_contract",
    "Feast 정의(`feature_repo/`) 수정을 허용한다": "feast_definition",
    "실험 결과를 champion으로 승격하는 것까지 검토한다": "promotion",
}
_SECTION_PATTERN = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_CHECKBOX_PATTERN = re.compile(r"^- \[([ xX])\]\s+(.+)$")
_METRIC_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
_NON_NEGATIVE_INTEGER_PATTERN = re.compile(r"^[0-9]+$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_IDENTIFIER_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_EXPERIMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CANDIDATE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
COMPLETION_CANDIDATE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class IssueInput:
    """검증된 Auto Research 이슈의 실행 전 불변 계약입니다."""

    issue_number: int
    issue_branch: str
    hypothesis: str
    change: str
    primary_metric_name: str
    primary_metric_direction: str
    minimum_primary_delta: float
    guardrail_metric_name: str | None
    guardrail_metric_direction: str
    maximum_guardrail_regression: float | None
    secondary_metrics: str
    comparison: str
    dataset_snapshot: str
    random_seeds: tuple[int, ...]
    split_seed: int
    test_size: float
    validation_size: float
    training_config_ref: str
    dataset: str
    snapshot_reuse: str
    allowed_scope: tuple[str, ...]
    criteria_id: str
    reproducibility_id: str


@dataclass(frozen=True)
class CompletionCandidate:
    """완료 event의 하나의 schema-versioned 후보 결과입니다."""

    candidate_id: str
    candidate_sha: str
    primary_candidate_metric: Decimal
    primary_baseline_metric: Decimal
    artifact_uri: str
    log_uri: str
    guardrail_candidate_metric: Decimal | None
    guardrail_baseline_metric: Decimal | None

    def canonical_contract(self) -> dict[str, str | int | None]:
        """순서 독립 result-set 식별자에 사용할 canonical 후보 표현을 반환합니다."""
        return {
            "schema_version": COMPLETION_CANDIDATE_SCHEMA_VERSION,
            "candidate_id": self.candidate_id,
            "candidate_sha": self.candidate_sha,
            "primary_candidate_metric": _decimal_text(self.primary_candidate_metric),
            "primary_baseline_metric": _decimal_text(self.primary_baseline_metric),
            "guardrail_candidate_metric": (
                _decimal_text(self.guardrail_candidate_metric)
                if self.guardrail_candidate_metric is not None
                else None
            ),
            "guardrail_baseline_metric": (
                _decimal_text(self.guardrail_baseline_metric)
                if self.guardrail_baseline_metric is not None
                else None
            ),
            "artifact_uri": self.artifact_uri,
            "log_uri": self.log_uri,
        }


@dataclass(frozen=True)
class CandidateSelection:
    """완료 후보 집합의 결정론적 selector 결과입니다."""

    schema_version: int
    result_set_id: str
    selection_reason: str
    candidate_sha: str | None
    candidate_id: str | None


def branch_name_for(issue_number: int, issue_title: str) -> str:
    """이슈 번호와 제목에서 이슈 하나에만 대응하는 Git branch 이름을 만듭니다."""
    if issue_number <= 0:
        raise ValueError("issue_number must be positive")

    title_without_prefix = re.sub(r"^\s*\[AR\]\s*", "", issue_title, flags=re.IGNORECASE)
    slug = re.sub(r"[^a-z0-9]+", "-", title_without_prefix.lower()).strip("-")
    if not slug:
        if not title_without_prefix.strip():
            raise ValueError("issue_title must not be empty")
        title_digest = hashlib.sha256(title_without_prefix.encode("utf-8")).hexdigest()[:12]
        slug = f"issue-{title_digest}"
    return f"exp/{issue_number}-{slug}"


def is_descendant(compare_status: str) -> bool:
    """GitHub compare 상태가 기준 SHA를 포함한 안전한 후손인지 판정합니다."""
    return compare_status in {"ahead", "identical"}


def select_best_candidate(
    criteria: IssueInput,
    experiment_id: str,
    base_dev_sha: str,
    candidates: Sequence[Mapping[str, object]],
) -> CandidateSelection:
    """완전한 완료 후보 집합에서 기준을 만족하는 최선 후보 하나를 고릅니다.

    Args:
        criteria: source issue에서 다시 파싱한 불변 기준·재현 계약입니다.
        experiment_id: 완료 event를 식별하는 단일 실험 ID입니다.
        base_dev_sha: issue branch marker에 기록된 40자 dev 기준 SHA입니다.
        candidates: schema version 1의 완료 후보 전체 집합입니다.

    Returns:
        적격 후보 SHA 또는 정상적인 ``no_qualified_candidate`` 결과입니다.

    Raises:
        ValueError: 하나라도 schema·좌표·식별자·수치 계약을 위반할 때 발생합니다.
    """
    _validate_event_identifier(experiment_id, "experiment_id", _EXPERIMENT_ID_PATTERN)
    _validate_event_identifier(base_dev_sha, "base_dev_sha", _SHA_PATTERN)
    if not candidates:
        raise ValueError("candidates must contain at least one candidate")

    parsed_candidates = tuple(
        _parse_completion_candidate(candidate, criteria, experiment_id, base_dev_sha)
        for candidate in candidates
    )
    _validate_candidate_set(parsed_candidates)
    result_set_id = _result_set_id(criteria, experiment_id, base_dev_sha, parsed_candidates)
    qualified = tuple(
        candidate
        for candidate in parsed_candidates
        if _is_qualified_candidate(candidate, criteria)
    )
    if not qualified:
        return CandidateSelection(
            schema_version=COMPLETION_CANDIDATE_SCHEMA_VERSION,
            result_set_id=result_set_id,
            selection_reason="no_qualified_candidate",
            candidate_sha=None,
            candidate_id=None,
        )

    selected = _best_qualified_candidate(qualified, criteria.primary_metric_direction)
    return CandidateSelection(
        schema_version=COMPLETION_CANDIDATE_SCHEMA_VERSION,
        result_set_id=result_set_id,
        selection_reason="qualified_candidate",
        candidate_sha=selected.candidate_sha,
        candidate_id=selected.candidate_id,
    )


def _parse_completion_candidate(
    candidate: Mapping[str, object],
    criteria: IssueInput,
    experiment_id: str,
    base_dev_sha: str,
) -> CompletionCandidate:
    """외부 completion event 후보 하나를 정확한 schema로 검증합니다."""
    if not isinstance(candidate, Mapping):
        raise ValueError("candidate must be an object")
    expected_keys = {
        "schema_version",
        "issue_number",
        "experiment_id",
        "base_dev_sha",
        "candidate_id",
        "candidate_sha",
        "criteria_id",
        "reproducibility_id",
        "primary_candidate_metric",
        "primary_baseline_metric",
        "artifact_uri",
        "log_uri",
    }
    if criteria.guardrail_metric_name is not None:
        expected_keys.update(
            {"guardrail_candidate_metric", "guardrail_baseline_metric"}
        )
    actual_keys = set(candidate)
    unknown_keys = actual_keys - expected_keys
    missing_keys = expected_keys - actual_keys
    if unknown_keys:
        raise ValueError("unknown candidate keys: " + ", ".join(sorted(unknown_keys)))
    if missing_keys:
        raise ValueError("missing candidate keys: " + ", ".join(sorted(missing_keys)))
    if candidate["schema_version"] != COMPLETION_CANDIDATE_SCHEMA_VERSION or isinstance(
        candidate["schema_version"], bool
    ):
        raise ValueError("schema_version must be 1")
    if candidate["issue_number"] != criteria.issue_number or isinstance(
        candidate["issue_number"], bool
    ):
        raise ValueError("issue_number does not match issue criteria")
    _validate_equal_identifier(candidate["experiment_id"], experiment_id, "experiment_id", _EXPERIMENT_ID_PATTERN)
    _validate_equal_identifier(candidate["base_dev_sha"], base_dev_sha, "base_dev_sha", _SHA_PATTERN)
    _validate_equal_identifier(candidate["criteria_id"], criteria.criteria_id, "criteria_id", _IDENTIFIER_PATTERN)
    _validate_equal_identifier(
        candidate["reproducibility_id"],
        criteria.reproducibility_id,
        "reproducibility_id",
        _IDENTIFIER_PATTERN,
    )
    candidate_id = _validated_string(candidate["candidate_id"], "candidate_id", _CANDIDATE_ID_PATTERN)
    candidate_sha = _validated_string(candidate["candidate_sha"], "candidate_sha", _SHA_PATTERN)
    primary_candidate_metric = _event_decimal(
        candidate["primary_candidate_metric"], "primary_candidate_metric"
    )
    primary_baseline_metric = _event_decimal(
        candidate["primary_baseline_metric"], "primary_baseline_metric"
    )
    artifact_uri = _single_line_reference(candidate["artifact_uri"], "artifact_uri")
    log_uri = _single_line_reference(candidate["log_uri"], "log_uri")

    guardrail_candidate_metric: Decimal | None = None
    guardrail_baseline_metric: Decimal | None = None
    if criteria.guardrail_metric_name is not None:
        guardrail_candidate_metric = _event_decimal(
            candidate["guardrail_candidate_metric"], "guardrail_candidate_metric"
        )
        guardrail_baseline_metric = _event_decimal(
            candidate["guardrail_baseline_metric"], "guardrail_baseline_metric"
        )
    return CompletionCandidate(
        candidate_id=candidate_id,
        candidate_sha=candidate_sha,
        primary_candidate_metric=primary_candidate_metric,
        primary_baseline_metric=primary_baseline_metric,
        artifact_uri=artifact_uri,
        log_uri=log_uri,
        guardrail_candidate_metric=guardrail_candidate_metric,
        guardrail_baseline_metric=guardrail_baseline_metric,
    )


def _validate_event_identifier(value: str, field_name: str, pattern: re.Pattern[str]) -> None:
    """이벤트 좌표로 쓰는 문자열의 타입·형식을 fail-closed로 검사합니다."""
    _validated_string(value, field_name, pattern)


def _validate_equal_identifier(
    value: object,
    expected: str,
    field_name: str,
    pattern: re.Pattern[str],
) -> None:
    """후보의 문자열 식별자가 검증된 event 좌표와 정확히 같은지 검사합니다."""
    parsed = _validated_string(value, field_name, pattern)
    if parsed != expected:
        raise ValueError(f"{field_name} does not match validated event input")


def _validated_string(value: object, field_name: str, pattern: re.Pattern[str]) -> str:
    """정규식 계약을 만족하는 단일 문자열을 반환합니다."""
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{field_name} has an invalid format")
    return value


def _event_decimal(value: object, field_name: str) -> Decimal:
    """completion schema의 공백 없는 유한 Decimal 문자열만 허용합니다."""
    if not isinstance(value, str) or not value or any(character.isspace() for character in value):
        raise ValueError(f"{field_name} must be a finite decimal string")
    return _finite_decimal(value, field_name)


def _single_line_reference(value: object, field_name: str) -> str:
    """artifact/log의 비어 있지 않은 단일 줄 식별자를 반환합니다."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\n" in value
        or "\r" in value
    ):
        raise ValueError(f"{field_name} must be a non-empty single-line reference")
    return value


def _validate_candidate_set(candidates: Sequence[CompletionCandidate]) -> None:
    """전체 후보에서 중복 SHA/ID와 baseline 불일치를 발견하면 실패합니다."""
    candidate_shas = tuple(candidate.candidate_sha for candidate in candidates)
    if len(set(candidate_shas)) != len(candidate_shas):
        raise ValueError("candidate_sha must be unique across the complete candidate set")
    candidate_ids = tuple(candidate.candidate_id for candidate in candidates)
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ValueError("candidate_id must be unique across the complete candidate set")
    if len({candidate.primary_baseline_metric for candidate in candidates}) != 1:
        raise ValueError("primary_baseline_metric must be canonically equal across candidates")
    guardrail_baselines = tuple(
        candidate.guardrail_baseline_metric for candidate in candidates
    )
    if guardrail_baselines[0] is not None and len(set(guardrail_baselines)) != 1:
        raise ValueError("guardrail_baseline_metric must be canonically equal across candidates")


def _result_set_id(
    criteria: IssueInput,
    experiment_id: str,
    base_dev_sha: str,
    candidates: Sequence[CompletionCandidate],
) -> str:
    """입력 후보 순서와 동치 Decimal 표기에 독립적인 결과 집합 ID를 계산합니다."""
    contract = {
        "schema_version": COMPLETION_CANDIDATE_SCHEMA_VERSION,
        "issue_number": criteria.issue_number,
        "experiment_id": experiment_id,
        "base_dev_sha": base_dev_sha,
        "criteria_id": criteria.criteria_id,
        "reproducibility_id": criteria.reproducibility_id,
        "candidates": [
            candidate.canonical_contract()
            for candidate in sorted(candidates, key=lambda candidate: candidate.candidate_sha)
        ],
    }
    return _identifier(contract)


def _is_qualified_candidate(candidate: CompletionCandidate, criteria: IssueInput) -> bool:
    """주 지표 최소 개선과 optional guardrail 최대 악화를 모두 판정합니다."""
    primary_delta = (
        candidate.primary_candidate_metric - candidate.primary_baseline_metric
        if criteria.primary_metric_direction == "higher_is_better"
        else candidate.primary_baseline_metric - candidate.primary_candidate_metric
    )
    if primary_delta < Decimal(str(criteria.minimum_primary_delta)):
        return False
    if criteria.guardrail_metric_name is None:
        return True

    assert candidate.guardrail_candidate_metric is not None
    assert candidate.guardrail_baseline_metric is not None
    regression = (
        candidate.guardrail_baseline_metric - candidate.guardrail_candidate_metric
        if criteria.guardrail_metric_direction == "higher_is_better"
        else candidate.guardrail_candidate_metric - candidate.guardrail_baseline_metric
    )
    assert criteria.maximum_guardrail_regression is not None
    return regression <= Decimal(str(criteria.maximum_guardrail_regression))


def _best_qualified_candidate(
    candidates: Sequence[CompletionCandidate],
    primary_direction: str,
) -> CompletionCandidate:
    """방향을 반영한 primary metric과 SHA 오름차순 동률 규칙으로 하나를 반환합니다."""
    if primary_direction == "higher_is_better":
        return min(
            candidates,
            key=lambda candidate: (-candidate.primary_candidate_metric, candidate.candidate_sha),
        )
    return min(
        candidates,
        key=lambda candidate: (candidate.primary_candidate_metric, candidate.candidate_sha),
    )


def parse_issue_input(issue_number: int, issue_title: str, issue_body: str) -> IssueInput:
    """Issue Form 본문을 파싱하고 실행에 필요한 계약만 fail-closed로 반환합니다."""
    sections = _parse_sections(issue_body)
    _validate_required_sections(sections)

    hypothesis = _required_content(sections, "연구 가설")
    change = _required_content(sections, "변경할 피처 · 모델")
    primary_metric_name = _metric_name(
        _required_content(sections, "주 지표 이름"),
        "primary_metric_name",
    )
    primary_metric_direction = _metric_direction(
        _required_content(sections, "주 지표 방향"),
        "primary_metric_direction",
    )
    minimum_primary_delta = _non_negative_decimal(
        _required_content(sections, "최소 주 지표 개선폭"),
        "minimum_primary_delta",
    )
    guardrail_metric_name, guardrail_metric_direction, maximum_guardrail_regression = (
        _parse_guardrail(
            _required_content(sections, "Guardrail 지표 이름"),
            _required_content(sections, "Guardrail 지표 방향"),
            _required_content(sections, "최대 Guardrail 악화폭"),
        )
    )
    comparison = _required_content(sections, "비교 대상")
    dataset_snapshot = _text_reference(
        _required_content(sections, "데이터셋 스냅샷"),
        "dataset_snapshot",
    )
    random_seeds = _parse_random_seeds(_required_content(sections, "랜덤 시드 목록"))
    split_seed = _non_negative_integer(
        _required_content(sections, "Split 시드"),
        "split_seed",
    )
    test_size, validation_size = _parse_split_sizes(
        _required_content(sections, "Test 비율"),
        _required_content(sections, "Validation 비율"),
    )
    training_config_ref = _text_reference(
        _required_content(sections, "학습 설정 참조"),
        "training_config_ref",
    )
    dataset = _required_content(sections, "대상 데이터 · 기간")
    snapshot_reuse = _required_content(sections, "스냅샷 재사용")
    allowed_scope_text = _required_content(sections, "허용 범위")

    if comparison not in _COMPARISONS:
        raise ValueError("comparison must be an Issue Form option")
    if snapshot_reuse not in _SNAPSHOT_REUSE:
        raise ValueError("snapshot_reuse must be an Issue Form option")

    allowed_scope = _parse_allowed_scope(allowed_scope_text)
    issue_branch = branch_name_for(issue_number, issue_title)
    secondary_metrics = sections.get("보조 관측 지표", "").strip()

    criteria_id = _identifier(
        {
            "primary_metric_name": primary_metric_name,
            "primary_metric_direction": primary_metric_direction,
            "minimum_primary_delta": _decimal_text(minimum_primary_delta),
            "guardrail_metric_name": guardrail_metric_name or _NONE_VALUE,
            "guardrail_metric_direction": guardrail_metric_direction,
            "maximum_guardrail_regression": (
                _decimal_text(maximum_guardrail_regression)
                if maximum_guardrail_regression is not None
                else _NONE_VALUE
            ),
        }
    )
    reproducibility_id = _identifier(
        {
            "dataset_snapshot": dataset_snapshot,
            "random_seeds": random_seeds,
            "split_seed": split_seed,
            "test_size": _decimal_text(test_size),
            "validation_size": _decimal_text(validation_size),
            "training_config_ref": training_config_ref,
        }
    )
    return IssueInput(
        issue_number=issue_number,
        issue_branch=issue_branch,
        hypothesis=hypothesis,
        change=change,
        primary_metric_name=primary_metric_name,
        primary_metric_direction=primary_metric_direction,
        minimum_primary_delta=_finite_float(minimum_primary_delta, "minimum_primary_delta"),
        guardrail_metric_name=guardrail_metric_name,
        guardrail_metric_direction=guardrail_metric_direction,
        maximum_guardrail_regression=(
            _finite_float(maximum_guardrail_regression, "maximum_guardrail_regression")
            if maximum_guardrail_regression is not None
            else None
        ),
        secondary_metrics=secondary_metrics,
        comparison=comparison,
        dataset_snapshot=dataset_snapshot,
        random_seeds=random_seeds,
        split_seed=split_seed,
        test_size=_finite_float(test_size, "test_size"),
        validation_size=_finite_float(validation_size, "validation_size"),
        training_config_ref=training_config_ref,
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


def _metric_name(value: str, field_name: str) -> str:
    """Issue Form metric 이름 규칙에 맞는 값을 반환합니다."""
    if _METRIC_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must match [A-Za-z][A-Za-z0-9._-]{{0,63}}")
    return value


def _metric_direction(value: str, field_name: str) -> str:
    """비교 가능한 metric 방향 두 값 중 하나만 반환합니다."""
    if value not in _METRIC_DIRECTIONS:
        raise ValueError(f"{field_name} must be an Issue Form direction")
    return value


def _non_negative_decimal(value: str, field_name: str) -> Decimal:
    """유한하고 0 이상이며 공개 float 범위에 드는 Decimal을 반환합니다."""
    decimal = _finite_decimal(value, field_name)
    if decimal < 0:
        raise ValueError(f"{field_name} must be non-negative")
    _finite_float(decimal, field_name)
    return decimal


def _parse_guardrail(
    name: str,
    direction: str,
    maximum_regression: str,
) -> tuple[str | None, str, Decimal | None]:
    """Guardrail 미사용 sentinel 또는 완전한 metric 계약만 반환합니다."""
    if name == _NONE_VALUE:
        if direction != _NOT_APPLICABLE or maximum_regression != _NONE_VALUE:
            raise ValueError(
                "guardrail without a metric must use 없음/not_applicable/없음"
            )
        return None, direction, None

    metric_name = _metric_name(name, "guardrail_metric_name")
    if direction == _NOT_APPLICABLE:
        raise ValueError("guardrail_metric_direction must compare a configured metric")
    metric_direction = _metric_direction(direction, "guardrail_metric_direction")
    if maximum_regression == _NONE_VALUE:
        raise ValueError("maximum_guardrail_regression is required for a guardrail")
    regression = _non_negative_decimal(
        maximum_regression,
        "maximum_guardrail_regression",
    )
    return metric_name, metric_direction, regression


def _text_reference(value: str, field_name: str) -> str:
    """1~256자의 비어 있지 않은 식별자·참조 문자열을 반환합니다."""
    if not 1 <= len(value) <= 256:
        raise ValueError(f"{field_name} must contain 1 to 256 characters")
    return value


def _parse_random_seeds(value: str) -> tuple[int, ...]:
    """쉼표로 구분한 고유 0 이상 ASCII 정수 시드를 반환합니다."""
    tokens = [token.strip() for token in value.split(",")]
    if not tokens or any(_NON_NEGATIVE_INTEGER_PATTERN.fullmatch(token) is None for token in tokens):
        raise ValueError("random_seeds must be comma-separated non-negative integers")
    seeds = tuple(int(token) for token in tokens)
    if len(set(seeds)) != len(seeds):
        raise ValueError("random_seeds must be unique")
    return seeds


def _non_negative_integer(value: str, field_name: str) -> int:
    """0 이상 ASCII 정수 문자열을 int로 반환합니다."""
    if _NON_NEGATIVE_INTEGER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return int(value)


def _parse_split_sizes(test_value: str, validation_value: str) -> tuple[Decimal, Decimal]:
    """train 몫이 남는 0~1 사이 test·validation 비율을 반환합니다."""
    test_size = _finite_decimal(test_value, "test_size")
    validation_size = _finite_decimal(validation_value, "validation_size")
    if (
        not 0 < test_size < 1
        or not 0 < validation_size < 1
        or test_size + validation_size >= 1
    ):
        raise ValueError("test_size and validation_size must leave training data")
    test_float = _finite_float(test_size, "test_size")
    validation_float = _finite_float(validation_size, "validation_size")
    if (
        not 0 < test_float < 1
        or not 0 < validation_float < 1
        or test_float + validation_float >= 1
    ):
        raise ValueError("test_size and validation_size must remain valid as floats")
    return test_size, validation_size


def _finite_decimal(value: str, field_name: str) -> Decimal:
    """유한한 Decimal 문자열만 받아 숫자 경계 검증에 사용합니다."""
    try:
        decimal = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{field_name} must be a finite decimal") from error
    if not decimal.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal")
    return decimal


def _finite_float(value: Decimal, field_name: str) -> float:
    """공개 float 필드로 변환한 값도 유한한지 확인합니다."""
    try:
        float_value = float(value)
    except (OverflowError, ValueError) as error:
        raise ValueError(f"{field_name} must fit in a finite float") from error
    if not math.isfinite(float_value):
        raise ValueError(f"{field_name} must fit in a finite float")
    return float_value


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
    """식별자 입력에 사용할 Decimal의 동치 표기 canonical string을 반환합니다."""
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """CLI에서 이슈 메타데이터와 본문 파일 경로를 읽습니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue-number", required=True, type=int)
    parser.add_argument("--issue-title", required=True)
    parser.add_argument("--issue-body-file", required=True, type=Path)
    parser.add_argument("--experiment-id")
    parser.add_argument("--base-dev-sha")
    parser.add_argument("--candidates-json")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """이슈 계약 또는 completion selector 결과를 GitHub Actions output으로 기록합니다."""
    arguments = _parse_arguments(argv)
    issue_body = arguments.issue_body_file.read_text(encoding="utf-8")
    issue_input = parse_issue_input(arguments.issue_number, arguments.issue_title, issue_body)
    lines: tuple[str, ...] = (
        f"issue_branch={issue_input.issue_branch}",
        f"criteria_id={issue_input.criteria_id}",
        f"reproducibility_id={issue_input.reproducibility_id}",
    )
    selection_flags = (
        arguments.experiment_id,
        arguments.base_dev_sha,
        arguments.candidates_json,
    )
    if any(value is not None for value in selection_flags):
        if any(value is None for value in selection_flags):
            raise ValueError(
                "--experiment-id, --base-dev-sha, and --candidates-json must be provided together"
            )
        try:
            candidates = json.loads(arguments.candidates_json)
        except json.JSONDecodeError as error:
            raise ValueError("candidates_json must be valid JSON") from error
        if not isinstance(candidates, list) or not all(
            isinstance(candidate, dict) for candidate in candidates
        ):
            raise ValueError("candidates_json must be a JSON array of objects")
        selection = select_best_candidate(
            issue_input,
            arguments.experiment_id,
            arguments.base_dev_sha,
            candidates,
        )
        lines += (
            f"completion_schema_version={selection.schema_version}",
            f"result_set_id={selection.result_set_id}",
            f"selection_reason={selection.selection_reason}",
            f"selected_candidate_sha={selection.candidate_sha or ''}",
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
