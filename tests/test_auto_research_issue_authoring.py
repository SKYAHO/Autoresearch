"""자연어 가설 → Auto Research 이슈 발행 경로(#490)의 계약 테스트."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Mapping

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORM_PATH = PROJECT_ROOT / ".github/ISSUE_TEMPLATE/auto_research.yml"
RENDERED_FORM_FIXTURE = PROJECT_ROOT / "tests/fixtures/auto_research_issue_form_rendered.md"
GUIDE_PATH = PROJECT_ROOT / "docs/guides/auto-research-issue-authoring.md"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
sys.path.insert(0, str(PROJECT_ROOT))

from tools import auto_research_issue_publish as publish_module  # noqa: E402
from tools.auto_research_issue_body import (  # noqa: E402
    HEADING_BY_FIELD,
    ORDERED_FIELDS,
    SCOPE_KEYS,
    render_issue_body,
)
from tools.auto_research_issue_branch import (  # noqa: E402
    _HEADING_NAMES,
    _SCOPE_LABELS,
    parse_issue_input,
)
from tools.auto_research_issue_publish import (  # noqa: E402
    REQUIRED_LABELS,
    TOKEN_ENVIRONMENT_VARIABLE,
    hypothesis_dedupe_key,
    load_drafts,
    main,
    prepare_drafts,
    publish_issues,
)

# fixture의 허용 범위를 실제 렌더대로 3줄로 고치기 **전** 값이다. 허용 범위는 두
# 식별자의 해시 입력이 아니므로 3줄로 고쳐도 이 값이 바뀌어서는 안 된다(spec 결정 1).
SEALED_CRITERIA_ID = "1ae256dd8c582c9cc639ead186cf8d7a206c75c777d0865ba315ad6f1e5c875e"
SEALED_REPRODUCIBILITY_ID = "315f6fc3abe7bf1262915dc00eb55a3136090a946ac2382fad907fb80c32c3df"

ISSUE_TITLE = "[AR] CTR ratio"


def draft_fields(**overrides: str) -> dict[str, str]:
    """fixture와 동등한 본문을 만드는 필드 mapping을 반환한다."""
    fields = {
        "hypothesis": "비율 피처가 ROC-AUC를 높인다.",
        "change": "- 추가 피처: views_per_day = views / (days + 1)",
        "primary_metric_name": "roc_auc",
        "primary_metric_direction": "higher_is_better",
        "minimum_primary_delta": "0.002",
        "guardrail_metric_name": "없음",
        "guardrail_metric_direction": "not_applicable",
        "maximum_guardrail_regression": "없음",
        "secondary_metrics": "pr_auc",
        "comparison": "동일 조건 baseline 재학습 (권장)",
        "dataset_snapshot": "bq://autoresearch/train@2026-07-31",
        "random_seeds": "42, 43, 44",
        "split_seed": "20260731",
        "test_size": "0.2",
        "validation_size": "0.2",
        "training_config_ref": "configs/train/lgbm-v1.yaml@abc1234",
        "dataset": (
            "- 데이터셋 / 경로: data/train.csv\n"
            "- 기간 (KST YYYY-MM-DD ~ YYYY-MM-DD): 2026-07-01 ~ 2026-07-31"
        ),
        "snapshot_reuse": "허용 (진행하되 실제로 쓴 데이터를 결과에 명시)",
        "result": "- 판정 (지지/기각):",
    }
    fields.update(overrides)
    return fields


def write_drafts(tmp_path: Path, *drafts: Mapping[str, object]) -> Path:
    """초안 JSON 배열 파일을 만들고 경로를 돌려준다."""
    drafts_file = tmp_path / "drafts.json"
    drafts_file.write_text(json.dumps(list(drafts), ensure_ascii=False), encoding="utf-8")
    return drafts_file


def valid_draft(**overrides: str) -> dict[str, object]:
    """검증을 통과하는 초안 하나를 만든다."""
    return {"title": ISSUE_TITLE, "fields": draft_fields(**overrides), "allowed_scope": []}


class RecordingRequest:
    """GitHub 요청 seam의 테스트 더블. 호출을 기록하고 정해진 응답을 돌려준다."""

    def __init__(self, open_issues: list[dict[str, object]] | None = None) -> None:
        self.calls: list[tuple[str, str, Mapping[str, object] | None]] = []
        self._open_issues = open_issues or []
        self._next_number = 501

    def __call__(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None,
    ) -> object:
        self.calls.append((method, path, payload))
        if method == "GET":
            return self._open_issues if path.endswith("&page=1") else []
        self._next_number += 1
        return {
            "number": self._next_number,
            "html_url": f"https://github.com/o/n/issues/{self._next_number}",
        }

    @property
    def created_payloads(self) -> list[Mapping[str, object] | None]:
        """실제 이슈 생성 요청의 payload만 모아 돌려준다."""
        return [payload for method, _, payload in self.calls if method == "POST"]


def form_labels_in_order() -> list[str]:
    """Issue Form이 렌더하는 heading label을 파일 순서대로 반환한다."""
    parsed = yaml.safe_load(FORM_PATH.read_text(encoding="utf-8"))
    return [
        item["attributes"]["label"] for item in parsed["body"] if item["type"] != "markdown"
    ]


def form_scope_labels_in_order() -> list[str]:
    """Issue Form 허용 범위 체크박스 label을 파일 순서대로 반환한다."""
    parsed = yaml.safe_load(FORM_PATH.read_text(encoding="utf-8"))
    for item in parsed["body"]:
        if item.get("id") == "allowed_scope":
            return [option["label"] for option in item["attributes"]["options"]]
    raise AssertionError("Issue Form에 allowed_scope 필드가 없다")


# --- fixture 봉인 회귀 -------------------------------------------------------


def test_fixture_renders_every_allowed_scope_checkbox() -> None:
    """GitHub의 `type: checkboxes`는 옵션을 모두 렌더하므로 fixture도 3줄이어야 한다."""
    body = RENDERED_FORM_FIXTURE.read_text(encoding="utf-8")
    scope_section = body.split("### 허용 범위\n", 1)[1].split("\n\n", 1)[0]
    lines = scope_section.splitlines()

    assert lines == [f"- [ ] {label}" for label in _SCOPE_LABELS]


def test_fixture_scope_expansion_preserves_sealed_identifiers() -> None:
    """허용 범위 3줄 수정 후에도 봉인된 두 식별자가 수정 전과 같아야 한다."""
    issue_input = parse_issue_input(
        449,
        ISSUE_TITLE,
        RENDERED_FORM_FIXTURE.read_text(encoding="utf-8"),
    )

    assert issue_input.criteria_id == SEALED_CRITERIA_ID
    assert issue_input.reproducibility_id == SEALED_REPRODUCIBILITY_ID
    assert issue_input.allowed_scope == ()


# --- 렌더러 ------------------------------------------------------------------


def test_rendered_body_equals_actual_form_fixture() -> None:
    """렌더러 출력이 실제 Issue Form 렌더 본문과 문자 단위로 같아야 한다."""
    assert render_issue_body(draft_fields()) == RENDERED_FORM_FIXTURE.read_text(encoding="utf-8")


def test_rendered_body_round_trips_through_parse_issue_input() -> None:
    """렌더러 출력을 그대로 파싱 정본에 넣으면 계약이 복원되어야 한다."""
    issue_input = parse_issue_input(449, ISSUE_TITLE, render_issue_body(draft_fields()))

    assert issue_input.issue_branch == "exp/449-ctr-ratio"
    assert issue_input.primary_metric_name == "roc_auc"
    assert issue_input.random_seeds == (42, 43, 44)
    assert issue_input.criteria_id == SEALED_CRITERIA_ID
    assert issue_input.reproducibility_id == SEALED_REPRODUCIBILITY_ID


def test_render_uses_heading_order_derived_from_parser_contract() -> None:
    """heading 문자열과 순서를 하드코딩하지 않고 `_HEADING_NAMES`에서 파생해야 한다."""
    body = render_issue_body(draft_fields(), SCOPE_KEYS)
    rendered_headings = [
        line.removeprefix("### ") for line in body.splitlines() if line.startswith("### ")
    ]

    assert rendered_headings == list(_HEADING_NAMES)
    assert "변경할 피처 · 모델" in rendered_headings
    assert "대상 데이터 · 기간" in rendered_headings


def test_render_always_emits_every_scope_checkbox() -> None:
    """미체크가 불허를 뜻하므로 세 줄을 항상 명시한다."""
    body = render_issue_body(draft_fields(), ["promotion"])
    scope_section = body.split("### 허용 범위\n", 1)[1].split("\n\n", 1)[0]

    assert scope_section.splitlines() == [
        "- [ ] prod 모델 계약(`src/features/model_contract.py`) 수정을 허용한다",
        "- [ ] Feast 정의(`feature_repo/`) 수정을 허용한다",
        "- [x] 실험 결과를 champion으로 승격하는 것까지 검토한다",
    ]
    assert parse_issue_input(449, ISSUE_TITLE, body).allowed_scope == ("promotion",)


def test_render_omits_optional_heading_when_value_is_absent() -> None:
    """`보조 관측 지표`는 채우거나 heading 자체를 생략한다."""
    fields = draft_fields()
    del fields["secondary_metrics"]
    body = render_issue_body(fields)

    assert "### 보조 관측 지표" not in body
    assert parse_issue_input(449, ISSUE_TITLE, body).secondary_metrics == ""


def test_render_rejects_unknown_field() -> None:
    with pytest.raises(ValueError, match="알 수 없는 필드"):
        render_issue_body(draft_fields() | {"unknown_field": "x"})


def test_render_rejects_missing_required_field() -> None:
    fields = draft_fields()
    fields["primary_metric_name"] = "   "

    with pytest.raises(ValueError, match="필수 필드가 비어 있습니다"):
        render_issue_body(fields)


def test_render_rejects_allowed_scope_inside_fields() -> None:
    with pytest.raises(ValueError, match="allowed_scope 인자로 지정합니다"):
        render_issue_body(draft_fields() | {"allowed_scope": "- [ ] x"})


def test_render_rejects_heading_injection_in_value() -> None:
    """값이 heading 줄을 담으면 알 수 없는·중복 heading을 만들어 낸다."""
    with pytest.raises(ValueError, match="heading 줄을 포함해"):
        render_issue_body(draft_fields(hypothesis="가설\n### 연구 가설\n중복"))


def test_render_rejects_unknown_scope_key() -> None:
    with pytest.raises(ValueError, match="알 수 없는 허용 범위"):
        render_issue_body(draft_fields(), ["everything"])


def test_render_rejects_duplicate_scope_key() -> None:
    with pytest.raises(ValueError, match="허용 범위가 중복"):
        render_issue_body(draft_fields(), ["promotion", "promotion"])


# --- 발행 전 게이트: 계약 위반은 발행되지 않는다 ------------------------------


def run_publish_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *drafts: Mapping[str, object],
    publish: bool = True,
    max_issues: int = 1,
    open_issues: list[dict[str, object]] | None = None,
) -> tuple[int, RecordingRequest]:
    """CLI를 실행하고 종료 코드와 GitHub 요청 기록을 돌려준다."""
    request = RecordingRequest(open_issues)
    monkeypatch.setattr(publish_module, "github_request", lambda token: request)
    monkeypatch.setenv(TOKEN_ENVIRONMENT_VARIABLE, "test-token-value")
    argv = [
        "--drafts-file",
        str(write_drafts(tmp_path, *drafts)),
        "--max-issues",
        str(max_issues),
    ]
    if publish:
        argv.extend(["--publish", "--repository", "SKYAHO/Autoresearch"])
    return main(argv), request


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"hypothesis": "가설\n### 연구 가설\n중복"}, "heading 줄을 포함해"),
        ({"guardrail_metric_direction": "lower_is_better"}, "없음/not_applicable/없음"),
        ({"guardrail_metric_name": "logloss"}, "guardrail_metric_direction must compare"),
        ({"random_seeds": "42, 42, 43"}, "random_seeds must be unique"),
        ({"test_size": "0.6", "validation_size": "0.5"}, "must leave training data"),
        ({"primary_metric_name": "1_roc_auc"}, "primary_metric_name must match"),
        ({"comparison": "임의 비교"}, "comparison must be an Issue Form option"),
    ],
)
def test_contract_violation_blocks_publication_with_readable_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, str],
    expected_reason: str,
) -> None:
    """계약 위반 초안은 GitHub 요청을 한 번도 만들지 않고 사유를 보고한다."""
    exit_code, request = run_publish_cli(tmp_path, monkeypatch, valid_draft(**overrides))
    output = capsys.readouterr().out

    assert exit_code == 1
    assert request.calls == []
    assert "발행 중단" in output
    assert expected_reason in output


def test_unknown_heading_field_blocks_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`_HEADING_NAMES`에 없는 필드는 알 수 없는 heading이 되기 전에 거부된다."""
    draft = valid_draft()
    draft["fields"]["기타 메모"] = "x"  # type: ignore[index]

    exit_code, request = run_publish_cli(tmp_path, monkeypatch, draft)
    output = capsys.readouterr().out

    assert exit_code == 1
    assert request.calls == []
    assert "알 수 없는 필드" in output


