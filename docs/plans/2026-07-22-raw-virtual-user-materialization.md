# Raw Virtual-User Materialization Implementation Plan

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

**Goal:** `feature_materialize`의 세 피처 변환이 모두 raw dataset의 원천 테이블을 읽도록 통일한다.

**Architecture:** `--dataset`은 Terraform이 관리하는 feature target만 가리키고, `--raw-dataset`은 action log, trending, virtual-user 원천을 모두 가리킨다. 기존 `src.pipeline.build_feature_tables`는 수정하지 않으며, virtual-user BigQuery 적재는 코드 배포와 분리된 명시적 운영 승인 작업으로 남긴다.

**Tech Stack:** Python 3.12, `google-cloud-bigquery`, pytest, BigQuery dry-run

## Global Constraints

- 공개 명령은 `python -m autoresearch.jobs.feature_materialize --project <project-id> --dataset <feature-dataset-id> --raw-dataset <raw-dataset-id>`이다.
- 대상은 `user_static_feature`, `user_dynamic_feature`, `video_feature` 세 테이블만이다.
- `src.pipeline.build_feature_tables`, Airflow schedule, Terraform을 수정하지 않는다.
- source raw table은 `data_lake_action_log`, `data_lake_youtube_trending_kr`, `asset_virtual_user_vu_1000`이며 모두 `--raw-dataset`에 존재해야 한다.
- BigQuery DML 또는 GCS-to-BigQuery load job은 별도 명시적 승인 없이는 실행하지 않는다.
- 코드 이외 문서와 커밋 메시지는 한국어로 작성한다.

---

### Task 1: Static Source Dataset Alignment

**Files:**
- Modify: `tests/test_feature_materialize_job.py:352-362`
- Modify: `autoresearch/jobs/feature_materialize.py:72-92`

**Interfaces:**
- Consumes: `build_materialize_script(project_id: str, dataset_id: str, raw_dataset_id: str, table_name: str) -> str`
- Produces: `user_static_feature` SQL that reads `asset_virtual_user_vu_1000` from `raw_dataset_id` and writes only to `dataset_id`.

- [ ] **Step 1: Write the failing test**

```python
def test_static_script_reads_virtual_user_source_from_raw_dataset():
    script = feature_materialize.build_materialize_script(
        "test-project", "feature_dataset", "raw_dataset", "user_static_feature"
    )

    assert "`test-project.raw_dataset.asset_virtual_user_vu_1000`" in script
    assert "feature_dataset.asset_virtual_user_vu_1000" not in script
    assert "DELETE FROM `test-project.feature_dataset.user_static_feature`" in script
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_feature_materialize_job.py::test_static_script_reads_virtual_user_source_from_raw_dataset -v`

Expected: FAIL because the static query still interpolates `dataset_id` for `asset_virtual_user_vu_1000`.

- [ ] **Step 3: Write minimal implementation**

```python
FROM `{project_id}.{raw_dataset_id}.asset_virtual_user_vu_1000`
```

Replace only the static query source reference. Preserve the target identifier and all nested-list flattening SQL.

- [ ] **Step 4: Run focused regression tests**

Run: `uv run python -m pytest tests/test_feature_materialize_job.py -v`

Expected: PASS, including static wrapper flattening, source dataset separation, transaction, and CLI summary tests.

- [ ] **Step 5: Commit**

```bash
git add autoresearch/jobs/feature_materialize.py tests/test_feature_materialize_job.py
git commit -m "fix: static 원천을 raw dataset으로 통일"
```

### Task 2: Documentation And Read-Only Validation

**Files:**
- Modify: `docs/guides/data-warehouse.md:31-95`
- Modify: `docs/specs/2026-07-13-public-batch-execution-contract.md:137-149`
- Modify: `docs/plans/2026-07-21-bigquery-feature-materialization.md:421-434`
- Modify: `docs/plans/2026-07-22-feature-materialization-coexistence.md:29`

**Interfaces:**
- Consumes: Task 1 static source contract and committed spec `docs/specs/2026-07-21-bigquery-feature-materialization.md`.
- Produces: one canonical CLI command and a clear precondition that all three raw source tables exist in `--raw-dataset`.

- [ ] **Step 1: Update operation guidance**

Document these facts without changing `src.pipeline.build_feature_tables`:

```text
--dataset: user_static_feature, user_dynamic_feature, video_feature target tables
--raw-dataset: data_lake_action_log, data_lake_youtube_trending_kr,
               asset_virtual_user_vu_1000 source tables
```

State that virtual-user loading uses the existing command below only after an approved BigQuery write operation:

```bash
uv run python scripts/load_raw_to_bigquery.py \
  --project "$GCP_PROJECT_ID" \
  --dataset "$CTR_TRAINING_BQ_RAW_DATASET" \
  --tables virtual_user
```

- [ ] **Step 2: Update stale plan command examples**

Ensure both existing plan files include the required `--raw-dataset "$CTR_TRAINING_BQ_RAW_DATASET"` argument wherever they show the canonical materialization command.

- [ ] **Step 3: Verify documentation consistency**

Run: `uv run python -m pytest tests/test_feature_materialize_job.py -v && git diff --check`

Expected: PASS with no whitespace errors.

- [ ] **Step 4: Run read-only BigQuery query validation**

After `asset_virtual_user_vu_1000` exists in `ar-infra-501607.data_lake_raw`, extract the `CREATE TEMP TABLE materialized_rows AS` SELECT from each generated script and submit each as a BigQuery dry-run. Do not submit the transaction, `DELETE`, `INSERT`, or load command.

Run: `uv run python -m pytest -v && git diff --check`

Expected: full test suite passes and all three SELECT dry-runs compile.

- [ ] **Step 5: Commit**

```bash
git add docs/guides/data-warehouse.md \
  docs/specs/2026-07-13-public-batch-execution-contract.md \
  docs/plans/2026-07-21-bigquery-feature-materialization.md \
  docs/plans/2026-07-22-feature-materialization-coexistence.md
git commit -m "docs: raw virtual-user 적재 전제 명시"
```
