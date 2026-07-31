# Auto Research 실험 브랜치 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 가설 이슈의 구조화 성공 기준을 강제하고 dev 기반 실험 브랜치를 자동 생성한다.

**Architecture:** Issue Form이 구조화 입력을 수집하고, `issues` workflow가 GitHub Script로 dev SHA 기반 ref를 idempotent하게 만든다.

**Tech Stack:** GitHub Issue Forms, GitHub Actions, actions/github-script, pytest

## Global Constraints

- 실험 브랜치는 `dev`에서만 만든다.
- branch 생성은 자동 배포·자동 병합을 수행하지 않는다.
- 이슈 이벤트 재실행은 기존 ref를 이동하지 않는다.

---

### Task 1: 구조화 성공 기준

**Files:**
- Modify: `.github/ISSUE_TEMPLATE/auto_research.yml`
- Create: `tests/test_auto_research_issue_form.py`

- [ ] **Step 1:** 구조화 필드의 이름·선택지·필수 여부를 검증하는 실패 테스트를 작성한다.
- [ ] **Step 2:** 테스트가 기존 자유 문장 필드 때문에 실패하는지 실행한다.
- [ ] **Step 3:** 주 지표·방향·최소 개선폭과 선택 guardrail 필드를 Issue Form에 추가한다.
- [ ] **Step 4:** Issue Form 테스트를 실행한다.

### Task 2: exp primary branch workflow

**Files:**
- Create: `.github/workflows/auto-research-experiment-branch.yml`
- Modify: `tests/test_auto_research_issue_form.py`

- [ ] **Step 1:** label gate, dev ref, idempotent ref 생성, 최소 권한을 검증하는 실패 테스트를 작성한다.
- [ ] **Step 2:** 테스트가 workflow 부재로 실패하는지 실행한다.
- [ ] **Step 3:** GitHub Script workflow를 추가한다.
- [ ] **Step 4:** focused pytest와 diff 검사를 실행한다.
