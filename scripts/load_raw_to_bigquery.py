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
