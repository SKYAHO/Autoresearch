"""
Feast Feature Store 환경 설정 스크립트

환경 변수(.env)에서 값을 읽어 feature_store.yaml의 연결 정보를 업데이트합니다.
Memorystore for Redis 인스턴스 생성 후 실행하세요.

사용법:
  1. .env 파일 생성 (cp .env.example .env 후 값 입력)
  2. python scripts/setup_feast_config.py

업데이트 대상:
  - online_store.connection_string (Redis 호스트:포트)
  - offline_store.project (BigQuery 프로젝트 ID)
  - offline_store.dataset (BigQuery 데이터셋)
  - offline_config.gcs_staging_location (GCS staging 경로)
"""

import os
import re
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    print("python-dotenv 가 필요합니다: pip install python-dotenv")
    sys.exit(1)

REPO_ROOT = Path(__file__).parent.parent
YAML_PATH = REPO_ROOT / "feature_repo" / "feature_store.yaml"


def update_yaml(yaml_text: str, key: str, new_value: str) -> str:
    """YAML 텍스트에서 key: value 패턴을 new_value로 교체"""
    pattern = rf"({re.escape(key)}:\s*)(\S+)"
    replacement = rf"\g<1>{new_value}"
    new_text, count = re.subn(pattern, replacement, yaml_text)
    if count == 0:
        print(f"  [WARN] '{key}' 를 찾을 수 없습니다.")
    return new_text


def main():
    load_dotenv(REPO_ROOT / ".env")

    redis_host = os.getenv("REDIS_HOST", "localhost")
    redis_port = os.getenv("REDIS_PORT", "6379")
    redis_conn = f"{redis_host}:{redis_port}"

    gcp_project = os.getenv("GCP_PROJECT_ID", "t-academy-bigquery")
    bq_dataset = os.getenv("BQ_DATASET", "feast_offline_store")
    gcs_staging = os.getenv(
        "GCS_STAGING_LOCATION",
        f"gs://feast-staging-{gcp_project}/",
    )

    print("Feast Feature Store 설정 업데이트:")
    print(f"  Redis 연결:        {redis_conn}")
    print(f"  BigQuery Project:  {gcp_project}")
    print(f"  BigQuery Dataset:  {bq_dataset}")
    print(f"  GCS Staging:       {gcs_staging}")
    print()

    if not YAML_PATH.exists():
        print(f"[ERROR] {YAML_PATH} 가 존재하지 않습니다.")
        sys.exit(1)

    yaml_text = YAML_PATH.read_text(encoding="utf-8")

    yaml_text = update_yaml(yaml_text, "connection_string", f'"{redis_conn}"')
    yaml_text = update_yaml(yaml_text, "project", gcp_project)
    yaml_text = update_yaml(yaml_text, "dataset", bq_dataset)
    yaml_text = update_yaml(yaml_text, "gcs_staging_location", gcs_staging)

    YAML_PATH.write_text(yaml_text, encoding="utf-8")
    print(f"[OK] {YAML_PATH} 업데이트 완료")


if __name__ == "__main__":
    main()
