# Coding Guidelines for AI Coding Agents

> Version: 1.0.0 | Last Updated: 2026-07-03

This document is the default entrypoint for Claude Code and other AI coding
agents working in this repository. It keeps mandatory rules short and points to
the detailed guides under `.claude/docs/`.

## Language Preference

Use English for agent-facing documentation, PR comments, review summaries, and
implementation notes unless the user explicitly asks for another language.

## Rule Priority

When rules conflict, apply this priority:

1. Explicit user requests.
2. `CLAUDE.md` and `AGENTS.md`.
3. Guides under `.claude/docs/`.
4. `README.md`, source comments, and other repository documentation.

If rules are at the same level, prefer the more specific and more recently
updated rule.

## Documentation Navigation

Before making non-trivial changes, open the most relevant guide first:

| Request type | Open first | Open next |
| --- | --- | --- |
| Project structure or ownership | `.claude/docs/agent-project-reference.md` | `.claude/docs/architecture-overview.md` |
| Python style, typing, async, logging | `.claude/docs/agent-python-reference.md` | `.claude/docs/coding-conventions.md` |
| Workflow, specs, plans, commits, PRs | `.claude/docs/agent-workflow-reference.md` | `.claude/docs/agent-prohibitions.md` |
| Security, secrets, external input | `.claude/docs/agent-security-guidelines.md` | `.claude/docs/agent-prohibitions.md` |
| Error handling | `.claude/docs/agent-error-handling-reference.md` | Relevant source files |
| Code review | `.claude/docs/agent-peer-review.md` | `.claude/docs/agent-workflow-reference.md` |
| Plan review | `.claude/docs/agent-plan-review.md` | `.claude/docs/agent-peer-review.md` |

## Project Context

- Runtime package code lives under `src/`.
- Models and training pipelines live under `src/pipeline/`.
- Feature engineering and Feast definitions live under `src/features/`.
- Tests live under `tests/`.
- Configuration is centralized in `src/config/` and `src/pipeline/config.yaml`.
- The project uses `uv` for dependency management and command execution.
- Main surfaces are CLI commands, training/evaluation scripts, and Airflow DAGs.
- Four team domains: Model Training (waieiches, hyochangsung), Feast Features (waieiches, hyochangsung), Airflow Orchestration (bbungjun), GCP Infrastructure (hyeongyu-data).

## Core Rules

- Prefer existing repository patterns over new abstractions.
- Keep structural changes separate from behavioral changes.
- Preserve type hints on Python functions, including return types.
- Avoid broad refactors unless they are required for the requested change.
- Do not commit secrets, local data roots, generated database files, or `.env`.
- Do not bypass quality checks with `# noqa` or `# type: ignore`; fix the issue
  or call out the scope explicitly.
- Update documentation when behavior, commands, configuration, or operational
  expectations change.

## Local Development

For local testing and development:

- Use `uv run` commands for focused CLI, training, or unit tests.
- For feature validation, use local SQLite or DuckDB as needed.
- For Airflow DAG testing, refer to `src/pipeline/` configuration and docs.

## Spec / Plan First

Write a plan before implementation when a change is non-trivial: broad behavior
changes, migrations, cross-module contracts, public APIs, or large multi-file
edits.

Use the repository work-document structure:

- Requirements, design decisions, behavior contracts, and architecture notes
  belong under `docs/specs/YYYY-MM-DD-<slug>.md`.
- Implementation sequencing, task breakdowns, and verification checklists belong
  under `docs/plans/YYYY-MM-DD-<slug>.md`.
- Prefer updating an existing related spec or plan over creating a duplicate.

Small docs-only or narrowly scoped workflow changes can proceed with a short
in-thread plan.

## Verification

Use the narrowest verification that proves the change, then broaden when the
blast radius is shared behavior or user-facing workflow.

Common commands:

```bash
uv run --extra dev pytest -v
uv run --extra dev ruff check .
uv run --extra dev basedpyright
uv build
```

For GitHub workflow and documentation changes, also run:

```bash
git diff --check
```

Use `actionlint` when it is available locally.

## Review Guidance

When reviewing PRs, lead with concrete findings ordered by severity. Focus on:

- Correctness bugs and unintended changes to existing behavior.
- Security and credential-handling risks.
- Database migration and concurrency risks.
- Missing or weak tests for changed behavior.
- Type-safety and async issues.
- Performance issues with clear preconditions and impact.

Prefer inline comments for specific code issues and keep summary comments short.
