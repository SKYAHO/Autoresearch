"""TEMP_FEAST_BOOTSTRAP 더미 seed 정리 (#356, [EPIC] #299 Phase 0).

``scripts/generate_and_upload_dummy_data.py``가 ``WRITE_TRUNCATE``로 채운 더미 row를
offline feature 테이블 4종에서 찾아 삭제한다. **커버리지 측정
(``verify_offline_coverage.py``)보다 먼저** 실행해야 한다 - 더미 row의
``event_timestamp``가 ``now(UTC) - 1h``(항상 "방금")라, 남아 있으면 커버리지가
결손일을 "없음"으로 오판해 Phase 0 실측이 무너진다.

더미 식별이 실데이터를 지우지 않는 근거:
  - 더미 ``user_id``는 ``user_0001``, ``video_id``는 ``video_00001`` 고정 패턴.
  - 실제 프로덕션 유저는 ``vu_0001``(``vu_`` 접두 -
    ``autoresearch/virtual_users/pipeline.py``의 ``vu_{index:04d}``), 실제 ``video_id``는
    YouTube ID(11자 영숫자). 둘 다 ``user_``/``video_`` 패턴과 겹치지 않는다.

기본은 dry-run(패턴 매칭 row 카운트만). ``--apply``로 실제 DELETE.

사용법:
  uv run --no-dev --group feast python scripts/cleanup_dummy_seed.py          # dry-run
  uv run --no-dev --group feast python scripts/cleanup_dummy_seed.py --apply  # 삭제
"""

import argparse
import os

from dotenv import load_dotenv
from google.cloud import bigquery

# (테이블, 엔티티 컬럼, 더미 리터럴 접두) - generate_and_upload_dummy_data.py와 정합.
# STARTS_WITH로 리터럴 접두 매칭한다(LIKE의 '_' 와일드카드/백슬래시 이스케이프 회피).
_DUMMY_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("user_static_feature", "user_id", "user"),
    ("user_dynamic_feature", "user_id", "user"),
    ("video_feature", "video_id", "video"),
    ("user_category_similarity", "user_id", "user"),
)


def _dummy_predicate(column: str, prefix: str) -> str:
    return f"STARTS_WITH({column}, '{prefix}_')"


def _count_dummy(client: bigquery.Client, table_id: str, predicate: str) -> int:
    sql = f"SELECT COUNT(*) AS n FROM `{table_id}` WHERE {predicate}"
    return int(next(iter(client.query(sql).result())).n)


def _delete_dummy(client: bigquery.Client, table_id: str, predicate: str) -> None:
    client.query(f"DELETE FROM `{table_id}` WHERE {predicate}").result()


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID"))
    parser.add_argument(
        "--dataset", default=os.environ.get("BQ_DATASET", "feast_offline_store")
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="실제 DELETE를 실행한다(기본은 dry-run 카운트만)",
    )
    args = parser.parse_args()
    if not args.project:
        raise SystemExit("GCP_PROJECT_ID (또는 --project)가 필요합니다")

    client = bigquery.Client(project=args.project)
    mode = "APPLY(삭제)" if args.apply else "DRY-RUN(카운트만)"
    print(f"[{mode}] dataset={args.project}.{args.dataset}")

    total = 0
    for table, column, prefix in _DUMMY_TARGETS:
        table_id = f"{args.project}.{args.dataset}.{table}"
        predicate = _dummy_predicate(column, prefix)
        n = _count_dummy(client, table_id, predicate)
        total += n
        if args.apply and n:
            _delete_dummy(client, table_id, predicate)
            print(f"  {table}: 더미 {n} row 삭제됨")
        else:
            print(f"  {table}: 더미 {n} row" + (" (삭제 예정)" if n else ""))

    if not args.apply and total:
        print(f"\n총 {total} row가 더미로 식별됨. 삭제하려면 --apply 로 재실행.")
    elif not total:
        print("\n더미 row 없음 - 정리 불필요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
