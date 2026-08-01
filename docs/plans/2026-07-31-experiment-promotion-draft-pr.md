# 실험 Draft main PR 승격 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 구조화 metric 기준을 통과한 dev 후보를 immutable promotion branch와 Draft main PR로 전환한다.

**Architecture:** 순수 Python gate가 issue criteria와 result payload를 검증하고, GitHub Actions가 통과한 SHA의 promotion ref와 PR만 만든다.

**Tech Stack:** Python dataclasses/argparse, GitHub Actions, actions/github-script, pytest

## Global Constraints

- 자동 병합·champion alias 변경·prod 배포를 수행하지 않는다.
- candidate SHA는 dev 조상이어야 한다.
- 기준 미달은 오류가 아닌 no-op이다.

---

### Task 1: 순수 promotion gate

**Files:**
- Create: `autoresearch/experiments/promotion_gate.py`
- Create: `autoresearch/experiments/__init__.py`
- Create: `tests/test_experiment_promotion_gate.py`

- [ ] **Step 1:** higher/lower metric, guardrail, 잘못된 payload를 검증하는 실패 테스트를 작성한다.
- [ ] **Step 2:** 테스트가 module 부재로 실패하는지 실행한다.
- [ ] **Step 3:** issue body parser와 result evaluator를 최소 구현한다.
- [ ] **Step 4:** focused pytest를 실행한다.

### Task 2: promotion workflow

**Files:**
- Create: `.github/workflows/auto-research-promotion.yml`
- Modify: `tests/test_experiment_promotion_gate.py`

- [ ] **Step 1:** dispatch, permissions, dev ancestry, immutable ref, Draft PR 계약의 실패 테스트를 작성한다.
- [ ] **Step 2:** workflow 부재로 실패하는지 실행한다.
- [ ] **Step 3:** payload 추출·gate·GitHub Script workflow를 추가한다.
- [ ] **Step 4:** focused pytest, ruff, diff 검사를 실행한다.
