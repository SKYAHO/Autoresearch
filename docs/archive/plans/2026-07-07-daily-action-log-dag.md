# Daily Action Log DAG Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 매일 GCS에 적재된 YouTube KR daily partition과 virtual user parquet을 읽어 daily action log parquet을 GCS에 생성한다.

**Architecture:** 기존 `autoresearch.action_logs` 생성기를 유지하고, GCS/YouTube daily partition 어댑터만 추가한다. Airflow DAG은 설정 읽기와 함수 호출만 담당하고, 후보 믹스와 파일 쓰기는 `autoresearch/action_logs` 모듈에 둔다.

**Tech Stack:** Python 3.12, Airflow TaskFlow, pyarrow, pydantic, pytest, GCS via `pyarrow.fs.GcsFileSystem`.

---

### Task 1: 후보 믹스 70/20/10

**Files:**
- Modify: `autoresearch/action_logs/schema.py`
- Modify: `autoresearch/action_logs/candidate.py`
- Test: `tests/test_action_logs_pipeline.py`

- [ ] **Step 1: Write failing tests**

Add tests that `EventGenerationRequest` defaults to `personalized_ratio=0.7`, `popular_ratio=0.2`, `exploration_ratio=0.1`, and `build_candidates` includes popular videos that are not already selected by personalized relevance.

- [ ] **Step 2: Run failing tests**

Run: `python -m pytest tests/test_action_logs_pipeline.py -v`

- [ ] **Step 3: Implement ratios**

Add request fields and update `build_candidates` to fill candidates in personalized, popular, exploration order with dedup and deterministic shuffle.

- [ ] **Step 4: Run passing tests**

Run: `python -m pytest tests/test_action_logs_pipeline.py -v`

### Task 2: YouTube daily partition loader

**Files:**
- Modify: `autoresearch/action_logs/video_source.py`
- Test: `tests/test_action_logs_pipeline.py`

- [ ] **Step 1: Write failing tests**

Add a test that a parquet file with `youtube_collection` columns (`video_title`, `video_description`, `video_tags`, `video_view_count`, `channel_title`, `video_published_at`) is loaded into action log `VideoRecord` keys (`title`, `description`, `tags`, `view_count`, `channel_name`, `published_at`).

- [ ] **Step 2: Run failing tests**

Run: `python -m pytest tests/test_action_logs_pipeline.py -v`

- [ ] **Step 3: Implement dual-schema mapping**

Update `_to_video_record` to accept both old sample columns and normalized YouTube collection columns.

- [ ] **Step 4: Run passing tests**

Run: `python -m pytest tests/test_action_logs_pipeline.py -v`

### Task 3: GCS daily action log runner

**Files:**
- Create: `autoresearch/action_logs/daily.py`
- Test: `tests/test_action_logs_daily.py`

- [ ] **Step 1: Write failing tests**

Use local tmp parquet inputs to verify `run_daily_action_log` reads users/videos, generates events, writes `dt=YYYY-MM-DD/part-0.parquet`, and returns summary.

- [ ] **Step 2: Run failing tests**

Run: `python -m pytest tests/test_action_logs_daily.py -v`

- [ ] **Step 3: Implement runner**

Create `run_daily_action_log` with injectable filesystem, local/GCS-compatible paths, default `RuleBasedActionLogGenerator`, and optional OpenRouter generator.

- [ ] **Step 4: Run passing tests**

Run: `python -m pytest tests/test_action_logs_daily.py -v`

### Task 4: Airflow DAG

**Files:**
- Create: `dags/youtube_action_log_daily.py`
- Modify: `airflow_settings.yaml`

- [ ] **Step 1: Add DAG wrapper**

Create a TaskFlow DAG scheduled after YouTube daily collection. It reads `YOUTUBE_LAKE_BUCKET`, optional action log variables, computes KST partition date, and calls `run_daily_action_log`.

- [ ] **Step 2: Add local Airflow variables**

Add empty/default variables to `airflow_settings.yaml`.

- [ ] **Step 3: Verify imports without Airflow tests**

Run: `python -m pytest tests/test_action_logs_daily.py tests/test_action_logs_pipeline.py -v`

### Task 5: Final verification

**Files:**
- All changed files

- [ ] **Step 1: Run focused tests**

Run: `python -m pytest tests/test_action_logs_daily.py tests/test_action_logs_pipeline.py -v`

- [ ] **Step 2: Run full test suite**

Run: `python -m pytest -v`

- [ ] **Step 3: Check diff hygiene**

Run: `git diff --check`
