from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORM_PATH = PROJECT_ROOT / ".github/ISSUE_TEMPLATE/auto_research.yml"
BRANCH_WORKFLOW = PROJECT_ROOT / ".github/workflows/auto-research-issue-branch.yml"
sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto_research_issue_branch import (  # noqa: E402
    branch_name_for,
    is_descendant,
    parse_issue_input,
)


def structured_body(
    *,
    primary_metric_name: str = "roc_auc",
    primary_metric_direction: str = "higher_is_better",
    minimum_primary_delta: str = "0.002",
    guardrail_metric_name: str = "없음",
    guardrail_metric_direction: str = "not_applicable",
    maximum_guardrail_regression: str = "없음",
    dataset_snapshot: str = "bq://autoresearch/train@2026-07-31",
    random_seeds: str = "42, 43, 44",
    split_seed: str = "20260731",
    test_size: str = "0.2",
    validation_size: str = "0.2",
    training_config_ref: str = "configs/train/lgbm-v1.yaml@abc1234",
    allowed_scope: str = "- [ ] prod 모델 계약(`src/features/model_contract.py`) 수정을 허용한다",
) -> str:
    """GitHub가 구조화 Form 응답에서 생성하는 실제 heading 본문을 만듭니다."""
    return f"""## 연구 가설
비율 피처가 ROC-AUC를 높인다.

## 변경할 피처 · 모델
- 추가 피처: views_per_day = views / (days + 1)

## 주 지표 이름
{primary_metric_name}

## 주 지표 방향
{primary_metric_direction}

## 최소 주 지표 개선폭
{minimum_primary_delta}

## Guardrail 지표 이름
{guardrail_metric_name}

## Guardrail 지표 방향
{guardrail_metric_direction}

## 최대 Guardrail 악화폭
{maximum_guardrail_regression}

## 보조 관측 지표
pr_auc

## 비교 대상
동일 조건 baseline 재학습 (권장)

## 데이터셋 스냅샷
{dataset_snapshot}

## 랜덤 시드 목록
{random_seeds}

## Split 시드
{split_seed}

## Test 비율
{test_size}

## Validation 비율
{validation_size}

## 학습 설정 참조
{training_config_ref}

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


def load_form() -> dict[str, object]:
    """Issue Form을 실제 YAML parser로 읽습니다."""
    parsed = yaml.safe_load(FORM_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def form_fields() -> dict[str, dict[str, object]]:
    """Markdown을 제외한 Form field를 ID로 반환합니다."""
    fields: dict[str, dict[str, object]] = {}
    for item in load_form()["body"]:
        if item["type"] == "markdown":
            continue
        fields[item["id"]] = item
    return fields


def load_branch_workflow() -> dict[object, object]:
    """이슈별 불변 branch workflow를 실제 YAML parser로 읽습니다."""
    parsed = yaml.safe_load(BRANCH_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def workflow_trigger(workflow: dict[object, object]) -> dict[str, object]:
    """YAML 1.1 parser의 ``on`` boolean 호환성을 포함해 trigger를 반환합니다."""
    trigger = workflow.get("on", workflow.get(True))
    assert isinstance(trigger, dict)
    return trigger


def body_rendered_from_form() -> str:
    """실제 Form label과 유효 입력값으로 GitHub 이슈 본문을 렌더링합니다."""
    values = {
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
        "dataset": "- 데이터셋 / 경로: data/train.csv",
        "snapshot_reuse": "허용 (진행하되 실제로 쓴 데이터를 결과에 명시)",
        "allowed_scope": "- [ ] prod 모델 계약(`src/features/model_contract.py`) 수정을 허용한다",
        "result": "- 판정 (지지/기각):",
    }
    sections = []
    for field_id, field in form_fields().items():
        label = field["attributes"]["label"]
        sections.append(f"## {label}\n{values[field_id]}")
    return "\n\n".join(sections) + "\n"


def test_form_uses_machine_readable_metric_and_reproducibility_fields() -> None:
    fields = form_fields()
    structured_ids = {
        "primary_metric_name",
        "primary_metric_direction",
        "minimum_primary_delta",
        "guardrail_metric_name",
        "guardrail_metric_direction",
        "maximum_guardrail_regression",
        "dataset_snapshot",
        "random_seeds",
        "split_seed",
        "test_size",
        "validation_size",
        "training_config_ref",
    }

    assert fields.keys() >= structured_ids
    assert all(fields[field_id]["validations"]["required"] for field_id in structured_ids)
    assert "success_criteria" not in fields
    assert "reproducibility" not in fields
    assert fields.keys() >= {
        "hypothesis",
        "change",
        "secondary_metrics",
        "comparison",
        "dataset",
        "snapshot_reuse",
        "allowed_scope",
        "result",
    }
    assert fields["primary_metric_direction"]["attributes"]["options"] == [
        "higher_is_better",
        "lower_is_better",
    ]
    assert fields["guardrail_metric_direction"]["attributes"]["options"] == [
        "not_applicable",
        "higher_is_better",
        "lower_is_better",
    ]
    assert fields["guardrail_metric_name"]["attributes"]["value"] == "없음"
    assert fields["maximum_guardrail_regression"]["attributes"]["value"] == "없음"


def test_issue_branch_workflow_has_minimum_permissions_and_exact_issue_trigger() -> None:
    workflow = load_branch_workflow()

    assert workflow["permissions"] == {"contents": "write", "issues": "write"}
    assert workflow_trigger(workflow) == {"issues": {"types": ["opened", "labeled"]}}


def test_issue_branch_workflow_uses_validator_and_never_updates_a_ref() -> None:
    workflow = load_branch_workflow()
    workflow_text = BRANCH_WORKFLOW.read_text(encoding="utf-8")
    job = workflow["jobs"]["create-or-verify-issue-branch"]
    assert isinstance(job, dict)
    job_if = job["if"]
    assert isinstance(job_if, str)
    assert "auto-research" in job_if
    assert "experiment" in job_if
    assert job["concurrency"] == {
        "group": "auto-research-issue-branch-${{ github.event.issue.number }}",
        "cancel-in-progress": False,
    }

    steps = job["steps"]
    assert isinstance(steps, list)
    checkout_step = next(step for step in steps if step["name"] == "Checkout dev validator")
    assert checkout_step["with"] == {"ref": "dev", "fetch-depth": 1}
    validate_step = next(step for step in steps if step.get("id") == "validate")
    validate_run = validate_step["run"]
    assert isinstance(validate_run, str)
    assert "tools/auto_research_issue_branch.py" in validate_run
    assert "--issue-number \"$ISSUE_NUMBER\"" in validate_run
    assert "--issue-title \"$ISSUE_TITLE\"" in validate_run

    branch_step = next(step for step in steps if step.get("id") == "branch")
    branch_with = branch_step["with"]
    assert isinstance(branch_with, dict)
    branch_script = branch_with["script"]
    assert isinstance(branch_script, str)
    assert "auto-research-issue-branch:v1" in branch_script
    assert "createRef" in branch_script
    assert "compareCommits" in branch_script
    assert "github-actions[bot]" in branch_script
    assert "base_dev_sha" in branch_script
    assert "ref: 'heads/dev'" in branch_script
    assert "main" not in branch_script
    assert "updateRef" not in branch_script
    assert "force:" not in branch_script
    assert "기준선은 항상 dev" in workflow_text


def test_parse_issue_input_reads_body_rendered_from_actual_form() -> None:
    issue_input = parse_issue_input(449, "[AR] CTR ratio", body_rendered_from_form())

    assert issue_input.issue_branch == "exp/449-ctr-ratio"
    assert issue_input.primary_metric_name == "roc_auc"
    assert issue_input.primary_metric_direction == "higher_is_better"
    assert issue_input.minimum_primary_delta == 0.002
    assert issue_input.guardrail_metric_name is None
    assert issue_input.guardrail_metric_direction == "not_applicable"
    assert issue_input.maximum_guardrail_regression is None
    assert issue_input.dataset_snapshot == "bq://autoresearch/train@2026-07-31"
    assert issue_input.random_seeds == (42, 43, 44)
    assert issue_input.split_seed == 20260731
    assert issue_input.test_size == 0.2
    assert issue_input.validation_size == 0.2
    assert issue_input.training_config_ref == "configs/train/lgbm-v1.yaml@abc1234"
    assert len(issue_input.criteria_id) == 64
    assert len(issue_input.reproducibility_id) == 64


def test_parse_issue_input_reads_configured_guardrail() -> None:
    issue_input = parse_issue_input(
        449,
        "[AR] CTR ratio",
        structured_body(
            guardrail_metric_name="log_loss",
            guardrail_metric_direction="lower_is_better",
            maximum_guardrail_regression="0.01",
        ),
    )

    assert issue_input.guardrail_metric_name == "log_loss"
    assert issue_input.guardrail_metric_direction == "lower_is_better"
    assert issue_input.maximum_guardrail_regression == 0.01


@pytest.mark.parametrize("metric_name", ["", "1auc", "한글", "a b", "a/b", "a" * 65])
def test_parse_issue_input_rejects_invalid_primary_metric_name(metric_name: str) -> None:
    with pytest.raises(ValueError, match="primary_metric_name"):
        parse_issue_input(449, "[AR] metric", structured_body(primary_metric_name=metric_name))


@pytest.mark.parametrize("direction", ["not_applicable", "maximize", ""])
def test_parse_issue_input_rejects_invalid_primary_metric_direction(direction: str) -> None:
    with pytest.raises(ValueError, match="primary_metric_direction"):
        parse_issue_input(449, "[AR] metric", structured_body(primary_metric_direction=direction))


@pytest.mark.parametrize("delta", ["NaN", "Infinity", "-0.001", "not-a-number"])
def test_parse_issue_input_rejects_invalid_primary_delta(delta: str) -> None:
    with pytest.raises(ValueError, match="minimum_primary_delta"):
        parse_issue_input(449, "[AR] metric", structured_body(minimum_primary_delta=delta))


def test_parse_issue_input_accepts_zero_primary_delta() -> None:
    assert (
        parse_issue_input(449, "[AR] metric", structured_body(minimum_primary_delta="0"))
        .minimum_primary_delta
        == 0
    )


def test_parse_issue_input_rejects_primary_delta_that_overflows_float() -> None:
    with pytest.raises(ValueError, match="minimum_primary_delta"):
        parse_issue_input(
            449,
            "[AR] metric",
            structured_body(minimum_primary_delta="1" + "0" * 400),
        )


@pytest.mark.parametrize(
    ("name", "direction", "maximum_regression"),
    [
        ("없음", "higher_is_better", "없음"),
        ("없음", "not_applicable", "0"),
        ("log_loss", "not_applicable", "0.01"),
        ("log_loss", "lower_is_better", "없음"),
        ("1invalid", "lower_is_better", "0.01"),
        ("log_loss", "lower_is_better", "-0.01"),
        ("log_loss", "lower_is_better", "NaN"),
    ],
)
def test_parse_issue_input_rejects_inconsistent_guardrail(
    name: str,
    direction: str,
    maximum_regression: str,
) -> None:
    with pytest.raises(ValueError, match="guardrail"):
        parse_issue_input(
            449,
            "[AR] metric",
            structured_body(
                guardrail_metric_name=name,
                guardrail_metric_direction=direction,
                maximum_guardrail_regression=maximum_regression,
            ),
        )


def test_parse_issue_input_rejects_guardrail_regression_that_overflows_float() -> None:
    with pytest.raises(ValueError, match="maximum_guardrail_regression"):
        parse_issue_input(
            449,
            "[AR] metric",
            structured_body(
                guardrail_metric_name="log_loss",
                guardrail_metric_direction="lower_is_better",
                maximum_guardrail_regression="1" + "0" * 400,
            ),
        )


@pytest.mark.parametrize("field_name", ["dataset_snapshot", "training_config_ref"])
@pytest.mark.parametrize("value", ["", " ", "x" * 257])
def test_parse_issue_input_rejects_invalid_text_reference(field_name: str, value: str) -> None:
    with pytest.raises(ValueError, match=field_name):
        parse_issue_input(449, "[AR] metric", structured_body(**{field_name: value}))


@pytest.mark.parametrize("random_seeds", ["", "42, 42", "42, -1", "42, 1.5", "42,"])
def test_parse_issue_input_rejects_invalid_random_seeds(random_seeds: str) -> None:
    with pytest.raises(ValueError, match="random_seeds"):
        parse_issue_input(449, "[AR] metric", structured_body(random_seeds=random_seeds))


@pytest.mark.parametrize("split_seed", ["", "-1", "1.5", "seed"])
def test_parse_issue_input_rejects_invalid_split_seed(split_seed: str) -> None:
    with pytest.raises(ValueError, match="split_seed"):
        parse_issue_input(449, "[AR] metric", structured_body(split_seed=split_seed))


@pytest.mark.parametrize(
    ("test_size", "validation_size"),
    [
        ("0", "0.2"),
        ("0.2", "0"),
        ("1", "0.2"),
        ("0.7", "0.3"),
        ("NaN", "0.2"),
        ("0.2", "Infinity"),
        ("1e-400", "0.2"),
        ("0.2", "1e-400"),
        ("0.1", "0.89999999999999999"),
    ],
)
def test_parse_issue_input_rejects_invalid_split_sizes(
    test_size: str,
    validation_size: str,
) -> None:
    with pytest.raises(ValueError, match="test_size|validation_size"):
        parse_issue_input(
            449,
            "[AR] metric",
            structured_body(test_size=test_size, validation_size=validation_size),
        )


@pytest.mark.parametrize(
    ("changed_field", "changed_value", "identifier_name"),
    [
        ("primary_metric_name", "pr_auc", "criteria_id"),
        ("primary_metric_direction", "lower_is_better", "criteria_id"),
        ("minimum_primary_delta", "0.003", "criteria_id"),
        ("guardrail_metric_name", "log_loss", "criteria_id"),
        ("dataset_snapshot", "bq://autoresearch/train@2026-08-01", "reproducibility_id"),
        ("random_seeds", "45, 46, 47", "reproducibility_id"),
        ("split_seed", "7", "reproducibility_id"),
        ("test_size", "0.25", "reproducibility_id"),
        ("validation_size", "0.25", "reproducibility_id"),
        ("training_config_ref", "configs/train/lgbm-v2.yaml@def5678", "reproducibility_id"),
    ],
)
def test_identifiers_include_each_structured_field(
    changed_field: str,
    changed_value: str,
    identifier_name: str,
) -> None:
    original = parse_issue_input(449, "[AR] metric", structured_body())
    changes = {changed_field: changed_value}
    if changed_field == "guardrail_metric_name":
        changes.update(
            guardrail_metric_direction="lower_is_better",
            maximum_guardrail_regression="0.01",
        )
    changed = parse_issue_input(449, "[AR] metric", structured_body(**changes))

    assert getattr(changed, identifier_name) != getattr(original, identifier_name)
    other_identifier = "reproducibility_id" if identifier_name == "criteria_id" else "criteria_id"
    assert getattr(changed, other_identifier) == getattr(original, other_identifier)


def test_criteria_id_includes_guardrail_direction_and_maximum_regression() -> None:
    original = parse_issue_input(
        449,
        "[AR] metric",
        structured_body(
            guardrail_metric_name="log_loss",
            guardrail_metric_direction="lower_is_better",
            maximum_guardrail_regression="0.01",
        ),
    )
    changed_direction = parse_issue_input(
        449,
        "[AR] metric",
        structured_body(
            guardrail_metric_name="log_loss",
            guardrail_metric_direction="higher_is_better",
            maximum_guardrail_regression="0.01",
        ),
    )
    changed_maximum = parse_issue_input(
        449,
        "[AR] metric",
        structured_body(
            guardrail_metric_name="log_loss",
            guardrail_metric_direction="lower_is_better",
            maximum_guardrail_regression="0.02",
        ),
    )

    assert changed_direction.criteria_id != original.criteria_id
    assert changed_maximum.criteria_id != original.criteria_id


def test_identifiers_canonicalize_equivalent_decimal_spellings() -> None:
    original = parse_issue_input(
        449,
        "[AR] metric",
        structured_body(
            minimum_primary_delta="0.20",
            guardrail_metric_name="log_loss",
            guardrail_metric_direction="lower_is_better",
            maximum_guardrail_regression="0.010",
            test_size="0.20",
            validation_size="0.20",
        ),
    )
    equivalent = parse_issue_input(
        449,
        "[AR] metric",
        structured_body(
            minimum_primary_delta="0.2",
            guardrail_metric_name="log_loss",
            guardrail_metric_direction="lower_is_better",
            maximum_guardrail_regression="0.01",
            test_size="0.2",
            validation_size="0.2",
        ),
    )

    assert equivalent.criteria_id == original.criteria_id
    assert equivalent.reproducibility_id == original.reproducibility_id


def test_parse_issue_input_rejects_legacy_unstructured_headings() -> None:
    legacy_body = structured_body().replace(
        "## 주 지표 이름\nroc_auc\n\n",
        "## 성공 기준 — 주 지표 1개와 수치 임계\nROC-AUC +0.002\n\n",
    )

    with pytest.raises(ValueError, match="unknown Issue Form heading"):
        parse_issue_input(449, "[AR] metric", legacy_body)


def test_parse_issue_input_rejects_missing_structured_heading() -> None:
    body = structured_body().replace("## Split 시드\n20260731\n\n", "")

    with pytest.raises(ValueError, match="split_seed"):
        parse_issue_input(449, "[AR] metric", body)


def test_parse_issue_input_rejects_duplicate_structured_heading() -> None:
    body = structured_body().replace(
        "## Split 시드\n20260731",
        "## Split 시드\n20260731\n\n## Split 시드\n7",
    )

    with pytest.raises(ValueError, match="duplicate Issue Form heading"):
        parse_issue_input(449, "[AR] metric", body)


def test_parse_issue_input_rejects_unknown_selected_scope() -> None:
    with pytest.raises(ValueError, match="allowed_scope"):
        parse_issue_input(
            449,
            "[AR] metric",
            structured_body(allowed_scope="- [x] 배포 워크플로우를 수정한다"),
        )


def test_identifiers_ignore_human_oriented_free_text() -> None:
    original = parse_issue_input(449, "[AR] metric", structured_body())
    changed_body = structured_body().replace(
        "비율 피처가 ROC-AUC를 높인다.",
        "사람이 읽는 가설 설명을 보완한다.",
    ).replace(
        "- 데이터셋 / 경로: data/train.csv",
        "- 데이터셋 / 경로: 설명용 별칭",
    )
    changed = parse_issue_input(449, "[AR] metric", changed_body)

    assert changed.criteria_id == original.criteria_id
    assert changed.reproducibility_id == original.reproducibility_id


def test_identifiers_ignore_fields_outside_fixed_structured_contracts() -> None:
    original = parse_issue_input(449, "[AR] metric", structured_body())
    changed_issue_number = parse_issue_input(450, "[AR] metric", structured_body())
    changed_comparison = parse_issue_input(
        449,
        "[AR] metric",
        structured_body().replace("동일 조건 baseline 재학습 (권장)", "둘 다"),
    )
    changed_snapshot_reuse = parse_issue_input(
        449,
        "[AR] metric",
        structured_body().replace(
            "허용 (진행하되 실제로 쓴 데이터를 결과에 명시)",
            "불허 (정규 조립 경로 실패 시 중단)",
        ),
    )
    changed_scope = parse_issue_input(
        449,
        "[AR] metric",
        structured_body(
            allowed_scope="- [x] prod 모델 계약(`src/features/model_contract.py`) 수정을 허용한다"
        ),
    )

    for changed in (
        changed_issue_number,
        changed_comparison,
        changed_scope,
        changed_snapshot_reuse,
    ):
        assert changed.criteria_id == original.criteria_id
        assert changed.reproducibility_id == original.reproducibility_id


def test_branch_name_is_single_issue_coordinate() -> None:
    assert branch_name_for(449, "[AR] CTR ratio") == "exp/449-ctr-ratio"


def test_branch_name_uses_deterministic_ascii_fallback_for_korean_title() -> None:
    assert branch_name_for(449, "[AR] 비율 피처 실험") == "exp/449-issue-09a97f67112d"


@pytest.mark.parametrize(
    ("compare_status", "expected"),
    [("ahead", True), ("identical", True), ("behind", False), ("diverged", False)],
)
def test_is_descendant_accepts_only_ancestor_safe_statuses(
    compare_status: str,
    expected: bool,
) -> None:
    assert is_descendant(compare_status) is expected


def run_cli(tmp_path: Path, body: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    """실제 CLI를 임시 이슈 본문과 GitHub output 경로로 실행합니다."""
    body_path = tmp_path / "issue.md"
    output_path = tmp_path / "github-output.txt"
    body_path.write_text(body, encoding="utf-8")
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
    return result, output_path


def test_cli_writes_only_issue_contract_outputs(tmp_path: Path) -> None:
    result, output_path = run_cli(tmp_path, structured_body())
    issue_input = parse_issue_input(449, "[AR] CTR ratio", structured_body())

    assert result.returncode == 0, result.stderr
    assert output_path.read_text(encoding="utf-8").splitlines() == [
        "issue_branch=exp/449-ctr-ratio",
        "criteria_id=" + issue_input.criteria_id,
        "reproducibility_id=" + issue_input.reproducibility_id,
    ]


def test_cli_fails_closed_without_outputs_for_invalid_form_body(tmp_path: Path) -> None:
    result, output_path = run_cli(tmp_path, structured_body(random_seeds="42, 42"))

    assert result.returncode != 0
    assert "random_seeds" in result.stderr
    assert not output_path.exists()
