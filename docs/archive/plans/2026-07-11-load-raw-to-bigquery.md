# GCS raw 데이터 BigQuery 적재 스크립트 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

- 이슈: #113
- spec: `docs/specs/2026-07-11-load-raw-to-bigquery.md`

**Goal:** BigQuery Load Job으로 GCS 데이터 레이크 parquet 3종을 BigQuery 네이티브 테이블로 서버사이드 적재하는 스크립트를 추가한다.

**Architecture:** `scripts/load_raw_to_bigquery.py` 단일 스크립트. 적재 대상을 `LoadTarget` 불변 데이터클래스 튜플로 선언하고, URI/job config 구성 로직을 순수 함수로 분리해 mock 기반 단위 테스트를 가능하게 한다. hive partitioned 소스는 `HivePartitioningOptions(mode=AUTO)`로 `dt` 컬럼을 복원한다. 테이블 단위 실패를 격리하고 요약 후 실패 존재 시 exit 1.

**Tech Stack:** google-cloud-bigquery (Load Job API), python-dotenv, argparse, pytest + unittest.mock

---

## File Structure

- Modify: `pyproject.toml` — dev 그룹에 `google-cloud-bigquery`, `python-dotenv` 추가 (현재 feast 격리 그룹에만 존재 → CI dev 환경에서 테스트 import 불가)
- Modify: `uv.lock` — `uv lock` 재생성
- Create: `scripts/load_raw_to_bigquery.py` — 적재 스크립트 (기존 `scripts/generate_and_upload_dummy_data.py` 패턴 준수: docstring 헤더, try/except import, print 로깅, argparse + .env 기본값)
- Create: `tests/test_load_raw_to_bigquery.py` — BigQuery 클라이언트 mock 단위 테스트

`requirements.txt`는 `[project].dependencies` 미러이므로 dev 그룹 변경에는 갱신 불요. `.env.example`의 `GCP_PROJECT_ID`, `BQ_DATASET`, `BQ_LOCATION`, `YOUTUBE_LAKE_BUCKET`은 이미 존재하므로 갱신 불요.

---

### Task 1: 계획 문서 커밋

- [ ] **Step 1: 이 계획 문서를 커밋**

```bash
git add docs/plans/2026-07-11-load-raw-to-bigquery.md
git commit -m "docs - add raw data bigquery load plan"
```

### Task 2: dev 의존성 추가 — google-cloud-bigquery

**Files:**
- Modify: `pyproject.toml:32-39` (dev 그룹)
- Modify: `uv.lock`

- [ ] **Step 1: pyproject.toml dev 그룹에 추가**

```toml
dev = [
    "pytest>=8.0",
    "datasets>=2.19",
    "openai>=1.0",
    "fastapi>=0.115,<0.129",
    "httpx>=0.28,<0.29",
    "google-cloud-bigquery>=3.20",
    { include-group = "lint" },
]
```

- [ ] **Step 2: lock 재생성 및 동기화**

Run: `uv lock && uv sync`
Expected: 에러 없이 완료 (feast 그룹과의 conflict 선언은 dev↔feast 동시 설치만 막으므로 영향 없음)

- [ ] **Step 3: import 확인**

Run: `uv run python -c "from google.cloud import bigquery; print(bigquery.__version__)"`
Expected: 버전 출력

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "feat - add google-cloud-bigquery to dev deps"
```

### Task 3: dev 의존성 추가 — python-dotenv

**Files:**
- Modify: `pyproject.toml` (dev 그룹)
- Modify: `uv.lock`

- [ ] **Step 1: pyproject.toml dev 그룹에 추가**

```toml
dev = [
    "pytest>=8.0",
    "datasets>=2.19",
    "openai>=1.0",
    "fastapi>=0.115,<0.129",
    "httpx>=0.28,<0.29",
    "google-cloud-bigquery>=3.20",
    "python-dotenv>=1.0",
    { include-group = "lint" },
]
```

- [ ] **Step 2: lock 재생성 및 동기화**

Run: `uv lock && uv sync && uv run python -c "import dotenv; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "feat - add python-dotenv to dev deps"
```

### Task 4: 실패하는 단위 테스트 작성 (RED)

**Files:**
- Test: `tests/test_load_raw_to_bigquery.py`

- [ ] **Step 1: 테스트 파일 작성**

```python
"""tests for scripts/load_raw_to_bigquery.py (BigQuery 클라이언트 mock, 실제 GCP 호출 없음)."""

