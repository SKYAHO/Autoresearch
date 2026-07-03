# Agent Workflow Reference

> Last Updated: 2026-07-03

Complete guide to the GitHub workflow: Issue → Branch → Commit → PR → Review → Merge.
This is the operational standard for all feature work.

## When To Use This Doc

- You're starting a new feature or bug fix and need the full workflow.
- You're writing a commit message or PR description.
- You're reviewing a PR and need to verify it follows the workflow.
- You're unsure about branch naming or merge strategy.

## Workflow Overview

```
Issue Created (Backlog)
    ↓
Branch Created (feat/NN-...)
    ↓
Commits (type: description)
    ↓
PR Created (Draft or Ready)
    ↓
Review → Approve → Squash Merge
    ↓
Issue Auto-Closed
```

## Issue Creation

**When to create an issue:**
- New feature or enhancement
- Bug discovered
- Experiment or research
- Documentation or configuration work
- Refactoring that needs tracking
- Any work that benefits from team visibility

**Issue template structure:**

```
[FEAT] Feature title (or [BUG], [EXP])

## 📝 작업 내용
What is this issue about? (Korean OK for team context)

## 🎯 목표 / 현상
For features: goal and scope
For bugs: current behavior, expected behavior, reproduction steps

## 📋 완료 조건
- [ ] Specific deliverable 1
- [ ] Specific deliverable 2
```

**Labeling:**
- Feature → `feature`
- Bug → `bug`
- Experiment → `experiment`

## Branch Naming

**Rule:** `<type>/<issue-number>-<slug>`

**Types:** `feat/`, `fix/`, `exp/`, `docs/`, `refactor/`, `chore/`

**Before creating a branch:**
```bash
git switch main
git pull origin main
git switch -c feat/45-docs-system-phase1
```

## Commit Messages

**Format:** `<type>: <description>`

**Types:** `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`

**Rules:**
1. One logical change per commit.
2. Separate formatting fixes from functional changes.
3. Use imperative mood ("add" not "added").

**Example commit sequence:**
```
feat: add CLAUDE.md routing table
test: add unit tests for config loading
docs: update architecture overview
```

## PR Creation

**Before creating PR:**
- [ ] All tests pass: `uv run --extra dev pytest -v`
- [ ] Lint passes: `uv run --extra dev ruff check .`
- [ ] No secrets or `.env` files committed
- [ ] Commit messages follow the convention

**PR template:**

```markdown
## 📝 작업 내용
Brief description of the change.

## 🛠️ 변경 사항
- Bullet 1
- Bullet 2

## 🔗 관련 이슈
Closes #45

## 👁️ 리뷰어 참고
Key things reviewers should know.
```

**Draft vs. Ready:**
- Draft: while actively working or seeking early feedback
- Ready: when you want formal review

## Review & Approval

**Required for merge:**
- ✅ Minimum 1 approval
- ✅ All conversations resolved
- ✅ CI/Lint checks passing
- ✅ Status: "Ready for review"

**Branch protection:**
- Direct push to `main` disabled
- PR required
- Approval required
- After approval, new commits reset approval status

## Merging

**Process:**
1. Click "Squash and merge"
2. Verify commit message (PR title + body)
3. Confirm

**Result:**
- All commits squashed into one
- GitHub auto-closes linked Issue
- Branch auto-deleted

**Example final commit on main:**

```
[FEAT] Repository 문서 체계 구축 — Phase 1

feat: CLAUDE.md routing table
feat: agent-project-reference.md project layout
feat: agent-workflow-reference.md PR workflow
feat: architecture-overview.md 4 domains
```

## Special Cases

### Conflicts with main

```bash
git fetch origin main
git rebase origin/main
git push --force-with-lease origin feat/45-...
```

Reviewers will need to re-review.

### Review requests changes

1. Make changes in new commits (don't amend)
2. Push the new commits
3. Request re-review

### Split one PR into multiple

Create new issues, cherry-pick commits to new branches, create separate PRs.
Link them in both PR descriptions.
