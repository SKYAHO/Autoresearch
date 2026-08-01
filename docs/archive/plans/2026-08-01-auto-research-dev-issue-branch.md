# Auto Research dev 기준 단일 이슈 브랜치 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** #449 Auto Research 이슈의 검증된 기준선을 `dev`에서 단일·불변 작업 브랜치로 만든다.

**Architecture:** 표준 라이브러리 Python validator가 Issue Form 본문을 파싱하고 구조화 성공 기준·재현 조건·branch/marker 계약을 검증한다. GitHub Actions는 validator output을 소비해 GitHub REST API로 한 번만 ref를 만들고 baseline lineage를 검사한 뒤 이슈에 기록한다. 완료 event의 후보 집합은 같은 모듈의 순수 selection gate로 판정하고, 최선 적격 후보만 `dev` merge API에 전달한다.

**Tech Stack:** Python 3.11+, pytest, PyYAML, GitHub Issue Forms, GitHub Actions, `actions/github-script@v8`.

## Global Constraints

- branch 형식은 `exp/<issue>-<slug>`이며 이슈당 하나만 만든다.
- branch 생성 기준선은 현재 `dev` SHA 한 번이고 output/comment 이름은 `base_dev_sha`다.
- branch 재실행은 baseline descendant를 허용하되 ref를 이동하지 않는다.
- 모든 구조화 numeric/reproducibility 입력은 fail-closed로 검증한다.
- selection threshold는 parse부터 gate까지 `Decimal`으로 유지하며, completion 후보는 최대
  50개, Decimal 문자열은 128자·64 digits·절댓값 1,000 exponent, artifact/log 식별자는
  2,048자로 제한한다.
- gate subtraction과 guardrail subtraction은 최소 136자리(현재 지수 한도에서는 2,072자리)
  `Decimal` local context를 사용하고, 후보 순위는 unary minus 정렬 대신 직접 `Decimal` 비교와
  SHA tie-break로 결정한다.
- candidate snapshot과 실험 Job 자체는 구현하지 않지만, 완료 event의 후보 결과 집합을 받아 결과 판정과 `dev` 병합을 구현한다.
- 권한 있는 workflow는 `github.workflow_sha`를 credential 없이 checkout하고, completion
  selector 자식 process에는 최소 allowlist 환경만 전달한다.
- mutation 없는 좌표 검증 job은 `permissions: {}`로 raw issue number와 experiment ID를
  canonical output으로 검증하고, promotion job은 전역 `auto-research-dev-promotion` group의
  `queue: max`로 dev merge를 직렬화한다. 후보 수는 workflow JSON parse 직후에도 1~50으로
  제한한다.
- `main` ref·main PR·prod 배포·champion alias는 workflow가 생성·갱신·병합하지 않는다.
- GitHub event 수신을 위해 workflow 정의는 기본 브랜치 `main`에도 반영하되, `main`은
  정의 위치일 뿐 자동 merge 대상이 아니며 모든 후보 ref 조작과 merge base는 `dev`로 고정한다.
- 커밋 제목은 `<type>: <한국어 설명>`이며 완료 문서는 `docs/archive/`로 옮긴다.

---

### Task 1: 검증 가능한 이슈 입력과 branch 계약

**Files:**

- Create: `tools/auto_research_issue_branch.py`
- Create: `tests/test_auto_research_issue_branch.py`

**Interfaces:**

- Produces: `parse_issue_input(issue_number: int, issue_title: str, issue_body: str) -> IssueInput`
- Produces: `branch_name_for(issue_number: int, issue_title: str) -> str`
- Produces: `is_descendant(compare_status: str) -> bool`
- Produces: CLI output keys `issue_branch`, `criteria_id`, `reproducibility_id`.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_issue_input_rejects_nan_delta() -> None:
    with pytest.raises(ValueError, match="minimum_primary_delta"):
        parse_issue_input(449, "[AR] metric", valid_body(minimum_primary_delta="NaN"))

def test_branch_name_is_single_issue_coordinate() -> None:
    assert branch_name_for(449, "[AR] CTR ratio") == "exp/449-ctr-ratio"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_auto_research_issue_branch.py -v`
Expected: import error because validator module does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
def is_descendant(compare_status: str) -> bool:
    return compare_status in {"ahead", "identical"}
```

