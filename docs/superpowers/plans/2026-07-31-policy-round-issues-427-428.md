# Policy Simulation Issues 427-428 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `simulate_policy_round` compare an arbitrary set of named policies with reproducible lineage and report LLM judgment variability, while correcting the `--videos` CLI contract.

**Architecture:** Keep the existing `main(reranker=...)` two-policy API as a compatibility adapter. Add a `PolicySpec` sequence for named policies, normalize all exposure/event/report paths to iterate over that sequence, and persist a round-scoped exposure snapshot for replay validation. Optional repeated judgments reuse the immutable exposure union; the first repeat remains the canonical event-log output and later repeats contribute only to uncertainty statistics.

**Tech Stack:** Python 3.11+, pandas, Pydantic, pytest, stdlib `statistics`/`uuid`, existing action-log and Reranker contracts.

## Global Constraints

- Preserve existing `main(reranker=...)`, baseline/model report keys, replay sidecar fallback, and historical `EventLog` optional-field compatibility.
- Policy names must be non-empty ASCII identifiers matching `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}` and must be unique.
- Every simulation round gets a persisted round id and policy-specific event-id prefix; final event ids must be unique within the batch.
- Repeated judgments use the same ordered policy exposures and candidate union; `judgment_repeats >= 1`.
- Existing event log is repeat 0; report statistics distinguish canonical output from all repeated judgments.
- No new dependency or generated data file may be committed.

### Task 1: Add failing tests for policy generalization and CLI help

**Files:**
- Modify: `tests/test_simulate_policy_round.py`
- Modify: `tests/test_action_logs_schema_policy.py`

**Interfaces:**
- The tests will define the expected `PolicySpec` input, arbitrary event policy names, three-policy reports, unique event ids, and the corrected `--videos` help text.

- [ ] **Step 1: Write failing tests**

Add tests for:

```python
def test_round_supports_three_named_policies(...): ...
def test_round_event_ids_are_unique_and_policy_versions_are_per_policy(...): ...
def test_round_replay_rejects_policy_exposure_snapshot_mismatch(...): ...
def test_event_log_accepts_named_policy(...): ...
def test_cli_videos_help_describes_videos_csv(...): ...
```

The three-policy test must execute the real `main()` with one baseline `PolicySpec` and two stub Reranker `PolicySpec`s, then assert all three policy keys exist in JSON, HTML, and parquet output.

- [ ] **Step 2: Run the focused tests and verify the expected failures**

Run:

```bash
uv run --no-sync python -m pytest tests/test_simulate_policy_round.py tests/test_action_logs_schema_policy.py -k "three_named_policies or event_ids_are_unique or exposure_snapshot_mismatch or named_policy or videos_help" -v
```

Expected: collection or assertion failures because `PolicySpec`, arbitrary policy validation, snapshot replay validation, and the corrected help text do not yet exist.

### Task 2: Generalize policy metadata and event identity

**Files:**
- Modify: `autoresearch/action_logs/schema.py:EventLog.policy`
- Modify: `autoresearch/action_logs/pipeline.py:ExposureMetadata`
- Modify: `src/pipeline/simulate_policy_round.py:PolicySpec, DraftReplay, _write_drafts_meta, main`

**Interfaces:**
- `PolicySpec(name: str, reranker: Reranker | None, version: str | None = None)` describes one baseline (`reranker=None`) or model-backed policy.
- `main(..., policies: Sequence[PolicySpec] | None = None, judgment_repeats: int = 1, round_id: str | None = None)` accepts arbitrary policies while retaining the existing `reranker` adapter.
- `DraftReplay` gains optional `round_id` and policy exposure snapshot fields; old sidecars continue through the existing union-key fallback.

- [ ] **Step 1: Implement only the smallest metadata/schema changes**

Change policy metadata types from `Literal["baseline", "model"]` to validated `str` fields, add policy-name validation, and add deterministic round/policy prefix helpers. Do not alter unrelated `exposure_source` validation.

- [ ] **Step 2: Run schema and new validation tests**

Run:

```bash
uv run --no-sync python -m pytest tests/test_action_logs_schema_policy.py tests/test_simulate_policy_round.py -k "named_policy or policy_name or event_ids_are_unique" -v
```

Expected: schema tests pass; policy execution tests still fail until Task 3.

### Task 3: Implement N-policy exposure, snapshot, replay, and reports

**Files:**
- Modify: `src/pipeline/simulate_policy_round.py`
- Modify: `src/pipeline/report_html.py`
- Modify: `tests/test_simulate_policy_round.py`

**Interfaces:**
- Default `policies=None` creates `baseline` and `model` specs using the legacy `reranker` and `policy_version` values.
- Named policies are processed in input order; a policy scoring failure skips the paired user and records the failure by policy.
- Sidecar `policy_exposures` stores ordered `{video_id, rank, ctr_score, is_exploration, policy_version}` values per user/policy; `exposure_keys` remains for compatibility.
- `overlap_jaccard_by_pair` and generic policy metrics are added while legacy `overlap_jaccard_mean` and baseline/model fields remain.

- [ ] **Step 1: Implement policy normalization and exposure selection**