from unittest import mock

import pytest
from google.cloud import bigquery

from scripts.load_raw_to_bigquery import (
    LOAD_TARGETS,
    build_job_config,
    build_source_uri,
    load_target,
    main,
    select_targets,
)

BUCKET = "test-lake-bucket"


def _target(key: str):
    return next(t for t in LOAD_TARGETS if t.key == key)


# ---------------------------------------------------------------------------
# build_source_uri
# ---------------------------------------------------------------------------

def test_build_source_uri_hive_partitioned():
    uri = build_source_uri(BUCKET, _target("action_log"))
    assert uri == f"gs://{BUCKET}/data_lake/action_log/*"


def test_build_source_uri_single_file():
    uri = build_source_uri(BUCKET, _target("virtual_user"))
    assert uri == f"gs://{BUCKET}/asset/virtual_user/vu_1000.parquet"


# ---------------------------------------------------------------------------
# build_job_config
# ---------------------------------------------------------------------------

def test_build_job_config_parquet_truncate():
    config = build_job_config(BUCKET, _target("virtual_user"))
    assert config.source_format == bigquery.SourceFormat.PARQUET
    assert config.write_disposition == bigquery.WriteDisposition.WRITE_TRUNCATE


def test_build_job_config_hive_partitioning():
    config = build_job_config(BUCKET, _target("youtube_trending_kr"))
    assert config.hive_partitioning is not None
    assert config.hive_partitioning.mode == "AUTO"
    assert (
        config.hive_partitioning.source_uri_prefix
        == f"gs://{BUCKET}/data_lake/youtube_trending_kr"
    )


def test_build_job_config_single_file_has_no_hive_partitioning():
    config = build_job_config(BUCKET, _target("virtual_user"))
    assert config.hive_partitioning is None


# ---------------------------------------------------------------------------
# select_targets
# ---------------------------------------------------------------------------

def test_select_targets_default_returns_all():
    assert select_targets(None) == LOAD_TARGETS


def test_select_targets_subset_preserves_request_order():
    targets = select_targets("virtual_user,action_log")
    assert [t.key for t in targets] == ["virtual_user", "action_log"]


def test_select_targets_unknown_key_raises():
    with pytest.raises(ValueError, match="unknown_table"):
        select_targets("action_log,unknown_table")


# ---------------------------------------------------------------------------
# load_target
# ---------------------------------------------------------------------------

def test_load_target_success_returns_row_count():
    client = mock.MagicMock()
    client.get_table.return_value.num_rows = 1234
    target = _target("action_log")

    result = load_target(
        client=client,
        project="proj",
        dataset="ds",
        location="asia-northeast3",
        bucket=BUCKET,
        target=target,
    )

    assert result.ok
    assert result.num_rows == 1234
    args, kwargs = client.load_table_from_uri.call_args
    assert args[0] == f"gs://{BUCKET}/data_lake/action_log/*"
    assert args[1] == "proj.ds.data_lake_action_log"
    assert kwargs["location"] == "asia-northeast3"
    client.load_table_from_uri.return_value.result.assert_called_once()


def test_load_target_failure_is_captured():
    client = mock.MagicMock()
    client.load_table_from_uri.return_value.result.side_effect = RuntimeError("boom")

    result = load_target(
        client=client,
        project="proj",
        dataset="ds",
        location="asia-northeast3",
        bucket=BUCKET,
        target=_target("virtual_user"),
    )

    assert not result.ok
    assert result.num_rows is None
    assert "boom" in result.error


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_env(monkeypatch):
    monkeypatch.setattr("scripts.load_raw_to_bigquery.load_dotenv", lambda: None)
    for var in ("GCP_PROJECT_ID", "BQ_DATASET", "BQ_LOCATION", "YOUTUBE_LAKE_BUCKET"):
        monkeypatch.delenv(var, raising=False)


