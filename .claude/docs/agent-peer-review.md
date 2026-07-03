# Agent Error Handling Reference

> Last Updated: 2026-05-26

Use this document when adding or changing error behavior.

## Error Selection Flow

1. Identify the failed boundary: CLI input, MCP request, hook payload, storage,
   search, dream curation, web rendering, or deployment.
2. Determine fault owner: caller input, missing configuration, unavailable
   dependency, data conflict, or internal bug.
3. Reuse existing exception types and exit-code behavior where present.
4. Keep messages actionable and safe to expose.
5. Add or update tests for user-visible error behavior.

## Common Patterns

- Missing or malformed hook payloads should fail with explicit messages and
  stable exit behavior.
- Optimistic concurrency conflicts should preserve the expected/current hash
  semantics used by memory writes.
- Migration failures should surface the target database URL only when safe.
- External service failures should include the operation and sanitized endpoint,
  not credentials.

## Do Not

- Replace precise existing errors with broad `Exception`.
- Swallow exceptions without logging or returning a meaningful result.
- Leak secrets, full transcripts, or full memory content in error messages.
- Change CLI exit behavior without tests and documentation.
