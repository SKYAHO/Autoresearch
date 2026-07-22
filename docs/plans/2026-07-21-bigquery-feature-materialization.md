# BigQuery 피처 테이블 Materialization 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** BigQuery raw 테이블에서 `user_static_feature`, `user_dynamic_feature`, `video_feature`를 안전하게 전체 갱신하는 공개 batch CLI를 제공한다.

**Architecture:** 새 `autoresearch.jobs.feature_materialize` 모듈이 BigQuery script job을 테이블별로 실행한다. 각 script는 Terraform이 만든 테이블을 보존하기 위해 transaction 내에서 변환 결과 행 수를 검증한 뒤 `DELETE`와 `INSERT`를 수행한다. static 변환은 raw Parquet list wrapper를 BigQuery `ARRAY<STRING>`으로 평탄화한다.

**Tech Stack:** Python 3.11+, `argparse`, `google-cloud-bigquery`, BigQuery Standard SQL, pytest.

**Spec:** `docs/specs/2026-07-21-bigquery-feature-materialization.md` · **이슈:** #218 · **브랜치:** `feat/218-bigquery-feature-materialization`

## Global Constraints

- 대상은 `user_static_feature`, `user_dynamic_feature`, `video_feature` 세 테이블로 고정한다.
- `training_entity`, `user_category_similarity`, embedding source, Airflow, Terraform은 수정하지 않는다.
- 공개 실행 경로는 `python -m autoresearch.jobs.feature_materialize`이며 batch-contract-v1 JSONL stdout·exit code 계약을 따른다.
- project/dataset 식별자는 SQL identifier로 안전하게 검증하며 secret과 raw user 식별자를 출력하지 않는다.
- `CREATE OR REPLACE TABLE`을 사용하지 않는다. 기존 table metadata와 partitioning을 보존한다.
- 테이블 하나의 refresh는 transaction 내에서 원자적으로 수행하며, 실패하면 이후 테이블을 실행하지 않는다.

---

### Task 1: 변환 SQL 및 transaction 계약 테스트

**Files:**
- Create: `tests/test_feature_materialize_job.py`
- Create: `autoresearch/jobs/feature_materialize.py`

**Interfaces:**
- Produces: `FEATURE_TABLES: tuple[str, ...]`, `build_materialize_script(project_id: str, dataset_id: str, raw_dataset_id: str, table_name: str) -> str`.
- Consumes: BigQuery raw tables `asset_virtual_user_vu_1000`, `data_lake_action_log`, `data_lake_youtube_trending_kr`.

- [ ] **Step 1: SQL 계약의 실패 테스트를 작성한다**

`tests/test_feature_materialize_job.py`를 만들고 다음 테스트를 작성한다.

```python
import pytest

import autoresearch.jobs.feature_materialize as feature_materialize


def test_feature_tables_are_the_three_supported_sources():
    assert feature_materialize.FEATURE_TABLES == (
        "user_static_feature",
        "user_dynamic_feature",
        "video_feature",
    )


def test_static_script_flattens_bigquery_parquet_list_wrappers():
    script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "test_raw_dataset", "user_static_feature"
    )

    assert "UNNEST(primary_categories.list) AS item" in script
    assert "item.element" in script
    assert "ARRAY<STRING>[]" in script
    assert "asset_virtual_user_vu_1000" in script


@pytest.mark.parametrize(
    "table_name,raw_table",
    [
        ("user_dynamic_feature", "data_lake_action_log"),
        ("video_feature", "data_lake_youtube_trending_kr"),
    ],
)
def test_supported_script_references_its_raw_source(table_name, raw_table):
    script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "test_raw_dataset", table_name
    )

    assert raw_table in script
    assert "BEGIN TRANSACTION" in script
    assert "DELETE FROM" in script
    assert "INSERT INTO" in script
    assert "ASSERT" in script
    assert "CREATE OR REPLACE TABLE" not in script


def test_script_rejects_unknown_feature_table():
    with pytest.raises(ValueError, match="unsupported feature table"):
        feature_materialize.build_materialize_script(
            "test-project", "test_dataset", "test_raw_dataset", "user_category_similarity"
        )
```

