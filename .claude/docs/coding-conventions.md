# Agent Workflow Reference

> Last Updated: 2026-06-11

This guide owns the day-to-day execution checklist for agent work in this
repository.

## When To Use This Doc

Use this document when you need:

- execution order for a non-trivial task
- plan and verification expectations
- commit and PR hygiene reminders
- guidance for mixed code, docs, tests, or workflow changes

## Execution Checklist

1. Read `CLAUDE.md` and apply the mandatory rules.
2. Classify the task: code, docs, tests, workflow, or mixed.
3. Identify the affected surface: CLI, MCP, hooks, storage, search, dream
   worker, web console, packaging, or documentation.
4. Decide whether a written plan is required.
5. Make the smallest coherent change.
6. Run focused verification first.
7. Broaden verification when shared contracts or user-facing behavior changed.
8. Summarize the result with file references and any verification gaps.

## Tiered Review Workflow

Use this workflow for non-trivial implementation work. Narrow docs-only,
format-only, or local workflow edits can use the shorter checklist above when a
full multi-review loop would add process without reducing risk.

1. Write the plan.
2. Run plan review in parallel:
   - top tier peer review
   - Codex independent plan review
3. Implement and create local commits.
   - Follow Tidy First: separate structural commits from behavioral commits.
4. Run implementation review in parallel:
   - top tier peer review
   - Codex independent code review
5. Run a main top tier synthesis peer review.
6. Run unit tests, QA, and integration simulations when they exist and match the
   changed surface.
7. If tests, QA, simulations, or reviews produce follow-up changes, apply the
   fixes and repeat the implementation review process before opening the PR.
8. Open a draft PR.
9. Check CI status.
   - If CI fails, fix the issue and repeat the relevant review and verification
     gates.
   - If CI passes, add the PR comment `/claude_review` while the PR is still a
     draft to trigger the Claude Code Action auto review.
10. Wait for Claude Auto Review (Fable 5).
11. Address each inline review thread with its own separate fix commit, then
    reply in that thread with the resulting commit SHA and resolve the thread.
12. Rerun CI after the fix commits. When CI passes and no known actionable
    findings remain, mark the PR ready for review. Do not trigger additional
    agent review by default after the draft review is complete; leave follow-up
    agent reviews to the user.

Tier labels are capability classes, not fixed model names. If a project-level
mapping exists, follow it; otherwise choose the current available model or
service that matches the tier at execution time. When documenting reviewer
selection, use tier labels (`top tier`, `middle tier`, `fast tier`) instead of
vendor-specific model names. Fixed integration names, such as Codex independent
review and Claude Auto Review (Fable 5), may remain explicit.

## Plan First

Create a written plan before implementation when the change affects multiple
runtime surfaces, public contracts, migrations, deployment behavior, or more
than a few files. A short in-thread plan is enough for narrow documentation or
workflow-only edits.

Use the official repository work-document paths:

| Document type | Path | Use for |
| --- | --- | --- |
| Spec | `docs/specs/YYYY-MM-DD-<slug>.md` | Requirements, design decisions, behavior contracts, data models, architecture, and acceptance criteria |
| Plan | `docs/plans/YYYY-MM-DD-<slug>.md` | Implementation sequencing, task breakdowns, verification commands, and rollout notes |

Match the document to the work's size and independence. A large, independent
workstream (its own behavior, contract, or migration; schedulable as a separate
PR) gets its **own spec**. Small changes (localized edits, polish, rendering-only
gaps) and conditional or deferred decisions stay **inline as sections in a
follow-up or backlog plan** rather than each getting a standalone spec; keep each
inline item grounded with scope, key file references, and a verification pointer.
Promote an inline backlog item to its own spec (and a matching plan) when its work
is actually scheduled — not before. Avoid spec sprawl: many tiny specs for small or
not-yet-planned items is a smell.

Before creating a new document, search `docs/specs` and `docs/plans` for an
existing related spec or plan. Update the existing document when it already owns
the topic.

Historical specs and plans are records of what was believed, decided, corrected,
or verified at the time they were written. When normalizing an existing document
to a newer template, preserve that document's content in the same file, including
stale assumptions, wrong claims, correction notes, verification notes, and
provenance-bearing context. Do not move historical content into another document
or retroactively fix an earlier document because a later document corrected it,
unless Logan explicitly asks for that amendment.

Review notes created while working are local scratch artifacts unless the user
explicitly asks to preserve them in the repository. Do not create a formal review
document path as part of the default workflow.

