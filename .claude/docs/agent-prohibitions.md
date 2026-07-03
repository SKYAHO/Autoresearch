# Plan Review Execution Guide

> Last Updated: 2026-05-26

Use this guide for reviewing pre-implementation plans. For code or diff review,
use `agent-peer-review.md`.

## When To Use This Doc

Use plan review for:

- large multi-file changes
- migrations or storage contract changes
- public CLI, MCP, hook, or web behavior changes
- deployment or workflow changes with operational risk
- explicit requests for "plan review" or ambiguity checks

## Review Questions

Evaluate the plan against these questions:

1. Is the goal clear and bounded?
2. Are non-goals explicit enough to prevent scope creep?
3. Are affected modules and ownership boundaries correct?
4. Does the plan separate structural and behavioral work?
5. Are tests and verification commands specific?
6. Are migration, rollback, and compatibility risks addressed?
7. Are assumptions stated and discoverable?
8. Is any step over-engineered for the requested outcome?

## Output Shape

Return:

1. Blocking issues.
2. Non-blocking improvements.
3. Ambiguities or assumptions.
4. Suggested plan edits.

If the plan is sound, say so and list the highest remaining risks.
