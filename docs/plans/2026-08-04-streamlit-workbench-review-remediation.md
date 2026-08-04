# Streamlit Workbench Review Remediation Plan

**Goal:** Address the three merge-blocking PR #511 review findings without expanding the v0 Experiment API contract.

**Architecture:** The Streamlit UI continues to be a server-side Experiment API consumer. State tracks one additional terminal refresh, the app owns retry and selection recovery, and the client converts malformed API payloads into safe display errors.

## Scope

- Render list and detail errors independently; re-run the app scope when polling removes the selected Experiment.
- On Event/Log `404`, re-read the Experiment detail to distinguish deletion from a cursor failure; only the latter clears activity cache and cursors.
- Derive UI polling and terminal status sets from the Experiment API's `ExperimentStatus` and `TERMINAL_STATUSES`.

## Excluded Follow-up

- UI-specific tests, pagination total display, URL validation, timestamp normalization, and documentation cleanup remain separate review follow-up work.

## Validation

- No local tests are run because the user did not request test execution.
- GitHub Actions will rerun the repository Ruff and pytest workflows after the follow-up commit is pushed.