def test_main_missing_bucket_exits_with_error(isolated_env, capsys):
    with mock.patch("scripts.load_raw_to_bigquery.bigquery.Client") as client_cls:
        exit_code = main(["--project", "proj"])

    assert exit_code == 1
    assert "YOUTUBE_LAKE_BUCKET" in capsys.readouterr().out
    client_cls.assert_not_called()


def test_main_missing_project_exits_with_error(isolated_env, capsys):
    with mock.patch("scripts.load_raw_to_bigquery.bigquery.Client") as client_cls:
        exit_code = main(["--bucket", BUCKET])

    assert exit_code == 1
    assert "GCP_PROJECT_ID" in capsys.readouterr().out
    client_cls.assert_not_called()


def test_main_one_failure_does_not_block_others(isolated_env, capsys):
    client = mock.MagicMock()
    client.get_table.return_value.num_rows = 10

    def fake_load(uri, table_id, location, job_config):
        job = mock.MagicMock()
        if "action_log" in table_id:
            job.result.side_effect = RuntimeError("load failed")
        return job

    client.load_table_from_uri.side_effect = fake_load

    with mock.patch(
        "scripts.load_raw_to_bigquery.bigquery.Client", return_value=client
    ):
        exit_code = main(["--project", "proj", "--bucket", BUCKET])

    assert exit_code == 1
    assert client.load_table_from_uri.call_count == len(LOAD_TARGETS)
    out = capsys.readouterr().out
    assert "data_lake_action_log" in out
    assert "[FAIL]" in out


def test_main_all_success_returns_zero(isolated_env):
    client = mock.MagicMock()
    client.get_table.return_value.num_rows = 10

    with mock.patch(
        "scripts.load_raw_to_bigquery.bigquery.Client", return_value=client
    ):
        exit_code = main(["--project", "proj", "--bucket", BUCKET])

    assert exit_code == 0
    assert client.load_table_from_uri.call_count == len(LOAD_TARGETS)


def test_main_unknown_table_key_exits_with_error(isolated_env, capsys):
    with mock.patch("scripts.load_raw_to_bigquery.bigquery.Client") as client_cls:
        exit_code = main(
            ["--project", "proj", "--bucket", BUCKET, "--tables", "nope"]
        )

    assert exit_code == 1
    assert "nope" in capsys.readouterr().out
    client_cls.assert_not_called()
```

- [ ] **Step 2: 실패 확인 (RED)**

Run: `uv run python -m pytest tests/test_load_raw_to_bigquery.py -v`
Expected: 전체 FAIL — `ModuleNotFoundError: No module named 'scripts.load_raw_to_bigquery'`

(커밋은 Task 5에서 구현 커밋 이후에 수행 — 커밋 단위 규칙상 구현/테스트 분리, 각 커밋은 green 상태 유지)

### Task 5: 스크립트 구현 (GREEN)

**Files:**
- Create: `scripts/load_raw_to_bigquery.py`

- [ ] **Step 1: 스크립트 작성**

```python
"""
GCS 데이터 레이크 raw 데이터 BigQuery 적재 스크립트

BigQuery Load Job으로 GCS parquet을 서버사이드 적재합니다.
데이터가 로컬을 거치지 않으며, 재실행 시 전체 재적재(WRITE_TRUNCATE)로 멱등합니다.

적재 대상 (키: 소스 -> 대상 테이블):
  action_log:          data_lake/action_log/dt=*          -> data_lake_action_log
  youtube_trending_kr: data_lake/youtube_trending_kr/dt=* -> data_lake_youtube_trending_kr
  virtual_user:        asset/virtual_user/vu_1000.parquet -> asset_virtual_user_vu_1000

hive partitioned 소스(dt=*)는 HivePartitioningOptions(mode=AUTO)로 dt 컬럼을 복원합니다.

사전 조건:
  - BigQuery 데이터셋이 생성되어 있어야 함
  - GOOGLE_APPLICATION_CREDENTIALS 환경 변수에 서비스 계정 키 경로 지정

사용법:
  python scripts/load_raw_to_bigquery.py                                  # 3종 전부
  python scripts/load_raw_to_bigquery.py --tables action_log,virtual_user # 일부만

옵션:
  --project PROJECT   GCP 프로젝트 ID (기본: .env의 GCP_PROJECT_ID)
  --dataset DATASET   BigQuery 데이터셋 (기본: .env의 BQ_DATASET 또는 feast_offline_store)
  --location LOCATION BigQuery location (기본: .env의 BQ_LOCATION 또는 asia-northeast3)
  --bucket BUCKET     GCS 버킷 이름, gs:// 제외 (기본: .env의 YOUTUBE_LAKE_BUCKET)
  --tables KEYS       적재 대상 키 쉼표 구분 (기본: 전부)
"""

