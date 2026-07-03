# Agent Python Reference

> Last Updated: 2026-05-26

Use this document when writing or editing Python code.

## Pre-Implementation Checklist

1. Confirm whether the changed path performs I/O and should be async.
2. Add explicit return type annotations to new or changed functions.
3. Add Google-style docstrings for public helpers, runtime/adaptor boundaries,
   and complex service methods when the call contract is not self-evident.
4. Keep settings in `LucidSettings` when adding configuration.
5. Prefer repository/service boundaries over direct cross-layer access.
6. Check whether tests need temporary filesystem, database, or environment
   isolation.

## Typing

- Type hints are required for functions, including return types.
- Prefer concrete domain types where they already exist.
- Use `Path` for filesystem paths instead of raw strings when practical.
- Avoid widening types to `Any` unless an external API forces it.

## Async and I/O

- Use async APIs for server, MCP, and web paths when the surrounding code is
  async.
- Keep blocking filesystem and database work behind existing service/storage
  patterns.
- Avoid mixing async orchestration directly into low-level pure helpers.

## Configuration

- Add environment-backed settings to `LucidSettings`.
- Keep defaults safe for local development.
- Do not read `.env` directly outside the settings layer.
- Document new settings in `README.md` and `.env.example` when user-facing.

## Logging and Errors

- Do not log secrets, tokens, full environment dumps, or raw transcript content
  unless explicitly needed and sanitized.
- Prefer clear error messages that identify the failed operation and safe
  context.
- Preserve existing exception types and user-facing exit codes where present.

## Tests

- Follow `tests/CLAUDE.md` for test placement, BDD-style scenario structure,
  fixture boundaries, and checked-in test data safety.
- Use focused tests for changed behavior.
- Prefer temporary directories and in-memory databases for isolation.
- For migrations, verify both empty-database and upgrade-path behavior when the
  change is schema-affecting.
