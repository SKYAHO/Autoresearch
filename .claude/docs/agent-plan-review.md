# Peer Review Execution Guide

> Last Updated: 2026-05-26

Use this guide for code or diff review. For plan-document review, use
`agent-plan-review.md`.

## When To Use This Doc

Use peer review when:

- a change spans multiple files or contracts
- migrations, storage, hooks, MCP, or web behavior changed
- a workflow or deployment change affects automation
- you need a final quality pass before committing or opening a PR

## Default Review Perspectives

| Perspective | Focus |
| --- | --- |
| Critic | Does the change fit existing repo patterns and avoid unintended changes to existing behavior? |
| Code quality | Are boundaries clear, duplication low, and abstractions justified? |
| Convention | Do naming, imports, docs, typing, and verification follow repo rules? |
| Security | Are secrets, paths, external inputs, and permissions handled safely? |

Use the security perspective whenever credentials, external input, workflows, or
deployment configuration changed.

## Review Prompt Template

```text
Review the current diff for Lucid Dream from the critic, code quality,
convention, and security perspectives. Focus on correctness bugs,
unintended changes to existing behavior, missing tests, unsafe permissions, and
violations of CLAUDE.md or .claude/docs/*.md. Return findings with file and line
references, ordered by severity. If there are no findings, state the remaining
verification risk.
```

## Output Rules

- Findings first, ordered by severity.
- Include file and line references.
- Do not list compliments.
- Summaries are secondary to concrete findings.
- Call out test gaps and verification gaps explicitly.