- [ ] **Step 2: 실패를 확인한다**

Run: `uv run python -m pytest tests/test_feature_materialize_job.py -v`

Expected: `ModuleNotFoundError` 또는 `feature_materialize`의 정의되지 않은 attribute로 실패한다.

- [ ] **Step 3: SQL builder의 최소 구현을 작성한다**

`autoresearch/jobs/feature_materialize.py`에 다음 public surface를 작성한다. SQL 문자열은 `docs/guides/data-warehouse.md`의 dynamic/video SELECT를 DDL 없이 옮기고, static SELECT의 모든 list 컬럼을 `_string_array` 표현으로 치환한다.

```python
FEATURE_TABLES = (
    "user_static_feature",
    "user_dynamic_feature",
    "video_feature",
)


def _string_array(column_name: str) -> str:
    return (
        "IFNULL(ARRAY(SELECT item.element "
        f"FROM UNNEST({column_name}.list) AS item), ARRAY<STRING>[])"
    )


def build_materialize_script(
    project_id: str, dataset_id: str, raw_dataset_id: str, table_name: str
) -> str:
    if table_name not in FEATURE_TABLES:
        raise ValueError(f"unsupported feature table: {table_name}")
    target = f"`{project_id}.{dataset_id}.{table_name}`"
    select_sql = _FEATURE_SELECTS[table_name].format(
        project_id=project_id,
        dataset_id=dataset_id,
        raw_dataset_id=raw_dataset_id,
        primary_categories=_string_array("primary_categories"),
        hobby_keywords=_string_array("hobby_keywords"),
        interest_keywords=_string_array("interest_keywords"),
        lifestyle_keywords=_string_array("lifestyle_keywords"),
        food_keywords=_string_array("food_keywords"),
        travel_keywords=_string_array("travel_keywords"),
        career_keywords=_string_array("career_keywords"),
        family_context_keywords=_string_array("family_context_keywords"),
    )
    return f"""
BEGIN TRANSACTION;
CREATE TEMP TABLE materialized_rows AS
{select_sql};
ASSERT (SELECT COUNT(*) FROM materialized_rows) > 0
  AS 'materialized feature result must not be empty';
DELETE FROM {target} WHERE TRUE;
INSERT INTO {target} SELECT * FROM materialized_rows;
COMMIT TRANSACTION;
"""
```

`_FEATURE_SELECTS["user_static_feature"]`는 다음 SELECT projection을 사용한다. keyword 표현식은 위 `_string_array()`로 생성한 placeholder를 그대로 사용한다.

```sql
SELECT
  user_id,
  TIMESTAMP '1970-01-01 00:00:00 UTC' AS event_timestamp,
  COALESCE(age_bucket, 'unknown') AS age_group,
  COALESCE(occupation, 'unknown') AS occupation,
  {primary_categories} AS preferred_category,
  ARRAY_CONCAT(
    {hobby_keywords}, {interest_keywords}, {lifestyle_keywords},
    {food_keywords}, {travel_keywords}, {career_keywords},
    {family_context_keywords}
  ) AS preferred_topics,
  CASE
    WHEN LOWER(TRIM(watch_time_band)) IN ('morning', 'am', '오전', '아침') THEN 'morning'
    WHEN LOWER(TRIM(watch_time_band)) IN ('evening', 'pm', '저녁', '오후') THEN 'evening'
    WHEN LOWER(TRIM(watch_time_band)) IN ('night', 'late_night', '밤', '심야') THEN 'night'
    ELSE 'unknown'
  END AS watch_time_band
FROM `{project_id}.{raw_dataset_id}.asset_virtual_user_vu_1000`
WHERE user_id IS NOT NULL
```

- [ ] **Step 4: 단위 테스트를 통과시킨다**

Run: `uv run python -m pytest tests/test_feature_materialize_job.py -v`

Expected: Task 1에서 작성한 테스트가 모두 PASS한다.

- [ ] **Step 5: 커밋한다**

