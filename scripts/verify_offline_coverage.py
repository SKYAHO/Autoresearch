"""offline feature store 실적재 커버리지 측정 (#356, [EPIC] #299 Phase 0).

PIT 조회가 성립하려면 학습 윈도우 전 기간의 스냅샷이 실제로 적재돼 있어야 한다.
offline 4종 + ``training_entity``의 적재 기간·행수·**결손일**을 학습 윈도우
(기본 최근 30일, 어제까지)로 read-only 집계해, 결손이 있으면 목록으로 드러낸다.

일일 스냅샷 테이블(training_entity / user_dynamic / video)은 KST 날짜별 결손일을,
정적 테이블(user_static / user_category_similarity)은 비어있지 않음 + 최신 시각을
본다.

**선행**: ``cleanup_dummy_seed.py``를 먼저 돌려 더미를 지울 것. 더미 row는
``event_timestamp = now()-1h``라 남아 있으면 결손일을 가려 실측을 오판시킨다.
방어로 이 스크립트도 더미(``user_``/``video_`` 접두)를 집계에서 제외하되, 더미가
남아 있으면 경고를 출력한다. (실제 유저는 ``vu_`` 접두라 이 제외가 실데이터를
지우지 않는다 - cleanup_dummy_seed.py 참조.)

사용법:
  uv run --no-dev --group feast python scripts/verify_offline_coverage.py
  uv run --no-dev --group feast python scripts/verify_offline_coverage.py --days 30
"""

import argparse
import os

from dotenv import load_dotenv
from google.cloud import bigquery

# (테이블, 엔티티 컬럼, 더미 접두, 일일 스냅샷 여부)
_TARGETS: tuple[tuple[str, str, str, bool], ...] = (
    ("training_entity", "user_id", "user", True),
    ("user_dynamic_feature", "user_id", "user", True),
    ("video_feature", "video_id", "video", True),
    ("user_static_feature", "user_id", "user", False),
    ("user_category_similarity", "user_id", "user", False),
)


def _dummy_predicate(column: str, prefix: str) -> str:
    # STARTS_WITH로 리터럴 접두 매칭(LIKE의 '_' 와일드카드/백슬래시 이스케이프 회피).
    return f"STARTS_WITH({column}, '{prefix}_')"


def _daily_sql(table_id: str, dummy: str, days: int) -> str:
    return f"""
DECLARE lo DATE DEFAULT DATE_SUB(CURRENT_DATE('Asia/Seoul'), INTERVAL {days} DAY);
DECLARE hi DATE DEFAULT DATE_SUB(CURRENT_DATE('Asia/Seoul'), INTERVAL 1 DAY);
WITH present AS (
  SELECT DATE(event_timestamp, 'Asia/Seoul') AS d, COUNT(*) AS n
  FROM `{table_id}`
  WHERE NOT ({dummy})
    AND DATE(event_timestamp, 'Asia/Seoul') BETWEEN lo AND hi
  GROUP BY d
)
SELECT
  lo, hi,
  (SELECT COUNT(*) FROM UNNEST(GENERATE_DATE_ARRAY(lo, hi))) AS expected_days,
  (SELECT COUNT(*) FROM present) AS present_days,
  (SELECT ARRAY_AGG(d ORDER BY d) FROM (
     SELECT d FROM UNNEST(GENERATE_DATE_ARRAY(lo, hi)) AS d
     WHERE d NOT IN (SELECT d FROM present)
   )) AS missing_days,
  (SELECT COALESCE(SUM(n), 0) FROM present) AS rows_in_window,
  (SELECT COUNT(*) FROM `{table_id}` WHERE {dummy}) AS dummy_rows
"""


def _static_sql(table_id: str, dummy: str) -> str:
    return f"""
SELECT
  COUNT(*) - COUNTIF({dummy}) AS real_rows,
  COUNTIF({dummy}) AS dummy_rows,
  MAX(event_timestamp) AS latest_ts
FROM `{table_id}`
"""


def _one(client: bigquery.Client, sql: str):
    return next(iter(client.query(sql).result()))


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID"))
    parser.add_argument(
        "--dataset", default=os.environ.get("BQ_DATASET", "feast_offline_store")
    )
    parser.add_argument(
        "--days", type=int, default=30, help="학습 윈도우 일수(어제까지, 기본 30)"
    )
    args = parser.parse_args()
    if not args.project:
        raise SystemExit("GCP_PROJECT_ID (또는 --project)가 필요합니다")

    client = bigquery.Client(project=args.project)
    print(f"[커버리지] {args.project}.{args.dataset} / 최근 {args.days}일(어제까지)\n")

    ok = True
    for table, column, prefix, is_daily in _TARGETS:
        table_id = f"{args.project}.{args.dataset}.{table}"
        dummy = _dummy_predicate(column, prefix)
        print(f"■ {table}")
        try:
            if is_daily:
                r = _one(client, _daily_sql(table_id, dummy, args.days))
                missing = list(r.missing_days or [])
                print(f"  기간 {r.lo}~{r.hi} / 스냅샷일 {r.present_days}/{r.expected_days}"
                      f" / 윈도우 내 {r.rows_in_window} row")
                if missing:
                    ok = False
                    shown = ", ".join(str(d) for d in missing[:10])
                    more = f" 외 {len(missing) - 10}일" if len(missing) > 10 else ""
                    print(f"  [결손] {len(missing)}일 누락: {shown}{more}")
                else:
                    print("  [OK] 결손일 없음")
            else:
                r = _one(client, _static_sql(table_id, dummy))
                print(f"  real {r.real_rows} row / 최신 {r.latest_ts}")
                if r.real_rows == 0:
                    ok = False
                    print("  [결손] 실데이터 0 row")
            if r.dummy_rows:
                print(f"  [경고] 더미 {r.dummy_rows} row 잔존 - cleanup_dummy_seed.py 먼저 실행")
        except Exception as exc:  # noqa: BLE001 - 테이블 부재 등도 결손으로 취급
            ok = False
            print(f"  [실패] {type(exc).__name__}: 테이블 없거나 조회 불가")
        print()

    print("=" * 50)
    print("결과: 전 테이블 커버리지 충족" if ok else "결과: 결손/문제 있음 - 위 목록 확인")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
