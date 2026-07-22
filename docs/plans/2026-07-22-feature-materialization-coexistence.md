# Feature Materialization Coexistence Implementation Plan

> [!WARNING]
> **이 문서는 이력 보존용입니다 (#256에서 철회).**
> 여기서 도입한 `autoresearch.jobs.feature_materialize` 는 제거되었고, feature
> materialization 은 `autoresearch.jobs.feature_store_build` 로 단일화되었습니다.
> 같은 대상 테이블 3종을 재구축하는 공개 CLI 가 둘이 되어 스케줄 충돌 위험이
> 있었고, `Autoresearch-airflow` 의 `feast_offline_feature_build` DAG 배선이 이미
> `feature_store_build` 기준이었습니다. 또한 이 문서의 `user_static_feature`
> 경로는 `{raw_dataset}.asset_virtual_user_vu_1000` 을 전제하는데, 그 BigQuery
> 테이블은 존재하지 않습니다(persona 는 GCS parquet 이 source of truth).
> 현재 계약은 `docs/guides/data-warehouse.md` 를 참조하십시오.

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
