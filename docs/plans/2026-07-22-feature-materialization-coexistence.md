# Feature Materialization Coexistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve both materialization modules while documenting `autoresearch.jobs.feature_materialize` as the only public operational command.

**Architecture:** Resolve the `main` merge conflicts by retaining the dataset-layer documentation and the feature-materialization command documentation together. Keep `src.pipeline.build_feature_tables` unchanged; it remains a non-public implementation that an orchestrator must not schedule alongside the public CLI.

**Tech Stack:** Git, Markdown, pytest, Ruff.

## Global Constraints

- Do not modify or delete `src/pipeline/build_feature_tables.py`.
- The public operational command is `python -m autoresearch.jobs.feature_materialize --project <project-id> --dataset <feature-dataset-id> --raw-dataset <raw-dataset-id>`.
- A schedule must not invoke both materialization paths for the same feature target tables.
- Do not submit BigQuery DML during this work.

---

### Task 1: Resolve Documentation Conflicts and Verify the Merged Branch

**Files:**
- Modify: `docs/guides/data-warehouse.md:3-98`
- Modify: `docs/specs/2026-07-13-public-batch-execution-contract.md:14-23`
- Modify: `docs/specs/2026-07-21-bigquery-feature-materialization.md:24-42`
- Create: `docs/plans/2026-07-22-feature-materialization-coexistence.md`

**Interfaces:**
- Consumes: public CLI `python -m autoresearch.jobs.feature_materialize --project <project-id> --dataset <feature-dataset-id> --raw-dataset <raw-dataset-id>`.
- Produces: conflict-free documentation that preserves the main-branch dataset-layer guide and defines the public operational path.

- [ ] **Step 1: Resolve the table-of-contents conflict**

Keep both entries:

```markdown
- [Feature materialization 실행](#feature-materialization-실행)
- [Dataset 계층 분리](#dataset-layers)
```

- [ ] **Step 2: Resolve the guide-section conflict**

Place the public CLI section before the dataset-layer section. Keep the CLI command, required BigQuery job/raw-read/target-DML permissions, and `Autoresearch-airflow` ownership statement. Preserve the incoming dataset-layer section unchanged.

- [ ] **Step 3: Resolve the public-contract scope conflict**

Keep both command families in the scope sentence:

```markdown
이 계약은 현재 운영 범위인 YouTube 일일 수집, YouTube backfill, action-log
single/shard/merge, action-log 품질 검사, BigQuery feature materialize와 Feast
materialize, 일일 추천 결과 적재를 다룬다.
```

- [ ] **Step 4: Verify no conflict markers remain**

Run: `git diff --check`

Expected: exit code 0 and no `leftover conflict marker` output.

- [ ] **Step 5: Verify the merged branch**

Run: `uv run python -m pytest -v`

Expected: all tests pass; existing third-party deprecation warnings may remain.

- [ ] **Step 6: Commit the merge resolution**

```bash
git add docs/guides/data-warehouse.md docs/specs/2026-07-13-public-batch-execution-contract.md docs/specs/2026-07-21-bigquery-feature-materialization.md docs/plans/2026-07-22-feature-materialization-coexistence.md
```

Expected: the in-progress merge completes without modifying `src/pipeline/build_feature_tables.py`.