본문 heading parser, finite decimal·seed·split·guardrail cross validation, deterministic criteria/reproducibility SHA-256 identifier와 GitHub output CLI를 추가한다.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_auto_research_issue_branch.py -v`
Expected: 모든 validator 계약 테스트 PASS.

- [ ] **Step 5: Commit**

```bash
git add tools/auto_research_issue_branch.py tests/test_auto_research_issue_branch.py
git commit -m "feat: Auto Research 이슈 입력 검증 추가"
```

### Task 2: 구조화 Issue Form과 validator 계약

**Files:**

- Modify: `.github/ISSUE_TEMPLATE/auto_research.yml`
- Modify: `tools/auto_research_issue_branch.py`
- Modify: `tests/test_auto_research_issue_branch.py`

**Interfaces:**

- Consumes: Task 1 parser·slug·GitHub output contract.
- Produces: metric direction·guardrail·dataset snapshot·split/seed·training config을 포함한
  `IssueInput` 및 criteria/reproducibility 식별자.

- [ ] **Step 1: Write the failing tests**

```python
def test_form_uses_machine_readable_metric_and_reproducibility_fields() -> None:
    assert form_field_ids() >= {"primary_metric_name", "minimum_primary_delta", "dataset_snapshot", "random_seeds", "test_size", "validation_size", "training_config_ref"}

```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_auto_research_issue_branch.py -v`
Expected: structured field assertion FAIL.

- [ ] **Step 3: Write minimal implementation**

form의 자유 문장 성공 기준을 주 지표 이름·방향·최소 개선폭·선택 guardrail 필드로 바꾸고, dataset snapshot·random seed·split·training config의 기계 판독 필드를 추가한다. Task 1 validator를 같은 headings·유한 수치·guardrail cross validation 규칙으로 갱신하고 모든 identifier가 구조화 계약을 바꿀 때만 달라지게 한다.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_auto_research_issue_branch.py -v`
Expected: 모든 focused 테스트 PASS.

Run: `ruby -e "require 'yaml'; YAML.load_file('.github/ISSUE_TEMPLATE/auto_research.yml')"`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git add .github/ISSUE_TEMPLATE/auto_research.yml tools/auto_research_issue_branch.py tests/test_auto_research_issue_branch.py
git commit -m "feat: Auto Research 이슈 입력 계약 구조화"
```

### Task 3: 불변 issue branch workflow

**Files:**

- Create: `.github/workflows/auto-research-issue-branch.yml`

**Interfaces:**

- Consumes: Task 2 validator CLI and `issue_branch`, `criteria_id`, `reproducibility_id` outputs.
- Produces: `base_dev_sha` workflow output and one trusted marker comment.

- [ ] **Step 1: Write the failing workflow validation test**

```python
def test_issue_branch_workflow_is_valid_yaml() -> None:
    assert yaml.safe_load(BRANCH_WORKFLOW.read_text(encoding="utf-8"))["permissions"] == {"contents": "write", "issues": "write"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_auto_research_issue_branch.py -v`
Expected: workflow file missing error.

- [ ] **Step 3: Write minimal implementation**

`opened`/`labeled` event에서 Task 2 CLI를 실행하고, 두 label이 모두 있는 이슈만 처리한다.
marker가 없으면 `heads/dev`에서 `issue_branch` ref를 만든 뒤 `base_dev_sha`·criteria/reproducibility
ID를 bot comment·output에 남긴다. marker가 있으면 recorded branch와 baseline을 parse하고 branch
descendant만 허용한다. ref는 어떤 재실행에서도 이동하지 않으며 malformed marker·missing ref·unrelated
ref는 fail-closed한다.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_auto_research_issue_branch.py -v`
Run: `actionlint .github/workflows/auto-research-issue-branch.yml`
Expected: focused tests PASS; `actionlint` 설치 시 exit 0.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/auto-research-issue-branch.yml tests/test_auto_research_issue_branch.py
git commit -m "feat: Auto Research 이슈 브랜치 생성 추가"
```

