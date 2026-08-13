"""리랭킹 서빙 부하테스트의 결정론적 BigQuery fixture provisioner.

[파이프라인] 수집 → 웨어하우스 적재 → 피처 → 학습 → 일일 추천 → 노출 조립 →
LLM 판정 → action log → 재학습 흐름 중, 리랭킹 부하측정 전에 네 offline feature
source table의 loadtest 행만 준비한다.

[기능] 기본 dry run에서 정확한 기존 fixture 행 수를 읽고, --apply에서만
parameterized DELETE와 고정 INSERT를 실행한다.

[비책임] Feast materialize, Redis online store 갱신은 Airflow와 feature_repo/가,
HTTP 리랭킹은 applications/reranking_api/이 담당한다.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import os
import sys
from typing import Sequence

from dotenv import load_dotenv
from google.cloud import bigquery

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from applications.reranking_api.loadtest.rerank_fixture import (  # noqa: E402
    FIXTURE_VERSION,
    FixtureTable,
    build_fixture,
    targeted_count_sql,
    targeted_delete_sql,
    targeted_insert_sql,
)


def _fixture_timestamp() -> datetime:
    return datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID"))
    parser.add_argument("--dataset", default=os.environ.get("BQ_DATASET", "feast_offline_store"))
    parser.add_argument("--apply", action="store_true", help="targeted BigQuery DML 실행")
    args = parser.parse_args(argv)
    if not args.project:
        parser.error("--project 또는 GCP_PROJECT_ID가 필요합니다")
    return args


def _existing_count(
    client: bigquery.Client, project: str, dataset: str, table: FixtureTable
) -> int:
    sql, config = targeted_count_sql(project, dataset, table)
    row = next(iter(client.query(sql, job_config=config).result()))
    return int(row.row_count)


def main(argv: Sequence[str] | None = None) -> int:
    """Dry run 또는 명시적 --apply로 loadtest fixture를 준비한다."""
    load_dotenv()
    args = _parse_args(argv)
    timestamp = _fixture_timestamp()
    tables = build_fixture(timestamp)
    client = bigquery.Client(project=args.project)

    mode = "apply" if args.apply else "dry-run"
    print(f"[{mode}] fixture={FIXTURE_VERSION} timestamp={timestamp.isoformat().replace('+00:00', 'Z')}")
    if not args.apply:
        for table in tables:
            count = _existing_count(client, args.project, args.dataset, table)
            print(f"[{mode}] {table.name}: existing_rows={count}")
        print("Redis는 Airflow materialization이 성공할 때까지 업데이트되지 않습니다.")
        return 0

    for table in tables:
        delete_sql, delete_config = targeted_delete_sql(args.project, args.dataset, table)
        deleted = client.query(delete_sql, job_config=delete_config).result().num_dml_affected_rows
        inserted = client.query(targeted_insert_sql(args.project, args.dataset, table)).result().num_dml_affected_rows
        print(f"[apply] {table.name}: deleted={deleted or 0} inserted={inserted or 0}")
    print("Redis는 Airflow materialization이 성공할 때까지 업데이트되지 않습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
