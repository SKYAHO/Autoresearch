# Main/Dev 배포 경로 분리 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** main을 prod 전용, dev를 dev 전용 Feast 배포 경로로 만들고 릴리스와 main 병합 보호를 강화한다.

**Architecture:** `.github/workflows/feast-apply.yml`은 ref 이름 또는 수동 입력에서 단일 대상 환경을 계산한다. GitHub Environment branch policy와 main ruleset은 저장소 API로 이 코드 계약을 강제한다. release workflow는 대상 커밋의 main 계보를 검사한다.

**Tech Stack:** GitHub Actions YAML, GitHub REST API, pytest, gh CLI

## Global Constraints

- main은 prod, dev는 dev 외 환경으로 자동 배포하지 않는다.
- prod release source SHA는 origin/main의 조상이어야 한다.
- Airflow 및 GCP 리소스 정의는 이 저장소에서 수정하지 않는다.
- workflow 동작 변경에는 회귀 테스트를 추가한다.

---

### Task 1: Feast main/dev 환경 선택

**Files:**
- Modify: `.github/workflows/feast-apply.yml`
- Modify: `.github/workflows/code-archive.yml`
- Modify: `tests/test_feast_apply_workflow.py`

**Interfaces:**
- Consumes: `github.ref_name`, `inputs.environment`
- Produces: `environment`, `AUTORESEARCH_ENV`, `FEAST_ONLINE_FULL_SCAN_FOR_DELETION`

- [ ] **Step 1: Write failing workflow-contract tests**

```python
def test_feast_apply_routes_dev_push_to_dev_environment() -> None:
    assert "github.ref_name" in workflow
    assert "dev" in push_branches
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync python -m pytest -q tests/test_feast_apply_workflow.py`
Expected: dev push route assertion failure.

- [ ] **Step 3: Add minimal workflow branch selector**

Use `github.ref_name` for push events and the existing dispatch input otherwise; preserve explicit prod/dev full-scan values. Add `dev` to code archive push triggers so the dev apply Job can wait for the immutable archive without updating `code/latest.txt`.

- [ ] **Step 4: Run focused tests**

Run: `uv run --no-sync python -m pytest -q tests/test_feast_apply_workflow.py`
Expected: PASS.

### Task 2: Release main ancestry guard

**Files:**
- Modify: `.github/workflows/release.yml`
- Modify: `tests/test_release_workflow.py`

**Interfaces:**
- Consumes: checked-out `source_sha`, `origin/main`
- Produces: early non-zero exit for a non-main source SHA

- [ ] **Step 1: Write failing workflow-contract test**

```python
def test_release_workflow_requires_main_ancestor() -> None:
    assert "merge-base --is-ancestor" in workflow
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --no-sync python -m pytest -q tests/test_release_workflow.py`
Expected: ancestry guard assertion failure.

- [ ] **Step 3: Add guard after immutable checkout**

Fetch `origin/main` and stop before any cloud authentication when `source_sha` is not its ancestor.

- [ ] **Step 4: Run focused tests**

Run: `uv run --no-sync python -m pytest -q tests/test_release_workflow.py`
Expected: PASS.

### Task 3: GitHub policy reconciliation and verification

**Files:**
- Modify: no repository file; GitHub Environment/ruleset settings

**Interfaces:**
- Consumes: ruleset `main-protection`, Environment names `prod` and `dev`
- Produces: prod allows `main`, dev allows `dev`; main requires approved, current CI, resolved discussions PRs

- [ ] **Step 1: Read current API configuration**

Run: `gh api repos/SKYAHO/Autoresearch/rulesets/18360502`

- [ ] **Step 2: Apply exact branch policies and ruleset update**

Keep squash-only and require reviews, current push approval, conversation resolution, Lint and both pytest suites.

- [ ] **Step 3: Read back settings and run all workflow tests**

Run: `uv run --no-sync python -m pytest -q tests/test_release_workflow.py tests/test_feast_apply_workflow.py`
Expected: PASS, with API output matching the specification.
