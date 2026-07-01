"""
더미 Feature 데이터를 BigQuery에 업로드

생성된 parquet 파일을 BigQuery 테이블로 적재합니다.

사용법:
  GOOGLE_APPLICATION_CREDENTIALS=./keys/service-account.json \
  python scripts/upload_to_bigquery.py

옵션:
  --project GCP_PROJECT_ID   (기본: .env의 GCP_PROJECT_ID)
  --dataset BQ_DATASET       (기본: feast_offline_store)
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

try:
    from google.cloud import bigquery
    from google.cloud import storage
except ImportError:
    print("google-cloud-bigquery, google-cloud-storage 가 필요합니다:")
    print("  pip install google-cloud-bigquery google-cloud-storage")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = REPO_ROOT / "feature_repo" / "data"

UPLOAD_MAP = {
    "user_features.parquet": "user_features",
    "video_features.parquet": "video_features",
    "user_video_interaction.parquet": "user_video_interaction",
}


def upload_parquet_to_bigquery(
    parquet_path: Path,
    table_id: str,
    client: bigquery.Client,
    gcs_bucket: str,
    storage_client: storage.Client,
):
    print(f"  업로드: {parquet_path.name} -> {table_id}")

    gcs_uri = f"gs://{gcs_bucket}/staging/{parquet_path.name}"
    blob = storage_client.bucket(gcs_bucket).blob(f"staging/{parquet_path.name}")
    blob.upload_from_filename(str(parquet_path))

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )

    load_job = client.load_table_from_uri(gcs_uri, table_id, job_config=job_config)
    load_job.result()

    table = client.get_table(table_id)
    print(f"    [OK] {table_id} ({table.num_rows} rows)")

    blob.delete()


def main():
    load_dotenv(REPO_ROOT / ".env")

    parser = argparse.ArgumentParser(description="더미 데이터 BigQuery 업로드")
    parser.add_argument("--project", default=os.getenv("GCP_PROJECT_ID"))
    parser.add_argument("--dataset", default=os.getenv("BQ_DATASET", "feast_offline_store"))
    parser.add_argument("--bucket", default=None, help="GCS 버킷 (없으면 자동 생성)")
    args = parser.parse_args()

    if not args.project:
        print("[ERROR] --project 또는 .env의 GCP_PROJECT_ID 필요")
        sys.exit(1)

    gcs_bucket = args.bucket or f"feast-staging-{args.project}"

    bq_client = bigquery.Client(project=args.project)
    storage_client = storage.Client(project=args.project)

    bucket = storage_client.bucket(gcs_bucket)
    if not bucket.exists():
        bucket = storage_client.create_bucket(gcs_bucket, location="asia-northeast3")
        print(f"GCS 버킷 생성: {gcs_bucket}")

    print(f"BigQuery 업로드 시작 (project={args.project}, dataset={args.dataset})\n")

    for parquet_name, table_suffix in UPLOAD_MAP.items():
        parquet_path = DATA_DIR / parquet_name
        if not parquet_path.exists():
            print(f"  [SKIP] {parquet_path} 없음. generate_dummy_data.py 먼저 실행하세요.")
            continue

        table_id = f"{args.project}.{args.dataset}.{table_suffix}"
        upload_parquet_to_bigquery(
            parquet_path, table_id, bq_client, gcs_bucket, storage_client
        )

    print(f"\n[완료] BigQuery 업로드 완료")


if __name__ == "__main__":
    main()