```bash
git add autoresearch/jobs/feature_materialize.py tests/test_feature_materialize_job.py
```

---

### Task 2: 공개 batch CLI 실행·검증 계약

**Files:**
- Modify: `autoresearch/jobs/feature_materialize.py`
- Modify: `tests/test_feature_materialize_job.py`
- Modify: `docs/specs/2026-07-13-public-batch-execution-contract.md`

**Interfaces:**
- Consumes: `build_materialize_script(project_id, dataset_id, raw_dataset_id, table_name)` from Task 1.
- Produces: `main(argv: Sequence[str] | None = None) -> int` and module execution path `python -m autoresearch.jobs.feature_materialize`.

- [ ] **Step 1: CLI의 실패 테스트를 추가한다**

`tests/test_feature_materialize_job.py`에 BigQuery client를 mock하는 다음 테스트를 추가한다.

```python
import json
from unittest.mock import MagicMock


def _summary(output: str) -> dict[str, object]:
    return json.loads(output.splitlines()[-1])


def test_main_runs_each_feature_table_in_order(monkeypatch, capsys):
    client = MagicMock()
    query_jobs = [MagicMock(job_id=f"job-{index}") for index in range(3)]
    client.query.side_effect = query_jobs
    monkeypatch.setattr(feature_materialize, "_bigquery_client", lambda project_id: client)

    assert feature_materialize.main(
        ["--project", "test-project", "--dataset", "test_dataset", "--raw-dataset", "test_raw_dataset"]
    ) == 0

    assert client.query.call_count == 3
    summary = _summary(capsys.readouterr().out)
    assert summary["status"] == "succeeded"
    assert summary["tables"] == list(feature_materialize.FEATURE_TABLES)
    assert summary["job_ids"] == ["job-0", "job-1", "job-2"]


def test_main_stops_when_a_table_query_fails(monkeypatch, capsys):
    client = MagicMock()
    client.query.side_effect = [MagicMock(job_id="job-static"), RuntimeError("query failed")]
    monkeypatch.setattr(feature_materialize, "_bigquery_client", lambda project_id: client)

    assert feature_materialize.main(
        ["--project", "test-project", "--dataset", "test_dataset", "--raw-dataset", "test_raw_dataset"]
    ) == 1

    assert client.query.call_count == 2
    assert _summary(capsys.readouterr().out)["error_type"] == "runtime_failure"


def test_main_rejects_invalid_project_identifier(monkeypatch, capsys):
    monkeypatch.setattr(feature_materialize, "_run", lambda args: pytest.fail("must not run"))

    assert feature_materialize.main(["--project", "bad project", "--dataset", "dataset", "--raw-dataset", "raw_dataset"]) == 2
    assert _summary(capsys.readouterr().out)["error_type"] == "invalid_arguments"
```

- [ ] **Step 2: 실패를 확인한다**

Run: `uv run python -m pytest tests/test_feature_materialize_job.py -v`

Expected: `main`과 `_bigquery_client`가 아직 없어 새 테스트가 실패한다.

- [ ] **Step 3: CLI를 구현한다**

기존 `autoresearch.jobs.feast_materialize`의 parser, `_summary`, `main` exit mapping 패턴을 따른다. 다음 동작을 구현한다.

```python
JOB_NAME = "feature_materialize"


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=_version_json())
    parser.add_argument("--project", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--raw-dataset", required=True)
    return parser


def _run(args: argparse.Namespace) -> dict[str, object]:
    client = _bigquery_client(args.project)
    job_ids: list[str] = []
    for table_name in FEATURE_TABLES:
        script = build_materialize_script(
            args.project, args.dataset, args.raw_dataset, table_name
        )
        job = client.query(script)
        job.result()
        job_ids.append(job.job_id)
    return {
        "status": "succeeded",
        "project": args.project,
        "dataset": args.dataset,
        "tables": list(FEATURE_TABLES),
        "job_ids": job_ids,
    }
```

- [ ] **Step 4: public batch contract 문서를 갱신한다**

`docs/specs/2026-07-13-public-batch-execution-contract.md`의 공개 명령 목록에 다음 행을 추가하고, `Feast materialize` 절 바로 앞에 새 절을 추가한다.