import argparse
import os
import sys
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
except ImportError:
    print("python-dotenv 가 필요합니다: uv sync")
    sys.exit(1)

try:
    from google.cloud import bigquery
except ImportError:
    print("google-cloud-bigquery 가 필요합니다: uv sync")
    sys.exit(1)


@dataclass(frozen=True)
class LoadTarget:
    """GCS 소스 하나를 BigQuery 테이블 하나로 적재하는 단위."""

    key: str
    source_path: str  # 버킷 내 경로 (hive partitioned면 디렉터리, 아니면 파일)
    table_name: str
    hive_partitioned: bool


LOAD_TARGETS: tuple[LoadTarget, ...] = (
    LoadTarget(
        key="action_log",
        source_path="data_lake/action_log",
        table_name="data_lake_action_log",
        hive_partitioned=True,
    ),
    LoadTarget(
        key="youtube_trending_kr",
        source_path="data_lake/youtube_trending_kr",
        table_name="data_lake_youtube_trending_kr",
        hive_partitioned=True,
    ),
    LoadTarget(
        key="virtual_user",
        source_path="asset/virtual_user/vu_1000.parquet",
        table_name="asset_virtual_user_vu_1000",
        hive_partitioned=False,
    ),
)


@dataclass(frozen=True)
class LoadResult:
    """테이블 하나의 적재 결과. 실패 시 error에 메시지를 담는다."""

    target: LoadTarget
    num_rows: int | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.error is None


def build_source_uri(bucket: str, target: LoadTarget) -> str:
    """적재 대상의 GCS 소스 URI를 만든다.

    hive partitioned 소스는 BigQuery 와일드카드 제약(URI당 1개)에 맞춰
    디렉터리 전체(`.../*`)를 지정한다.
    """
    if target.hive_partitioned:
        return f"gs://{bucket}/{target.source_path}/*"
    return f"gs://{bucket}/{target.source_path}"


def build_job_config(bucket: str, target: LoadTarget) -> bigquery.LoadJobConfig:
    """Load Job 설정을 만든다: parquet, 전체 재적재(멱등), dt 파티션 복원."""
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    if target.hive_partitioned:
        hive_options = bigquery.HivePartitioningOptions()
        hive_options.mode = "AUTO"
        hive_options.source_uri_prefix = f"gs://{bucket}/{target.source_path}"
        job_config.hive_partitioning = hive_options
    return job_config


def select_targets(tables_arg: str | None) -> tuple[LoadTarget, ...]:
    """--tables 인자(쉼표 구분 키)를 LoadTarget 튜플로 해석한다.

    Raises:
        ValueError: 알 수 없는 키가 포함된 경우.
    """
    if not tables_arg:
        return LOAD_TARGETS
    by_key = {t.key: t for t in LOAD_TARGETS}
    keys = [k.strip() for k in tables_arg.split(",") if k.strip()]
    unknown = [k for k in keys if k not in by_key]
    if unknown:
        raise ValueError(
            f"알 수 없는 테이블 키: {', '.join(unknown)}"
            f" (사용 가능: {', '.join(by_key)})"
        )
    return tuple(by_key[k] for k in keys)


