# GCP 프로젝트 선택 fail-fast 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 프로젝트 ID 기본값을 제거하고 설정 누락을 외부 BigQuery 호출 전에 명확히 실패시킨다.

**Architecture:** CLI 두 개는 argparse 단계에서 프로젝트를 검증하고, 학습 데이터셋 모듈은 재사용 가능한 프로젝트 요구 헬퍼를 BigQuery/Feast 진입점과 기존 환경 사전검증에서 사용한다. 테스트는 실제 인자 해석과 환경 검증을 실행하면서 외부 클라이언트를 차단한다.

**Tech Stack:** Python 3.11/3.12, argparse, pytest, python-dotenv

## Global Constraints

- 프로젝트 ID의 런타임 기본값을 두지 않는다.
- `feature_store_build` 미설정은 종료 코드 2와 `CTR_TRAINING_BQ_PROJECT`, `--project`를 포함한 오류여야 한다.
- `build_static_features` 미설정은 argparse 종료 코드 2와 `GCP_PROJECT_ID`, `--project`를 포함한 오류여야 한다.
- 학습 데이터셋 미설정은 BigQuery/Feast 전에 `CTR_TRAINING_BQ_PROJECT`를 포함한 `ValueError`여야 한다.
- 공개 batch JSON summary 및 기존 명시 `--project` 동작은 유지한다.
- 변경된 런타임 모듈의 최상단 책임 docstring을 계약에 맞게 갱신한다.
- 과거 runbook 기록·계획·archive는 수정하지 않는다.
- `None`은 문자열화하지 않고 명시적으로 미설정으로 판정한다.

---

### Task 1: 프로젝트 해석과 fail-fast 테스트

**Files:**
- Modify: `autoresearch/jobs/feature_store_build.py:1-55, 484-526`
- Modify: `scripts/build_static_features.py:1-60, 594-643`
- Modify: `src/pipeline/build_training_dataset.py:1-106, 141-179, 376-420, 497-536`
- Modify: `tests/test_feature_store_build.py:300-390`
- Modify: `tests/test_build_static_features.py:1-130`
- Modify: `tests/test_build_training_dataset.py:230-300`

**Interfaces:**
- Consumes: `--project`, `CTR_TRAINING_BQ_PROJECT`, `GCP_PROJECT_ID`.
- Produces: fail-fast errors before `_client`, `bigquery.Client`, Feast import, or GCP credential checks.

- [ ] **Step 1: Write the failing tests**

Add one controlled-input test per execution path. The feature-store test must use this shape so a future client creation is always visible:

```python
def test_main_requires_project_before_client(monkeypatch, caplog) -> None:
    monkeypatch.delenv("CTR_TRAINING_BQ_PROJECT", raising=False)

    def _must_not_create_client(*args, **kwargs):
        raise AssertionError("project validation must run before _client")

    monkeypatch.setattr(feature_store_build, "_client", _must_not_create_client)
    assert feature_store_build.main(_PARTITION_ARGS) == 2
    assert "CTR_TRAINING_BQ_PROJECT" in caplog.text
    assert "--project" in caplog.text
```

The static-script test must replace `load_dotenv` with a no-op, clear `GCP_PROJECT_ID`, call `bsf.main(["--bucket", "bucket"])`, assert `SystemExit.code == 2`, and check stderr for `GCP_PROJECT_ID` and `--project`. Add a second static-script test that clears `GCP_PROJECT_ID`, supplies `--project explicit-project`, injects a fake BigQuery client, and proves the explicit argument wins. The training tests must set `BIGQUERY_PROJECT` to `None`, set the remaining required assembly variables, call `main(events_start_date="2026-07-01", events_end_date="2026-07-01")`, and assert `ValueError` containing `CTR_TRAINING_BQ_PROJECT`; make the BigQuery spine loader fail if reached. Add direct tests for `load_events_from_bigquery`, `load_training_entity_spine`, and `_assemble_via_feast` with no project and assert each stops before its external client/import path.