```text
python -m autoresearch.jobs.feature_materialize --project <project-id> --dataset <feature-dataset-id> --raw-dataset <raw-dataset-id>
```

새 절에는 다음 계약을 명시한다.

```markdown
## BigQuery feature materialize

- `--project`, `--dataset`, `--raw-dataset`은 BigQuery identifier 문법을 만족하는 필수 인자다.
- `--dataset`은 feature target table을, `--raw-dataset`은 action-log, YouTube-trending, virtual-user source table을 가리킨다.
- 명령은 `user_static_feature`, `user_dynamic_feature`, `video_feature`를 이 순서로 전체 갱신한다.
- 각 테이블은 transaction 내 `DELETE` + `INSERT`로 갱신한다. 한 테이블의 실패는 기존 행을 유지하고 뒤 테이블 실행을 중단하며 exit 1이다.
- raw 결과가 0행이면 transaction을 실패시킨다.
- 성공 `job_summary`에는 project, dataset, 대상 table 이름과 BigQuery job ID만 포함한다.
```

- [ ] **Step 5: 테스트와 CLI help/version을 검증한다**

Run: `uv run python -m pytest tests/test_feature_materialize_job.py -v && uv run python -m autoresearch.jobs.feature_materialize --help && uv run python -m autoresearch.jobs.feature_materialize --version`

Expected: 테스트 PASS, help는 exit 0, version은 `application_revision`과 `batch-contract-v1` JSON을 출력한다.

- [ ] **Step 6: 커밋한다**

```bash
git add autoresearch/jobs/feature_materialize.py tests/test_feature_materialize_job.py docs/specs/2026-07-13-public-batch-execution-contract.md
```

---

### Task 3: 문서 SQL 동기화와 실제 BigQuery dry-run 검증

**Files:**
- Modify: `docs/guides/data-warehouse.md:73-107`
- Modify: `tests/test_feature_materialize_job.py`

**Interfaces:**
- Consumes: `build_materialize_script(project_id, dataset_id, raw_dataset_id, table_name)` from Task 1.
- Produces: 문서화된 static SQL과 실행 SQL의 동일한 nested-list 평탄화 계약.

- [ ] **Step 1: 문서 SQL 회귀 테스트를 작성한다**

`tests/test_feature_materialize_job.py`에 static builder가 실제 wrapper 해제 규칙을 모든 해당 컬럼에 적용하는 테스트를 추가한다.

```python
def test_static_script_flattens_every_virtual_user_list_column():
    script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "test_raw_dataset", "user_static_feature"
    )

    for column_name in (
        "primary_categories",
        "hobby_keywords",
        "interest_keywords",
        "lifestyle_keywords",
        "food_keywords",
        "travel_keywords",
        "career_keywords",
        "family_context_keywords",
    ):
        assert f"UNNEST({column_name}.list) AS item" in script
```

- [ ] **Step 2: 실패를 확인한다**

Run: `uv run python -m pytest tests/test_feature_materialize_job.py::test_static_script_flattens_every_virtual_user_list_column -v`

Expected: 모든 list field를 아직 평탄화하지 않았다면 누락한 field 이름을 포함해 실패한다.

- [ ] **Step 3: `data-warehouse.md` static SQL을 수정한다**

`docs/guides/data-warehouse.md`의 `user_static_feature` SQL에서 DDL을 다음 주석으로 교체한다.

```sql
-- 실행은 python -m autoresearch.jobs.feature_materialize가 담당한다.
-- Terraform 관리 테이블의 metadata를 보존하기 위해 CREATE OR REPLACE TABLE을 사용하지 않는다.
```

`COALESCE(primary_categories, ARRAY<STRING>[])`와 각 keyword `COALESCE`를 다음 형태로 교체한다.

```sql
IFNULL(
  ARRAY(SELECT item.element FROM UNNEST(primary_categories.list) AS item),
  ARRAY<STRING>[]
) AS preferred_category
```

