"""Auto Research 이슈 작성 계약(#490)의 drift 테스트.

에이전트는 `gh issue create`로 직접 이슈를 발행한다. 별도 렌더러나 발행 도구를
두지 않으므로, 이 파일은 **작성 가이드와 계약 정본이 어긋나면 실패**시키는 역할만
한다. 값의 의미 검증은 `tools/auto_research_issue_branch.py`의
`parse_issue_input()`이 소유하며 여기서 중복하지 않는다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORM_PATH = PROJECT_ROOT / ".github/ISSUE_TEMPLATE/auto_research.yml"
RENDERED_FORM_FIXTURE = PROJECT_ROOT / "tests/fixtures/auto_research_issue_form_rendered.md"
GUIDE_PATH = PROJECT_ROOT / "docs/guides/auto-research-issue-authoring.md"
sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto_research_issue_branch import (  # noqa: E402
    _HEADING_NAMES,
    _SCOPE_LABELS,
    parse_issue_input,
)

# fixture의 허용 범위를 실제 렌더대로 3줄로 고치기 **전** 값이다. 허용 범위는 두
# 식별자의 해시 입력이 아니므로 3줄로 고쳐도 이 값이 바뀌어서는 안 된다.
SEALED_CRITERIA_ID = "1ae256dd8c582c9cc639ead186cf8d7a206c75c777d0865ba315ad6f1e5c875e"
SEALED_REPRODUCIBILITY_ID = "315f6fc3abe7bf1262915dc00eb55a3136090a946ac2382fad907fb80c32c3df"

ISSUE_TITLE = "[AR] CTR ratio"


def _form_fields() -> list[dict]:
    form = yaml.safe_load(FORM_PATH.read_text(encoding="utf-8"))
    return [block for block in form["body"] if block.get("type") != "markdown"]


# --- fixture가 실제 렌더와 같은가 -------------------------------------------


def test_fixture_renders_every_allowed_scope_checkbox() -> None:
    """GitHub의 `type: checkboxes`는 옵션을 모두 렌더하므로 fixture도 3줄이어야 한다."""
    body = RENDERED_FORM_FIXTURE.read_text(encoding="utf-8")
    scope_section = body.split("### 허용 범위\n", 1)[1].split("\n\n", 1)[0]

    assert scope_section.splitlines() == [f"- [ ] {label}" for label in _SCOPE_LABELS]


def test_fixture_scope_expansion_preserves_sealed_identifiers() -> None:
    """허용 범위 3줄 수정 후에도 봉인된 두 식별자가 수정 전과 같아야 한다.

    `criteria_id`는 주 지표 6필드, `reproducibility_id`는 dataset/seed/split/config
    6필드만 묶는다. `허용 범위`는 어느 쪽에도 들어가지 않으므로 marker 봉인
    재검증이 깨지지 않는다.
    """
    issue_input = parse_issue_input(
        449,
        ISSUE_TITLE,
        RENDERED_FORM_FIXTURE.read_text(encoding="utf-8"),
    )

    assert issue_input.criteria_id == SEALED_CRITERIA_ID
    assert issue_input.reproducibility_id == SEALED_REPRODUCIBILITY_ID
    assert issue_input.allowed_scope == ()


# --- 파서 계약과 Issue Form이 일치하는가 -------------------------------------


def test_parser_headings_match_issue_form_labels_in_order() -> None:
    """`_HEADING_NAMES`의 heading이 Issue Form label과 순서까지 같아야 한다."""
    assert list(_HEADING_NAMES) == [field["attributes"]["label"] for field in _form_fields()]


def test_parser_scope_labels_match_issue_form_options_in_order() -> None:
    """`_SCOPE_LABELS`가 `허용 범위` 체크박스 옵션과 순서까지 같아야 한다."""
    scope_field = next(
        field for field in _form_fields() if field["attributes"]["label"] == "허용 범위"
    )
    options = [option["label"] for option in scope_field["attributes"]["options"]]

    assert list(_SCOPE_LABELS) == options


# --- 가이드가 계약을 빠짐없이 담고 있는가 ------------------------------------


def test_guide_documents_every_heading() -> None:
    """가이드에 20개 heading이 모두 적혀 있어야 한다.

    에이전트가 이 가이드만 보고 본문을 작성하므로, heading 하나가 빠지면 그
    필드를 누락한 본문이 만들어지고 워크플로가 fail-closed로 거부한다.
    """
    guide = GUIDE_PATH.read_text(encoding="utf-8")
    missing = [heading for heading in _HEADING_NAMES if heading not in guide]

    assert not missing, f"가이드에 없는 heading: {missing}"


def test_guide_documents_every_allowed_scope_label() -> None:
    """`허용 범위` 체크박스 label 세 개가 가이드에 정확한 문자열로 있어야 한다."""
    guide = GUIDE_PATH.read_text(encoding="utf-8")
    missing = [label for label in _SCOPE_LABELS if label not in guide]

    assert not missing, f"가이드에 없는 scope label: {missing}"


def test_guide_documents_the_required_labels_for_gh_issue_create() -> None:
    """가이드가 `gh issue create`에 필요한 label 두 개를 모두 적어야 한다.

    Form을 우회해 API로 발행하면 label이 자동 적용되지 않는데, 브랜치 생성 job은
    두 label을 동시에 가질 때만 실행된다. 하나라도 빠지면 job이 실패가 아니라
    **skip**되어 아무 알림도 남지 않는다.
    """
    guide = GUIDE_PATH.read_text(encoding="utf-8")

    assert "--label auto-research" in guide
    assert "--label experiment" in guide


def test_guide_declares_itself_derived_from_the_canonical_contracts() -> None:
    """가이드가 정본이 아니라 파생물임을 명시해야 한다."""
    guide = GUIDE_PATH.read_text(encoding="utf-8")

    assert ".github/ISSUE_TEMPLATE/auto_research.yml" in guide
    assert "tools/auto_research_issue_branch.py" in guide
