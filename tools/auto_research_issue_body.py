"""Auto Research 이슈 본문을 실제 Issue Form 렌더 형식으로 조립하는 순수 렌더러입니다.

[파이프라인] 자율 실험 진입점에서 자연어 가설과 GitHub 이슈 등록 사이 — 에이전트가
정한 필드 값을 GitHub이 Issue Form 응답으로 렌더하는 ``### `` heading 본문 문자열로
바꾸는 구간을 담당합니다.

[기능] heading 순서·문자열과 허용 범위 label을 파싱 정본
(`tools/auto_research_issue_branch.py`)에서 파생해 본문을 만들고, 필수 필드 누락과
알 수 없는 필드, heading 구조를 깨뜨리는 값을 조립 시점에 거부합니다. 허용 범위는
미체크가 불허를 뜻하므로 항상 세 줄을 모두 출력합니다.

[비책임] 값의 의미 검증(지표 이름 규칙·시드 중복·split 비율·비교 대상 문자열)은
`tools/auto_research_issue_branch.py`의 `parse_issue_input()`이, GitHub 발행과 폭주
방지는 `tools/auto_research_issue_publish.py`가 소유합니다.
"""

from __future__ import annotations

import re
from typing import Mapping, Sequence

from tools.auto_research_issue_branch import (
    _HEADING_NAMES,
    _REQUIRED_SECTIONS,
    _SCOPE_LABELS,
)


ALLOWED_SCOPE_FIELD = "allowed_scope"
#: Issue Form이 렌더하는 순서 그대로의 필드 이름입니다. 정본은 `_HEADING_NAMES`입니다.
ORDERED_FIELDS: tuple[str, ...] = tuple(_HEADING_NAMES.values())
#: 필드 이름 → heading 문자열. 가운뎃점(U+00B7)을 포함한 heading도 정본에서 파생됩니다.
HEADING_BY_FIELD: dict[str, str] = {field: heading for heading, field in _HEADING_NAMES.items()}
#: 값이 비면 렌더러가 거부하는 필드입니다. 허용 범위는 별도 인자로 받습니다.
REQUIRED_TEXT_FIELDS: frozenset[str] = frozenset(
    _HEADING_NAMES[heading] for heading in _REQUIRED_SECTIONS
) - {ALLOWED_SCOPE_FIELD}
#: 채우거나 heading 자체를 생략하는 필드입니다.
OPTIONAL_TEXT_FIELDS: frozenset[str] = (
    frozenset(ORDERED_FIELDS) - REQUIRED_TEXT_FIELDS - {ALLOWED_SCOPE_FIELD}
)
#: 허용 범위 체크박스의 scope 키입니다. label 문자열의 정본은 `_SCOPE_LABELS`입니다.
SCOPE_KEYS: tuple[str, ...] = tuple(_SCOPE_LABELS.values())
_LABEL_BY_SCOPE: dict[str, str] = {scope: label for label, scope in _SCOPE_LABELS.items()}
_HEADING_LINE_PATTERN = re.compile(r"^###\s", re.MULTILINE)


def render_issue_body(
    fields: Mapping[str, str],
    allowed_scope: Sequence[str] = (),
) -> str:
    """필드 mapping을 GitHub Issue Form이 렌더하는 본문 문자열로 조립합니다.

    Args:
        fields: `_HEADING_NAMES`의 필드 이름을 키로 하는 값 mapping입니다.
            `allowed_scope`는 이 mapping이 아니라 전용 인자로 받습니다.
        allowed_scope: 체크할 scope 키(`SCOPE_KEYS`)입니다. 나머지 두 줄도 미체크로
            함께 렌더합니다 — 미체크가 불허를 뜻하므로 세 줄을 모두 명시합니다.

    Returns:
        `### ` heading 사이에 값을 담은 이슈 본문입니다. 그대로
        `parse_issue_input()`에 넣어 자가 검증할 수 있습니다.

    Raises:
        ValueError: 알 수 없는 필드, 빈 필수 필드, heading 구조를 깨뜨리는 값,
            알 수 없거나 중복된 scope 키가 있을 때 발생합니다.
    """
    _validate_fields(fields)
    scopes = _validated_scopes(allowed_scope)

    blocks: list[str] = []
    for field in ORDERED_FIELDS:
        heading = HEADING_BY_FIELD[field]
        if field == ALLOWED_SCOPE_FIELD:
            blocks.append(_render_scope_block(heading, scopes))
            continue
        value = fields.get(field, "").strip()
        if not value:
            continue
        blocks.append(f"### {heading}\n{value}")
    return "\n\n".join(blocks) + "\n"


def _validate_fields(fields: Mapping[str, str]) -> None:
    """필드 이름·타입·필수 여부와 heading 안전성을 조립 전에 검사합니다."""
    unknown_fields = sorted(set(fields) - set(ORDERED_FIELDS))
    if unknown_fields:
        raise ValueError("알 수 없는 필드: " + ", ".join(unknown_fields))
    if ALLOWED_SCOPE_FIELD in fields:
        raise ValueError(f"{ALLOWED_SCOPE_FIELD}는 fields가 아니라 allowed_scope 인자로 지정합니다")

    for field, value in fields.items():
        if not isinstance(value, str):
            raise ValueError(f"{field} 값은 문자열이어야 합니다")
        if _HEADING_LINE_PATTERN.search(value):
            raise ValueError(f"{field} 값이 '### ' heading 줄을 포함해 본문 구조를 깨뜨립니다")

    missing_fields = sorted(
        field for field in REQUIRED_TEXT_FIELDS if not fields.get(field, "").strip()
    )
    if missing_fields:
        raise ValueError("필수 필드가 비어 있습니다: " + ", ".join(missing_fields))


def _validated_scopes(allowed_scope: Sequence[str]) -> tuple[str, ...]:
    """체크할 scope 키가 알려진 값이고 중복이 없는지 확인합니다."""
    if isinstance(allowed_scope, str):
        raise ValueError("allowed_scope는 scope 키의 sequence여야 합니다")
    scopes = tuple(allowed_scope)
    unknown_scopes = sorted(set(scopes) - set(SCOPE_KEYS))
    if unknown_scopes:
        raise ValueError("알 수 없는 허용 범위: " + ", ".join(unknown_scopes))
    if len(set(scopes)) != len(scopes):
        raise ValueError("허용 범위가 중복되었습니다")
    return scopes


def _render_scope_block(heading: str, scopes: tuple[str, ...]) -> str:
    """허용 범위 세 줄을 체크 여부와 함께 정본 label 그대로 렌더합니다."""
    lines = [
        f"- [{'x' if scope in scopes else ' '}] {_LABEL_BY_SCOPE[scope]}" for scope in SCOPE_KEYS
    ]
    return f"### {heading}\n" + "\n".join(lines)
