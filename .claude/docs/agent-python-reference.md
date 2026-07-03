# Agent Project Reference

> Last Updated: 2026-05-26

This is the quick project map for agents. Use
`.claude/docs/project-architecture.md` when you need deeper system context.

## When To Use This Doc

Use this first when you need:

- entrypoints for a feature or bug
- ownership boundaries between modules
- safe locations for new code or tests
- a quick path through the repository before editing

## Recommended Reading Paths

For CLI behavior:

1. `src/lucid_dream/cli.py`
2. the relevant service under `src/lucid_dream/services/`
3. focused tests under `tests/test_cli.py` or related service tests

For MCP behavior:

1. `src/lucid_dream/mcp/server.py`
2. `src/lucid_dream/mcp/tools.py`
3. service and storage modules used by the tool

For hook/session ingestion:

1. `src/lucid_dream/services/hook_service.py`
2. transcript/domain helpers under `src/lucid_dream/domain/`
3. session repository paths under `src/lucid_dream/storage/`

For storage and migrations:

1. `src/lucid_dream/storage/schema.py`
2. `src/lucid_dream/storage/repositories.py`
3. Alembic files under `src/lucid_dream/storage/alembic/`

For dream worker behavior:

1. `src/lucid_dream/services/dream_service.py`
2. `src/lucid_dream/dreaming/`
3. tests covering dream service and worker flows

For the web console:

1. `src/lucid_dream/web/`
2. services used by the rendered view
3. tests covering web routes and session liveness

## Ownership Boundaries

| Area | Responsibility |
| --- | --- |
| `src/lucid_dream/cli.py` | CLI command surface and argument wiring |
| `src/lucid_dream/config.py` | Runtime settings and environment loading |
| `src/lucid_dream/domain/` | Small domain value objects and helpers |
| `src/lucid_dream/services/` | Application workflows and business operations |
| `src/lucid_dream/storage/` | Database schema, repositories, content/object stores |
| `src/lucid_dream/search/` | Lexical search adapters and indexing |
| `src/lucid_dream/mcp/` | MCP server and tool surface |
| `src/lucid_dream/dreaming/` | Dream curation and LLM-facing logic |
| `src/lucid_dream/web/` | Read-only server-rendered console |
| `tests/` | Unit, integration, packaging, and e2e coverage |

## Extension Rules

- Add runtime behavior behind services rather than directly in CLI or MCP glue.
- Keep storage changes behind repository methods.
- Add Alembic migrations for persistent schema changes.
- Keep CLI/MCP/web surfaces thin and focused on input/output translation.
- Mirror new behavior with focused tests near the closest existing test file.
