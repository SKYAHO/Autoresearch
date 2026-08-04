# Streamlit Workbench Review Remediation Plan

**Goal:** Address the six unresolved PR #511 review findings without expanding the v0 Experiment API contract.

**Architecture:** The Streamlit UI continues to be a server-side Experiment API consumer. State tracks one additional terminal refresh, the app owns retry and selection recovery, and the client converts malformed API payloads into safe display errors.

## Scope

- Poll a terminal Experiment one extra time after first observing its terminal status.
- Remove a deleted Experiment from the local list; reset Event/Log cursors after a cursor-level 404.
- Provide manual Experiment list refresh for external status changes and temporary API failures.
- Move Streamlit into an `orchestration-ui` dependency group so the API image excludes UI dependencies.
- Normalize transport and model parsing failures to `ApiUnavailableError`.

## Validation

- No local tests are added or run in this change because the user did not request test execution.
- GitHub Actions reruns the repository Ruff and pytest workflows after the follow-up commit is pushed.