Specs should include:

- problem and context
- goals and non-goals
- proposed behavior or design
- affected contracts, data, APIs, or user-visible behavior
- acceptance criteria
- risks and open questions

Plans should include:

- goal and non-goals
- affected files and ownership boundaries
- implementation steps
- verification commands
- rollback or risk notes when relevant

## Tidy First

Keep structural and behavioral changes separate:

- Structural changes rearrange code without changing behavior.
- Behavioral changes modify runtime behavior, outputs, contracts, or data.

When both are needed, do structural changes first and verify they preserve
behavior before adding the behavior change.

## Commit Rules

Commit subjects must use one of these formats:

```text
<type>(<ticket>) - <message>
<type> - <message>
```

Use the ticket form only when the user provides a work-tracking ticket, such as
a Jira ticket, Linear ticket, or equivalent. Examples include `ML-1234`,
`DP-1234`, and `ENG-567`. If no ticket is provided, remove the entire
`(<ticket>)` segment. Do not invent placeholders such as `no-ticket`, `none`, or
similar.

Allowed commit types:

| Type | Meaning |
| --- | --- |
| `feat` | New user-facing or runtime capability |
| `fix` | Bug fix |
| `hotfix` | Urgent production-impacting fix |
| `refactor` | Structural code change without behavior change |
| `chore` | Maintenance, tooling, dependency, or repo housekeeping |
| `docs` | Documentation-only change |
| `test` | Test-only change |
| `ci` | CI/CD or GitHub Actions change |
| `style` | Formatting, whitespace, import ordering, wording polish, or UI/CSS styling without behavior change |

Write `<message>` as a short lowercase imperative phrase:

```text
feat(ML-1234) - add claude review workflow
docs - document agent commit rules
```

Commit splitting rules:

- Keep structural and behavioral changes in separate commits.
- Record fixes requested by PR review or agent review in a separate commit from
  the original change.
- For inline PR review comments, create one fix commit per review thread, reply
  in that thread with the commit SHA, and resolve the thread.
- Review-fix examples:

```text
fix(ML-1234) - address review feedback
docs - address agent review feedback
```

Commit subjects and bodies must not contain bare GitHub auto-link references.
Avoid patterns like `#1`, `#2`, and `fixes #12`. Use `PR 12`, `issue 12`, or a
full URL instead.

## Verification Order

For docs-only changes:

1. Check links and referenced paths.
2. Search for stale references.
3. Run `git diff --check`.

For code changes:

1. Run the most focused tests first.
2. Run relevant lint/type checks.
3. Run broader tests when shared behavior changed.

Common commands:

```bash
uv run --extra dev pytest -v
uv run --extra dev ruff check .
uv run --extra dev basedpyright
uv build
```

For workflow files, run `actionlint` when available.

## PR Rules

PR titles must use one of these formats:

```text
<ticket> - <type> - <overview>
<type> - <overview>
```

Use the initial work-tracking ticket for the PR title when one is provided. If
multiple tickets are related, keep the title anchored to the initial ticket and
put related tickets or context in the PR body. If no ticket is provided, remove
the ticket segment.

Allowed PR types are the same as commit types:

```text
feat
fix
hotfix
refactor
chore
docs
test
ci
style
```

Write `<overview>` as a short lowercase phrase:

```text
ML-1234 - ci - add claude review workflow
ci - add claude review workflow
```

PR bodies must follow `.github/pull_request_template.md`. Remove sections that
do not apply instead of leaving placeholders or empty bullets.

PR descriptions should state:

- what changed
- why it changed
- how it was verified
- related issues, PRs, or prior context

In `Verification`, separate commands that were actually run from checks that
were not run and explain why.

PR titles, bodies, and comments must not contain bare GitHub auto-link
references. Avoid patterns like `#1`, `#2`, and `fixes #12`. Use `PR 12`,
`issue 12`, or a full URL instead.

Keep summaries factual. Put risks and verification gaps where reviewers can see
them.

## Agent Behavior For Commits And PRs

- If no work-tracking ticket is provided, omit the ticket segment from commit
  subjects and PR titles.
- Do not invent ticket placeholders.
- Before committing or opening a PR, inspect `git status`, review the staged
  diff, and confirm relevant verification results.
- When applying PR review or agent review feedback, keep the follow-up changes
  in a separate commit.
- Agent-authored commit messages, PR text, and review comments must avoid bare
  GitHub auto-link references like `#N`.
