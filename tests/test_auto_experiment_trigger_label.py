"""Auto Research 트리거 label 문자열이 다섯 곳에서 어긋나지 않게 고정한다.

전체 파이프라인 기준으로 이 모듈은 **실험 실행이나 판정에 관여하지 않는다.**
가설 이슈가 exp 브랜치를 만들고 승격 단계까지 가는 흐름의 **입구 조건**만
검증하는 계약 테스트다. 브랜치 생성과 본문 파싱은
`.github/workflows/auto-research-issue-branch.yml`과
`tools/auto_research_issue_branch.py`가, 승격 판정은
`autoresearch/experiments/promotion_gate.py`가 담당하며 여기서 다루지 않는다.

트리거 label 문자열은 Issue Form·워크플로 2개·문서 2개에 흩어져 있고, 어긋나도
**조용히 실패한다.** 워크플로의 `if:` 미충족은 실패가 아니라 skip이라 체크도
알림도 남지 않고, `auto-research-promotion.yml`의 가드는 실행 이력이 없으면
아무도 모른다 — #495에서 `promotion_gate._LABELS`가 실제 Issue Form에 없는
label을 가리킨 채 도입 이래 한 번도 동작하지 않았던 것이 같은 실패 유형이다.

[기능] Issue Form의 `labels:`를 정본으로 삼아 나머지 네 곳이 같은 label을
말하는지 검사한다.

- Form이 정확히 하나의 label만 부여한다는 사실 (트리거 = 분류 = 하나)
- 브랜치 생성 job이 그 label **단독**으로 게이팅된다는 사실
- 승격 워크플로의 이슈 가드가 같은 label을 요구한다는 사실
- 두 문서의 Issue Form 표가 Form의 `labels:`와 정확히 같은 집합을 싣는다는 사실

[비책임] label이 GitHub 저장소에 실제로 존재하는지는 검사하지 않는다(네트워크
접근 없음). label 생성은 저장소 밖 조치이며 `CONTRIBUTING.md`가 안내한다.
"""

import re
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

ISSUE_FORM = REPOSITORY_ROOT / ".github" / "ISSUE_TEMPLATE" / "auto_research.yml"
ISSUE_BRANCH_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "auto-research-issue-branch.yml"
)
PROMOTION_WORKFLOW = (
    REPOSITORY_ROOT / ".github" / "workflows" / "auto-research-promotion.yml"
)
CONTRIBUTING = REPOSITORY_ROOT / "CONTRIBUTING.md"
WORKFLOW_REFERENCE = (
    REPOSITORY_ROOT / ".claude" / "docs" / "agent-workflow-reference.md"
)

DOCUMENTS_WITH_ISSUE_FORM_TABLE = (CONTRIBUTING, WORKFLOW_REFERENCE)

# 두 문서 모두 `| `auto_research.yml` | `[AR]` | <자동 label> | <설명> |` 형식의
# Issue Form 표를 가진다. 표에서 자동 label 칸만 뽑아 Form과 대조한다.
_FORM_TABLE_ROW = re.compile(
    r"^\|\s*`auto_research\.yml`\s*\|(?P<rest>.*)$", re.MULTILINE
)
_BACKTICKED = re.compile(r"`([^`]+)`")


def _form_labels() -> list[str]:
    """Issue Form이 자동 부여하는 label 목록 — 이 계약의 정본."""
    form = yaml.safe_load(ISSUE_FORM.read_text(encoding="utf-8"))
    labels = form["labels"]
    assert isinstance(labels, list)
    return labels


def _documented_labels(path: Path) -> list[str]:
    """문서의 Issue Form 표에서 `auto_research.yml` 행의 자동 label 칸을 읽는다."""
    matches = _FORM_TABLE_ROW.findall(path.read_text(encoding="utf-8"))
    assert len(matches) == 1, (
        f"{path.name}에 auto_research.yml 표 행이 {len(matches)}개 있습니다 (1개여야 함)"
    )
    # rest = " `[AR]` | `auto-experiment` | 설명 |" — 두 번째 칸이 자동 label이다.
    cells = matches[0].split("|")
    assert len(cells) >= 3, f"{path.name}의 표 행 칸 수가 부족합니다: {matches[0]!r}"
    return _BACKTICKED.findall(cells[1])


def test_issue_form_applies_exactly_one_trigger_label() -> None:
    """Form이 label 하나만 부여함을 고정한다(#507).

    label을 다시 늘리면 Form을 우회해 발행하는 주체가 전부를 붙여야 하고, 하나만
    빠져도 브랜치 생성이 조용히 skip되던 상태로 되돌아간다.
    """
    assert _form_labels() == ["auto-experiment"]


def test_issue_branch_job_gates_on_the_form_label_alone() -> None:
    """브랜치 생성 job의 `if:`가 Form label 단독으로 구성됨을 고정한다."""
    (trigger_label,) = _form_labels()
    workflow = yaml.safe_load(ISSUE_BRANCH_WORKFLOW.read_text(encoding="utf-8"))
    condition = workflow["jobs"]["create-or-verify-issue-branch"]["if"]

    expected = f"contains(github.event.issue.labels.*.name, '{trigger_label}')"
    assert condition.strip() == expected


def test_promotion_workflow_guard_requires_the_form_label() -> None:
    """승격 워크플로의 이슈 가드가 같은 label을 요구함을 고정한다.

    여기가 두 번째 게이트다. Form만 바꾸고 이 가드를 두면 발행된 `[AR]` 이슈는
    옛 label을 갖지 않으므로 승격 단계에서 항상 throw한다.
    """
    (trigger_label,) = _form_labels()
    workflow_text = PROMOTION_WORKFLOW.read_text(encoding="utf-8")

    assert f"label.name === '{trigger_label}'" in workflow_text
    assert f"issue must have the {trigger_label} label" in workflow_text


def test_documents_list_the_same_labels_as_the_issue_form() -> None:
    """두 문서의 Issue Form 표가 Form의 `labels:`와 같은 집합을 실음을 고정한다."""
    form_labels = _form_labels()
    for path in DOCUMENTS_WITH_ISSUE_FORM_TABLE:
        assert _documented_labels(path) == form_labels, (
            f"{path.name}의 자동 label 표기가 Issue Form과 다릅니다"
        )


def test_documents_name_the_trigger_label_in_the_automation_label_section() -> None:
    """두 문서의 '자동화 트리거 label' 절이 실제 트리거 label을 지목함을 고정한다.

    표만 고치고 본문 설명을 두면 사람이 읽는 정본과 워크플로가 어긋난다.
    """
    (trigger_label,) = _form_labels()
    for path in DOCUMENTS_WITH_ISSUE_FORM_TABLE:
        text = path.read_text(encoding="utf-8")
        head, separator, tail = text.partition("자동화 트리거 label")
        assert separator, f"{path.name}에 '자동화 트리거 label' 절이 없습니다"
        assert trigger_label in tail, (
            f"{path.name}의 '자동화 트리거 label' 절이 {trigger_label}을 언급하지 않습니다"
        )
