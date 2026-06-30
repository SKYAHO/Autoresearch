# Claude PR Review Smoke Test

This document is intentionally small. It exists to verify that the Claude Code PR Review workflow runs when a pull request is opened from a normal feature branch.

Test flow:

- Create an issue.
- Create a branch from `main`.
- Add this documentation-only change.
- Open a pull request that closes the issue.
- Confirm that the Claude Code PR Review workflow runs on the pull request.
