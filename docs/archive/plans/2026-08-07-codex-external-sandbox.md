# Codex External Sandbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 외부에서 격리된 executor Pod 안의 Codex가 저장소의 `.agents -> .claude` 심볼릭 링크 때문에 내부 sandbox 초기화에 실패하지 않도록 한다.

**Architecture:** Kubernetes Pod의 비루트 실행, read-only root filesystem, `.git` read-only mount, 제한된 egress와 candidate verifier를 실행 경계로 유지한다. Codex CLI에는 외부 격리 환경 전용 `--dangerously-bypass-approvals-and-sandbox`를 전달하고 중복되는 내부 `workspace-write` sandbox를 제거한다.

**Tech Stack:** Python 3.12, pytest, Codex CLI 0.147.0, Kubernetes executor Job

## Global Constraints

- `codex_worker`의 environment allowlist, auth scratch copy, timeout과 process-group 회수는 변경하지 않는다.
- `.git` read-only mount 검증과 실행 전후 metadata digest 검증은 유지한다.
- candidate verifier의 허용 경로·검증 명령·finalizer 계약은 변경하지 않는다.
- 새 의존성이나 Kubernetes 권한은 추가하지 않는다.

---

### Task 1: 외부 sandbox 전용 Codex argv 계약

**Files:**
- Modify: `tests/test_experiment_codex_worker.py`
- Modify: `agent_orchestration/executor/codex_worker.py`
- Modify: `docs/specs/2026-08-06-experiment-executor-phase2.md`

**Interfaces:**
- Consumes: `run_codex(run: CodexRunInput) -> CodexRunResult`
- Produces: `codex exec --ephemeral --dangerously-bypass-approvals-and-sandbox -C <repository> <prompt>` argv 계약

- [x] **Step 1: 고정 argv 테스트를 실패하도록 갱신한다**

`test_run_codex_uses_fixed_argv_and_allowlisted_environment`의 기대 argv를 아래처럼 바꾼다.

```python
assert json.loads(argv_path.read_text(encoding="utf-8")) == [
    "exec",
    "--ephemeral",
    "--dangerously-bypass-approvals-and-sandbox",
    "-C",
    str(run.repository),
    prompt,
]
```

- [x] **Step 2: RED를 확인한다**

Run: `uv run python -m pytest tests/test_experiment_codex_worker.py::test_run_codex_uses_fixed_argv_and_allowlisted_environment -v`

Expected: 실제 argv에 `--sandbox`, `workspace-write`가 남아 있어 assertion failure.

- [x] **Step 3: 최소 구현과 책임 문서를 갱신한다**

`run_codex` argv에서 `--sandbox`, `workspace-write`를 제거하고 `--dangerously-bypass-approvals-and-sandbox`를 추가한다. 모듈 docstring과 함수 docstring은 내부 sandbox 대신 외부 Pod 경계를 사용한다는 책임을 명시한다. Phase 2 spec에는 외부 경계 목록과 내부 sandbox를 사용하지 않는 이유를 기록한다.

- [x] **Step 4: GREEN과 관련 회귀 검증을 확인한다**

Run:

```bash
uv run python -m pytest tests/test_experiment_codex_worker.py -v
uv run python -m pytest tests/test_experiment_executor_integration.py -v
uv run --no-sync ruff check agent_orchestration tests
git diff --check
```

Expected: 모든 명령 exit 0.

- [x] **Step 5: 변경을 커밋한다**

```bash
git add agent_orchestration/executor/codex_worker.py tests/test_experiment_codex_worker.py docs/specs/2026-08-06-experiment-executor-phase2.md docs/superpowers/plans/2026-08-07-codex-external-sandbox.md
git commit -m "fix: Codex 내부 sandbox 충돌을 제거한다"
```