- [ ] **Step 2: Run the three new tests and verify RED**

Run `uv run --no-sync python -m pytest -q tests/test_feature_store_build.py tests/test_build_static_features.py tests/test_build_training_dataset.py`. The new assertions must fail because each code path still selects `autoresearch-503903` or does not require `CTR_TRAINING_BQ_PROJECT`.

- [ ] **Step 3: Implement the minimal project checks**

Remove `DEFAULT_PROJECT` from both CLI modules. Use `default=os.getenv(...)`; reject a blank or absent value with `BatchArgumentError` in feature-store build and `parser.error` in the static script. The feature-store check must use `value is None or not str(value).strip()` so `None` cannot become the non-empty string `"None"`. In the training module, replace the global fallback with an optional environment value and add this helper:

```python
def require_bigquery_project() -> str:
    project = str(BIGQUERY_PROJECT or "").strip()
    if not project:
        raise ValueError(
            "CTR_TRAINING_BQ_PROJECT 환경변수가 필요합니다. "
            ".env.example을 참고해 설정하세요."
        )
    return project
```

Call `require_bigquery_project()` before every BigQuery/Feast project use and as the first existing environment check. Keep all explicit `BIGQUERY_PROJECT = "proj"` test seams valid. Update `build_static_features.py` module usage text so its documented invocation includes `--project` or required `GCP_PROJECT_ID`.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run `uv run --no-sync python -m pytest -q tests/test_feature_store_build.py tests/test_build_static_features.py tests/test_build_training_dataset.py`. All selected tests must pass without GCP network access.

- [ ] **Step 5: Commit**

Stage only the six Task 1 source and test files. Commit with `fix: GCP 프로젝트 기본값을 제거`.

### Task 2: 설정·운영 문서 계약 갱신

**Files:**
- Modify: `.env.example:32-53`
- Modify: `scripts/backfill_feature_store.py:1-30`
- Modify: `docs/guides/data-warehouse.md:95-104`
- Modify: `docs/guides/release-pipeline.md:220-240`
- Modify: `docs/specs/2026-07-22-feature-store-build-batch.md:20-38`
- Move: `docs/specs/2026-08-01-gcp-project-resolution-fail-fast.md` to `docs/archive/specs/2026-08-01-gcp-project-resolution-fail-fast.md`
- Move: `docs/plans/2026-08-01-gcp-project-resolution-fail-fast.md` to `docs/archive/plans/2026-08-01-gcp-project-resolution-fail-fast.md`

**Interfaces:**
- Consumes: Task 1 fail-fast contract.
- Produces: no current hard-coded project ID in runtime code or current operational examples.

- [ ] **Step 1: Update user-facing configuration prose**

Set `CTR_TRAINING_BQ_PROJECT=` in `.env.example` under a Korean comment that identifies it as required. Replace backfill examples with `--project "<gcp-project-id>"`. State that the data-warehouse project variable has no default and that users must configure it. Replace each old GAR project component with `${GCP_PROJECT_ID}` and `${GAR_REPOSITORY}` placeholders.

- [ ] **Step 2: Update the living batch contract**

Change the `--project` default cell to `CTR_TRAINING_BQ_PROJECT (필수)` and state that neither it nor `--project` may be omitted.

- [ ] **Step 3: Archive this completed spec and plan**

Move both #416 planning documents to the matching `docs/archive/` directories only after Task 2 has reflected the completed code contract. Do not alter historical runbook execution records.

- [ ] **Step 4: Run documentation and residue checks**

Run `git diff --check`, then run `git grep -n 'ar-infra-501607' -- . ':!docs/plans' ':!docs/specs' ':!docs/archive' ':!docs/runbooks/2026-07-23-action-log-feature-loop.md'` and the runtime-code project-literal grep. Both grep commands must produce no output, and `git diff --check` must exit 0.

- [ ] **Step 5: Commit**

Stage only Task 2 files, including the archived spec and plan. Commit with `docs: 프로젝트 설정 필수 계약을 갱신`.