def test_allowed_scope_with_non_checkbox_line_fails_the_publication_gate() -> None:
    """허용 범위에 체크박스가 아닌 줄이 섞이면 발행 전 게이트가 막는다.

    렌더러는 구조적으로 이런 본문을 만들 수 없다 — 손으로 고친 본문이 게이트를
    통과하지 못한다는 것을 파싱 정본에 직접 확인한다.
    """
    body = render_issue_body(draft_fields()).replace(
        "### 허용 범위\n",
        "### 허용 범위\n허용 범위는 아래와 같습니다\n",
    )

    with pytest.raises(ValueError, match="allowed_scope must contain only Issue Form checkboxes"):
        parse_issue_input(publish_module.PLACEHOLDER_ISSUE_NUMBER, ISSUE_TITLE, body)


def test_publication_limit_blocks_the_whole_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """1회 실행 상한을 넘으면 한 건도 발행하지 않는다."""
    exit_code, request = run_publish_cli(
        tmp_path,
        monkeypatch,
        valid_draft(),
        valid_draft(minimum_primary_delta="0.005"),
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert request.calls == []
    assert "1회 실행 상한" in output


def test_same_hypothesis_twice_in_one_batch_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code, request = run_publish_cli(
        tmp_path,
        monkeypatch,
        valid_draft(),
        valid_draft(),
        max_issues=2,
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert request.calls == []
    assert "동일한 가설" in output


def test_open_issue_with_same_contract_blocks_republication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """같은 criteria_id+reproducibility_id의 열린 이슈가 있으면 발행하지 않는다."""
    open_issue = {
        "number": 449,
        "title": ISSUE_TITLE,
        "body": RENDERED_FORM_FIXTURE.read_text(encoding="utf-8"),
    }

    exit_code, request = run_publish_cli(
        tmp_path,
        monkeypatch,
        valid_draft(),
        open_issues=[open_issue],
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert request.created_payloads == []
    assert "같은 연구 가설·변경 내용의 열린 이슈가 이미 있습니다" in output


def test_same_criteria_and_reproducibility_with_new_hypothesis_is_not_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """판정기준·재현조건이 같아도 가설·변경이 다르면 정상 반복 실험이므로 통과한다.

    `criteria_id`/`reproducibility_id` 조합을 차단 키로 쓰면 같은 스냅샷·시드·지표
    위에서 피처만 바꿔 돌리는 정상 사용 패턴이 전부 거부된다.
    """
    open_issue = {
        "number": 449,
        "title": ISSUE_TITLE,
        "body": RENDERED_FORM_FIXTURE.read_text(encoding="utf-8"),
    }
    new_draft = valid_draft(
        hypothesis="로그 변환 피처가 ROC-AUC를 높인다.",
        change="- 추가 피처: log_views = log1p(views)",
    )
    existing = parse_issue_input(449, ISSUE_TITLE, str(open_issue["body"]))
    candidate = prepare_drafts([new_draft])[0]

    assert candidate.criteria_id == existing.criteria_id
    assert candidate.reproducibility_id == existing.reproducibility_id

    exit_code, request = run_publish_cli(
        tmp_path,
        monkeypatch,
        new_draft,
        open_issues=[open_issue],
    )

    assert exit_code == 0
    assert len(request.created_payloads) == 1


def test_same_hypothesis_with_a_different_seed_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """시드 하나만 바꿔 같은 가설을 다시 발행하는 우회를 막는다."""
    open_issue = {
        "number": 449,
        "title": ISSUE_TITLE,
        "body": RENDERED_FORM_FIXTURE.read_text(encoding="utf-8"),
    }
    resubmission = valid_draft(random_seeds="42, 43, 45")
    existing = parse_issue_input(449, ISSUE_TITLE, str(open_issue["body"]))
    candidate = prepare_drafts([resubmission])[0]

    assert candidate.reproducibility_id != existing.reproducibility_id

    exit_code, request = run_publish_cli(
        tmp_path,
        monkeypatch,
        resubmission,
        open_issues=[open_issue],
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert request.created_payloads == []
    assert "같은 연구 가설·변경 내용의 열린 이슈가 이미 있습니다" in output


def test_hypothesis_dedupe_key_ignores_criteria_and_reproducibility_fields() -> None:
    """차단 키의 해시 입력은 `연구 가설`과 `변경할 피처 · 모델` 둘뿐이다."""
    base = parse_issue_input(449, ISSUE_TITLE, render_issue_body(draft_fields()))
    other_conditions = parse_issue_input(
        449,
        ISSUE_TITLE,
        render_issue_body(draft_fields(random_seeds="1, 2", minimum_primary_delta="0.05")),
    )
    other_hypothesis = parse_issue_input(
        449,
        ISSUE_TITLE,
        render_issue_body(draft_fields(hypothesis="다른 가설")),
    )

    assert hypothesis_dedupe_key(base) == hypothesis_dedupe_key(other_conditions)
    assert hypothesis_dedupe_key(base) != hypothesis_dedupe_key(other_hypothesis)
    assert len(hypothesis_dedupe_key(base)) == 64


def test_unparsable_open_issue_is_reported_and_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """계약을 만족하지 않는 열린 이슈는 차단 키 확인에서 제외하고 보고한다."""
    open_issue = {"number": 400, "title": "[AR] legacy", "body": "본문 없음"}

    exit_code, _ = run_publish_cli(
        tmp_path,
        monkeypatch,
        valid_draft(),
        open_issues=[open_issue],
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "#400" in output


# --- 발행 경로 ---------------------------------------------------------------


def test_dry_run_is_the_default_and_makes_no_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--publish` 없이는 절대 발행하지 않는다."""
    exit_code, request = run_publish_cli(tmp_path, monkeypatch, valid_draft(), publish=False)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert request.calls == []
    assert "dry-run" in output
    assert "발행하지 않았습니다" in output
    assert SEALED_CRITERIA_ID in output
    assert "hypothesis_dedupe_key=" in output


def test_publish_applies_both_required_labels_and_recomputes_only_the_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """워크플로 job은 두 label을 동시에 가질 때만 실행된다."""
    exit_code, request = run_publish_cli(tmp_path, monkeypatch, valid_draft())
    output = capsys.readouterr().out

    assert exit_code == 0
    assert len(request.created_payloads) == 1
    payload = request.created_payloads[0]
    assert payload is not None
    assert payload["labels"] == list(REQUIRED_LABELS)
    assert set(REQUIRED_LABELS) == {"auto-research", "experiment"}
    assert "issue_branch=exp/502-ctr-ratio" in output
    assert "issue_number=502" in output


def test_publish_does_not_recompute_identifiers_after_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """발행 후 재계산이 필요한 값은 `issue_branch` 하나뿐이다(spec 결정 5)."""
    prepared = prepare_drafts([valid_draft()])
    request = RecordingRequest()

    outcome = publish_issues(prepared, "SKYAHO/Autoresearch", request)

    assert prepared[0].criteria_id == SEALED_CRITERIA_ID
    assert prepared[0].reproducibility_id == SEALED_REPRODUCIBILITY_ID
    published_body = request.created_payloads[0]
    assert published_body is not None
    assert published_body["body"] == prepared[0].body
    assert outcome.published[0].issue_branch == "exp/502-ctr-ratio"


def test_prepare_drafts_rejects_title_without_form_prefix() -> None:
    with pytest.raises(ValueError, match=r"\[AR\]"):
        prepare_drafts([valid_draft() | {"title": "CTR ratio"}])


def test_load_drafts_rejects_unknown_draft_key(tmp_path: Path) -> None:
    drafts_file = write_drafts(tmp_path, valid_draft() | {"labels": ["custom"]})

    with pytest.raises(ValueError, match="알 수 없는 키"):
        load_drafts(drafts_file)


def test_token_value_never_appears_in_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """토큰 값을 로그·에러 메시지 어디에도 남기지 않는다."""
    secret = "sentinel-token-must-not-leak"
    monkeypatch.setenv(TOKEN_ENVIRONMENT_VARIABLE, secret)
    request = RecordingRequest()
    monkeypatch.setattr(publish_module, "github_request", lambda token: request)
    drafts_file = write_drafts(tmp_path, valid_draft())

    main(["--drafts-file", str(drafts_file), "--publish", "--repository", "SKYAHO/Autoresearch"])
    main(["--drafts-file", str(drafts_file)])

    assert secret not in capsys.readouterr().out


def test_missing_token_blocks_publication_without_naming_a_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv(TOKEN_ENVIRONMENT_VARIABLE, raising=False)
    drafts_file = write_drafts(tmp_path, valid_draft())

    exit_code = main(
        ["--drafts-file", str(drafts_file), "--publish", "--repository", "SKYAHO/Autoresearch"]
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert TOKEN_ENVIRONMENT_VARIABLE in output
    assert "issues: write" in output


def test_publish_requires_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(TOKEN_ENVIRONMENT_VARIABLE, "test-token-value")
    drafts_file = write_drafts(tmp_path, valid_draft())

    exit_code = main(["--drafts-file", str(drafts_file), "--publish"])

    assert exit_code == 1
    assert "--repository" in capsys.readouterr().out


# --- drift: 가이드 ↔ 파서 정본 ↔ Issue Form ---------------------------------


def test_parser_headings_match_issue_form_labels_in_order() -> None:
    assert list(_HEADING_NAMES) == form_labels_in_order()


def test_parser_scope_labels_match_issue_form_options_in_order() -> None:
    assert list(_SCOPE_LABELS) == form_scope_labels_in_order()


def test_guide_documents_every_heading_and_field_name() -> None:
    """가이드가 heading·필드 이름을 하나라도 빠뜨리면 실패한다."""
    guide = GUIDE_PATH.read_text(encoding="utf-8")
    missing_headings = [heading for heading in _HEADING_NAMES if heading not in guide]
    missing_fields = [field for field in ORDERED_FIELDS if field not in guide]

    assert not missing_headings, f"가이드에 없는 heading: {missing_headings}"
    assert not missing_fields, f"가이드에 없는 필드 이름: {missing_fields}"


def test_guide_documents_every_allowed_scope_label() -> None:
    guide = GUIDE_PATH.read_text(encoding="utf-8")
    missing = [label for label in _SCOPE_LABELS if label not in guide]

    assert not missing, f"가이드에 없는 허용 범위 label: {missing}"


def test_guide_declares_itself_derived_from_the_canonical_contracts() -> None:
    """가이드는 정본이 아니라 파생물임을 명시해야 한다(spec)."""
    guide = GUIDE_PATH.read_text(encoding="utf-8")

    assert "정본이 아니라 파생물" in guide
    assert "tools/auto_research_issue_branch.py" in guide
    assert ".github/ISSUE_TEMPLATE/auto_research.yml" in guide


def test_heading_by_field_covers_every_ordered_field() -> None:
    assert set(HEADING_BY_FIELD) == set(ORDERED_FIELDS)


def test_env_example_registers_the_token_with_an_empty_value() -> None:
    """새 환경 변수는 빈 값 + 용도 주석으로 등록한다."""
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()

    assert f"{TOKEN_ENVIRONMENT_VARIABLE}=" in lines
    index = lines.index(f"{TOKEN_ENVIRONMENT_VARIABLE}=")
    assert lines[index - 1].startswith("#")
