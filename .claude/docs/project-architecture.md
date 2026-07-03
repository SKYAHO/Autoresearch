# Coding Conventions

> Last Updated: 2026-05-26

This document provides practical conventions for implementation details. The
mandatory rules live in `CLAUDE.md`.

## Naming

- Use descriptive names tied to the domain concept.
- Keep service method names action-oriented.
- Keep repository method names persistence-oriented.
- Avoid abbreviations unless they are already established in the codebase.

## Module Boundaries

- CLI, MCP, and web modules should translate inputs and outputs.
- Services should coordinate workflows.
- Repositories should own database access.
- Content and object stores should own external storage semantics.
- Domain modules should contain small data transformations and value helpers.

## Imports

- Prefer absolute imports from `lucid_dream`.
- Keep imports sorted through Ruff.
- Avoid import-time side effects outside established entrypoints.

## Comments

- Add comments only when they clarify non-obvious constraints or tradeoffs.
- Do not narrate obvious assignments or control flow.
- Prefer docstrings for public helpers and complex service methods.
- Use Google-style docstrings for Python code when documenting non-trivial
  call contracts:
  - `Args:` for meaningful parameters, especially public APIs, service
    boundaries, CLI/MCP/web adapters, and runtime factories.
  - `Returns:` when the return value is not obvious from the function name or
    type annotation, or when it is a composed domain/view model.
  - `Raises:` for domain errors, validation failures, external-boundary errors,
    or state conflicts callers must handle.
- Keep docstrings concise and behavior-level. Do not add coverage-only
  docstrings to trivial private helpers, simple pass-through methods, or obvious
  value carriers.

## Data Handling

- Keep durable schema changes in Alembic migrations.
- Keep Markdown content and metadata responsibilities separate.
- Preserve optimistic concurrency checks around mutable memory content.

## Dependency Changes

- Add dependencies only when they remove real complexity.
- Update `uv.lock` with the matching dependency change.
- Document operational dependencies when users need to install or configure
  something new.