Replace direct `BASELINE`/`MODEL` loops with `PolicySpec` iteration. Build one feature frame per user for model-backed policies, use each policy's own reranker, seed namespace, version, and exposure list, and keep paired-user semantics when any policy fails.

- [ ] **Step 2: Implement snapshot persistence and strict replay validation**

Persist `round_id`, policy versions, and ordered policy exposures. On replay, compare policy names, users, ordered video ids, rank, score, exploration flag, and version before generating events. Use the old union-only validation only when the snapshot is absent.

- [ ] **Step 3: Implement unique event ids and generic report rendering**

Use `evt_{round_id}_{policy_slug}` prefixes, verify uniqueness before writing, and update HTML rendering to iterate over all policies without injecting raw names into CSS class attributes.

- [ ] **Step 4: Run the focused green cycle**

Run:

```bash
uv run --no-sync python -m pytest tests/test_simulate_policy_round.py tests/test_action_logs_schema_policy.py -v
```

Expected: all focused tests pass, including legacy two-policy and old-sidecar replay tests.

### Task 4: Add repeated judgment statistics

**Files:**
- Modify: `src/pipeline/simulate_policy_round.py`
- Modify: `src/pipeline/report_html.py`
- Modify: `tests/test_simulate_policy_round.py`

**Interfaces:**
- `judgment_repeats` must be an integer >= 1.
- Repeat 0 writes `action_log_drafts.parquet`, `event_log.parquet`, and canonical metrics. Every repeat contributes to `judgment_repeat_metrics`.
- Per-policy report statistics include `judgment_repeats`, `ctr_mean`, `ctr_stddev`, `ctr_interval_95`, `intended_impressions`, `judged_impressions`, and a reliability guideline result based on 3 repeats and 100 unique intended impressions.
- Replay uses one stored repeat and reports `judgment_repeats=1` with replay provenance.

- [ ] **Step 1: Write failing repeated-judgment tests**

Add tests using a stateful fake generator that returns different propensities for the same candidate union. Assert exposures are unchanged across calls, repeat metrics contain both results, the canonical event log uses repeat 0, and `judgment_repeats=0` is rejected.

- [ ] **Step 2: Run tests to verify red**

Run:

```bash
uv run --no-sync python -m pytest tests/test_simulate_policy_round.py -k "judgment_repeat or uncertainty or reliability" -v
```

Expected: failures because the new argument and report fields do not exist.

- [ ] **Step 3: Implement repeated generation and empirical intervals**

Reuse the same `union_by_user` for every generation call, collect repeat-level policy metrics, calculate sample standard deviation only when there are at least two repeats, and use a clamped mean ± 1.96·standard-error interval with an explicit warning when the recommended repeat/sample guideline is not met. Do not merge repeats into the canonical draft parquet.

- [ ] **Step 4: Run the repeated-judgment green cycle**

Run:

```bash
uv run --no-sync python -m pytest tests/test_simulate_policy_round.py -k "judgment_repeat or uncertainty or reliability" -v
```

Expected: all new repeated-judgment tests pass.

### Task 5: Fix CLI help, contracts, and full verification

**Files:**
- Modify: `src/pipeline/simulate_policy_round.py:_cli`
- Modify: `docs/specs/2026-07-20-policy-simulation-round.md`
- Modify: `docs/specs/2026-07-23-policy-round-draft-replay.md` if replay wording is affected
- Modify: `docs/guides/action-log.md` if policy attribution wording is affected
- Modify: `README.md` only if a new public CLI argument or required environment variable is introduced

**Interfaces:**
- `--videos` help must say `사전 파싱된 videos.csv 경로` and must not mention `youtube_videos.csv`.
- Add `--judgment-repeats` and `--round-id` only as optional CLI arguments with the same defaults as `main()`.

- [ ] **Step 1: Update CLI and living contract docs**

Document N-policy input, policy-specific lineage, snapshot replay, canonical repeat-0 event log, repeated statistics, and the reliability guideline. Keep the existing two-policy example valid.

- [ ] **Step 2: Run all focused and related tests**

Run:

```bash
uv run --no-sync python -m pytest tests/test_simulate_policy_round.py tests/test_action_logs_schema_policy.py tests/test_action_logs_pipeline.py tests/test_action_logs_daily.py -v
uv run --no-sync ruff check autoresearch src tests tools
git diff --check
```

- [ ] **Step 3: Inspect the final diff and verify no generated data or secrets are included**

Run:

```bash
git status --short
git diff --stat
git diff --name-only
```

- [ ] **Step 4: Commit the implementation**

Use separate logical commits where practical:

```bash
git commit -m "feat: 정책 시뮬레이션 다중 비교 지원"
git commit -m "docs: 정책 판정 반복성과 videos 계약 정정"
```

## Self-Review Checklist

- [ ] Every plan requirement maps to a test and implementation task.
- [ ] No unresolved placeholder or unspecified failure policy remains.
- [ ] `PolicySpec`, `DraftReplay`, `main`, sidecar, report, and CLI names are consistent.
- [ ] Legacy baseline/model and old sidecar replay behavior are covered before claiming completion.