모든 keyword 컬럼에도 같은 `IFNULL(ARRAY(SELECT item.element FROM UNNEST(<column>.list) AS item), ARRAY<STRING>[])` 표현을 적용한다. dynamic/video 절의 `CREATE OR REPLACE TABLE`도 이 CLI가 materialization을 담당한다는 주석과 SELECT-only 표현으로 정리해 문서가 DDL 실행을 유도하지 않게 한다.

- [ ] **Step 4: 테스트와 BigQuery dry-run을 실행한다**

Run: `uv run python -m pytest tests/test_feature_materialize_job.py -v`

Expected: PASS.

ADC와 read-only BigQuery 권한이 있는 환경에서는 다음 one-off 검증을 실행한다.

```bash
uv run python - <<'PY'
from google.cloud import bigquery

from autoresearch.jobs.feature_materialize import FEATURE_TABLES, build_materialize_script

client = bigquery.Client(project="ar-infra-501607")
for table_name in FEATURE_TABLES:
    script = build_materialize_script(
        "ar-infra-501607", "feast_offline_store", "data_lake_raw", table_name
    )
    select_sql = script.split("CREATE TEMP TABLE materialized_rows AS\n", 1)[1].split(";\nASSERT", 1)[0]
    job = client.query(select_sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
    print(f"{table_name}: {job.total_bytes_processed}")
PY
```

Expected: 세 feature SELECT가 모두 컴파일되고 각 table의 예상 scan bytes가 출력된다. 이 단계는 대상 테이블을 변경하지 않는다.

- [ ] **Step 5: 변경을 검사하고 커밋한다**

Run: `git diff --check && uv run python -m pytest tests/test_feature_materialize_job.py -v`

Expected: 출력 오류 없이 테스트 PASS.

```bash
git add docs/guides/data-warehouse.md tests/test_feature_materialize_job.py
```

---

### Task 4: 회귀 검증과 실행 안내

**Files:**
- Modify: `docs/guides/data-warehouse.md`
- Modify: `docs/README.md:64-68,80-88`
- Modify: `docs/specs/2026-07-21-bigquery-feature-materialization.md`

**Interfaces:**
- Consumes: `python -m autoresearch.jobs.feature_materialize --project <project-id> --dataset <feature-dataset-id> --raw-dataset <raw-dataset-id>` from Task 2.
- Produces: 운영자가 실행할 canonical command와 명시적인 out-of-scope handoff.

- [ ] **Step 1: warehouse guide에 canonical 실행 안내를 추가한다**

`docs/guides/data-warehouse.md`의 feature 테이블 SQL 절 앞에 `## Feature
materialization 실행` 절을 추가한다. 아래 canonical command와 사전 조건을
기록한다.

```bash
python -m autoresearch.jobs.feature_materialize \
  --project "$GCP_PROJECT_ID" \
  --dataset "$BQ_DATASET" \
  --raw-dataset "$CTR_TRAINING_BQ_RAW_DATASET"
```

ADC 또는 workload identity의 BigQuery job 실행 및 대상 테이블 DML 권한이
필요하며, Airflow schedule 연결은 `Autoresearch-airflow` 후속 작업임을
명시한다.

- [ ] **Step 2: 문서 인덱스와 설계 문서를 갱신한다**

`docs/README.md`의 Infrastructure 절에
`docs/specs/2026-07-21-bigquery-feature-materialization.md` 링크를 추가한다.
`docs/specs/2026-07-21-bigquery-feature-materialization.md`의 검증 절에는 실제로
실행한 unit test와 dry-run 결과를 기록할 체크리스트를 추가한다. 실제 row를
적재하는 통합 실행은 별도 승인된 환경에서만 수행한다고 유지한다.

- [ ] **Step 3: 전체 회귀 검증을 실행한다**

Run: `uv run python -m pytest -v`

Expected: 전체 테스트 PASS.

Run: `git diff --check`

Expected: 출력 없이 exit 0.

- [ ] **Step 4: 커밋한다**

```bash
git add docs/guides/data-warehouse.md docs/README.md \
  docs/specs/2026-07-21-bigquery-feature-materialization.md
```