def load_target(
    client: bigquery.Client,
    project: str,
    dataset: str,
    location: str,
    bucket: str,
    target: LoadTarget,
) -> LoadResult:
    """테이블 하나를 적재한다. 실패는 예외 대신 LoadResult.error로 반환한다."""
    table_id = f"{project}.{dataset}.{target.table_name}"
    source_uri = build_source_uri(bucket, target)
    print(f"  적재 중: {source_uri} -> {table_id}")
    try:
        job = client.load_table_from_uri(
            source_uri,
            table_id,
            location=location,
            job_config=build_job_config(bucket, target),
        )
        job.result()
        num_rows = client.get_table(table_id).num_rows
    except Exception as exc:  # noqa: BLE001 - 테이블 단위 실패 격리 (spec 동작 계약)
        print(f"    [FAIL] {exc}")
        return LoadResult(target=target, num_rows=None, error=str(exc))
    print(f"    [OK] {num_rows} rows")
    return LoadResult(target=target, num_rows=num_rows, error=None)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="GCS 데이터 레이크 raw parquet을 BigQuery 네이티브 테이블로 적재"
    )
    parser.add_argument("--project", default=os.getenv("GCP_PROJECT_ID"))
    parser.add_argument("--dataset", default=os.getenv("BQ_DATASET", "feast_offline_store"))
    parser.add_argument("--location", default=os.getenv("BQ_LOCATION", "asia-northeast3"))
    parser.add_argument("--bucket", default=os.getenv("YOUTUBE_LAKE_BUCKET"))
    parser.add_argument("--tables", default=None, help="적재 대상 키 쉼표 구분")
    args = parser.parse_args(argv)

    if not args.project:
        print("[ERROR] --project 또는 .env의 GCP_PROJECT_ID 필요")
        return 1
    if not args.bucket:
        print("[ERROR] --bucket 또는 .env의 YOUTUBE_LAKE_BUCKET 필요")
        return 1

    try:
        targets = select_targets(args.tables)
    except ValueError as exc:
        print(f"[ERROR] {exc}")
        return 1

    print("GCS raw 데이터 BigQuery 적재")
    print(f"  Project:  {args.project}")
    print(f"  Dataset:  {args.dataset}")
    print(f"  Location: {args.location}")
    print(f"  Bucket:   {args.bucket}")
    print(f"  Tables:   {', '.join(t.key for t in targets)}")
    print()

    client = bigquery.Client(project=args.project)
    results = [
        load_target(
            client=client,
            project=args.project,
            dataset=args.dataset,
            location=args.location,
            bucket=args.bucket,
            target=target,
        )
        for target in targets
    ]

    print("\n적재 요약")
    for result in results:
        if result.ok:
            print(f"  [OK]   {result.target.table_name}: {result.num_rows} rows")
        else:
            print(f"  [FAIL] {result.target.table_name}: {result.error}")

    failed = [r for r in results if not r.ok]
    if failed:
        print(f"\n[실패] {len(failed)}/{len(results)}개 테이블 적재 실패")
        return 1
    print(f"\n[완료] {len(results)}개 테이블 적재 성공")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 테스트 통과 확인 (GREEN)**

Run: `uv run python -m pytest tests/test_load_raw_to_bigquery.py -v`
Expected: 전체 PASS

- [ ] **Step 3: 스크립트 커밋**

```bash
git add scripts/load_raw_to_bigquery.py
git commit -m "feat - add raw data bigquery load script"
```

- [ ] **Step 4: 테스트 커밋**

```bash
git add tests/test_load_raw_to_bigquery.py
git commit -m "feat - add bigquery load script tests"
```

### Task 6: 전체 검증

- [ ] **Step 1: 전체 테스트**

Run: `uv run python -m pytest -v`
Expected: 전체 PASS (CI와 동일 명령)

- [ ] **Step 2: 린트**

Run: `uv run ruff check scripts/load_raw_to_bigquery.py tests/test_load_raw_to_bigquery.py`
Expected: 에러 없음

- [ ] **Step 3: (환경 가용 시) 실전 검증**

`.env`에 실제 GCP 자격 증명이 있으면:

Run: `uv run python scripts/load_raw_to_bigquery.py`
Expected: 3개 테이블 [OK] + 행 수 출력, exit 0. 재실행 시 동일 행 수(멱등).

자격 증명이 없으면 PR 본문에 실전 검증 미수행을 명시.

### Task 7: 브랜치 마무리

- [ ] **Step 1: push + PR 생성** (superpowers:finishing-a-development-branch 절차)

PR 본문에 spec/plan 링크, 검증 결과 포함. `Closes #113`.