### Task 4: 완료 후보 선택과 dev 자동 병합

**Files:**

- Modify: `tools/auto_research_issue_branch.py`
- Modify: `tests/test_auto_research_issue_branch.py`
- Create: `.github/workflows/auto-research-dev-promotion.yml`

**Interfaces:**

- Consumes: issue body, recorded `base_dev_sha`·`issue_branch`·criteria/reproducibility IDs,
  `experiment_id`, JSON 후보 배열.
- Produces: `selected_candidate_sha`, `selection_reason`, deterministic result-set ID.
- Calls: GitHub merge API with `base: dev` only after the complete candidate set passes validation.

- [ ] **Step 1: Write the failing tests**

```python
def test_select_best_candidate_uses_primary_direction_then_sha() -> None:
    selection = select_best_candidate(criteria, [qualified("b" * 40, 0.79), qualified("a" * 40, 0.79)])
    assert selection.candidate_sha == "a" * 40

def test_select_best_candidate_rejects_one_mismatched_identifier() -> None:
    with pytest.raises(ValueError, match="criteria_id"):
        select_best_candidate(criteria, [qualified("a" * 40, 0.79), qualified("b" * 40, 0.80, criteria_id="wrong")])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_auto_research_issue_branch.py -v`
Expected: selection API import error or assertion failure.

- [ ] **Step 3: Write minimal implementation**

모든 후보 JSON schema·bounded Decimal metric·issue criteria/reproducibility ID를 검증하고,
direction·minimum delta·guardrail로 적격 후보를 결정한다. 완료 event의 result-set ID와
선택 SHA를 GitHub output으로 기록한다. workflow는 모든 candidate가 recorded baseline의
descendant이자 issue branch의 ancestor임을 확인하고, selector SHA가 그 lineage 통과 집합에
포함될 때만 진행한다. 새 적격 result-set은 source issue에 strict `pending` marker를 먼저
생성한 뒤 `github.rest.repos.merge({base: 'dev', head: selectedCandidateSha})`를 호출한다.
201/204는 `merged`, 409은 `merge_conflict`, 그 밖의 실패는 `merge_api_failed`로 marker를
update하며 update 실패는 pending reconciliation으로 복구한다. mutation 없는 선행 job이
canonical issue number·experiment ID output을 만들고, promotion은 전역
`auto-research-dev-promotion` group의 `queue: max`에서 직렬 실행한다. 후보 수는 JSON parse
직후 object/lineage loop 전에 1~50으로 제한하며 `main` base·PR·ref API는 사용하지 않는다.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/test_auto_research_issue_branch.py -v`
Run: `actionlint .github/workflows/auto-research-dev-promotion.yml`
Expected: focused tests PASS; `actionlint` 설치 시 exit 0.

- [ ] **Step 5: Commit**

```bash
git add tools/auto_research_issue_branch.py tests/test_auto_research_issue_branch.py .github/workflows/auto-research-dev-promotion.yml
git commit -m "feat: 최선 실험 후보를 dev에 병합"
```

### Task 5: 문서 수명과 전체 검증

**Files:**

- Move: `docs/specs/2026-08-01-auto-research-dev-issue-branch.md` → `docs/archive/specs/2026-08-01-auto-research-dev-issue-branch.md`
- Move: `docs/plans/2026-08-01-auto-research-dev-issue-branch.md` → `docs/archive/plans/2026-08-01-auto-research-dev-issue-branch.md`

- [ ] **Step 1: 완료된 spec/plan archive 이동**

`docs/README.md`의 완료 문서 수명 규칙에 따라 두 문서를 archive 하위에 둔다.

- [ ] **Step 2: 전체 검증 실행**

Run: `uv run python -m pytest -v`
Run: `uv run --no-sync ruff check agent_orchestration autoresearch tests tools`
Run: `git diff --check`

Expected: 각 명령 exit 0.

- [ ] **Step 3: Commit**

```bash
git add docs/archive/specs/2026-08-01-auto-research-dev-issue-branch.md docs/archive/plans/2026-08-01-auto-research-dev-issue-branch.md
git commit -m "docs: Auto Research 이슈 브랜치 계획 보관"
```
