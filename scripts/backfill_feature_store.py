"""feature_store 결손일 백필 러너 (#356 Phase 0).

`verify_offline_coverage.py`가 드러낸 결손일만 골라 `feature_store_build`를 날짜별로
반복 실행한다. 30일 창을 통째로 다시 돌지 않고 **실제 결손일 ∩ 날짜 상한선**만
대상으로 삼는다(DELETE+INSERT는 멱등이라 재실행이 안전하지만 굳이 이미 채운 날짜를
다시 스캔·쓸 이유가 없다).

날짜 상한선(테이블별로 다름):
  - training_entity: D <= 오늘(KST) - 2. 빌드가 D+1 파티션을 읽으므로(cross-midnight
    click 귀속) D+1 데이터가 다 쌓인 뒤라야 label이 온전하다(spec의 D+2 안전).
  - user_dynamic_feature / video_feature: D <= 오늘(KST) - 1. #295 dt <= P-1 소비 계약.

세 테이블은 서로 날짜 의존이 없어 어느 순서로 돌리든 무관하다. 실패 추적이 쉽게
각 테이블 안에서는 오름차순(오래된 날짜 -> 최근)으로 돈다.

기본은 dry-run(각 날짜에 feature_store_build --dry-run = BQ 문법 검증만). `--apply`로
실제 적재. 첫 실 데이터 실행이므로 `--tables training_entity --limit 1 --apply`로
가장 오래된 결손일 1건만 먼저 돌려 결과(row 수, clicked 비율)를 확인한 뒤 나머지를
미는 것을 권장한다.

사용법:
  uv run --no-dev --group feast python scripts/backfill_feature_store.py --project ar-infra-501607
  uv run --no-dev --group feast python scripts/backfill_feature_store.py --project ar-infra-501607 --tables training_entity --limit 1 --apply
  uv run --no-dev --group feast python scripts/backfill_feature_store.py --project ar-infra-501607 --apply
"""

import argparse
import os
import sys

from dotenv import load_dotenv
from google.cloud import bigquery

# 파일로 직접 실행(python scripts/...)해도 autoresearch 패키지를 찾도록 repo root 추가.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autoresearch.jobs import feature_store_build  # noqa: E402

# (테이블, 엔티티 컬럼, 더미 접두, 자릿수, 상한선 오프셋일)
_TARGETS: tuple[tuple[str, str, str, int, int], ...] = (
    ("training_entity", "user_id", "user", 4, 2),
    ("user_dynamic_feature", "user_id", "user", 4, 1),
    ("video_feature", "video_id", "video", 5, 1),
)


def _dummy_predicate(column: str, prefix: str, digits: int) -> str:
    return rf"REGEXP_CONTAINS({column}, r'^{prefix}_[0-9]{{{digits}}}$')"


def _missing_days(
    client: bigquery.Client, table_id: str, dummy: str, window: int, cutoff: int
) -> list[str]:
    """결손일(창 [오늘-window, 오늘-cutoff]에서 스냅샷 없는 KST 날짜)을 오름차순 반환."""
    sql = f"""
DECLARE lo DATE DEFAULT DATE_SUB(CURRENT_DATE('Asia/Seoul'), INTERVAL {window} DAY);
DECLARE hi DATE DEFAULT DATE_SUB(CURRENT_DATE('Asia/Seoul'), INTERVAL {cutoff} DAY);
WITH present AS (
  SELECT DISTINCT DATE(event_timestamp, 'Asia/Seoul') AS d
  FROM `{table_id}`
  WHERE NOT ({dummy})
    AND DATE(event_timestamp, 'Asia/Seoul') BETWEEN lo AND hi
)
SELECT ARRAY(
  SELECT d FROM UNNEST(GENERATE_DATE_ARRAY(lo, hi)) AS d
  WHERE d NOT IN (SELECT d FROM present)
  ORDER BY d
) AS missing
"""
    row = next(iter(client.query(sql).result()))
    return [d.isoformat() for d in (row.missing or [])]


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID"))
    parser.add_argument(
        "--dataset", default=os.environ.get("CTR_TRAINING_BQ_DATASET", "feast_offline_store")
    )
    parser.add_argument(
        "--raw-dataset",
        default=os.environ.get("CTR_TRAINING_BQ_RAW_DATASET", "data_lake_raw"),
    )
    parser.add_argument("--window", type=int, default=30, help="창 일수(기본 30)")
    parser.add_argument("--tables", help="쉼표 구분 테이블 필터(기본 전체)")
    parser.add_argument("--limit", type=int, help="테이블당 최대 날짜 수(1건 검증용)")
    parser.add_argument(
        "--apply", action="store_true", help="실제 적재(기본은 feature_store_build --dry-run)"
    )
    args = parser.parse_args()
    if not args.project:
        raise SystemExit("GCP_PROJECT_ID (또는 --project)가 필요합니다")

    wanted = {t.strip() for t in args.tables.split(",")} if args.tables else None
    targets = [t for t in _TARGETS if wanted is None or t[0] in wanted]
    client = bigquery.Client(project=args.project)
    mode = "APPLY(적재)" if args.apply else "DRY-RUN(문법 검증만)"
    print(f"[{mode}] {args.project}.{args.dataset} / 창 {args.window}일\n")

    pending: list[str] = []  # 테이블 미생성 등 "아직 못 함"(에러 아님)
    failed: list[str] = []  # 빌드가 실제로 실패(rc != 0)
    for table, column, prefix, digits, cutoff in targets:
        table_id = f"{args.project}.{args.dataset}.{table}"
        dummy = _dummy_predicate(column, prefix, digits)
        try:
            missing = _missing_days(client, table_id, dummy, args.window, cutoff)
        except Exception as exc:  # noqa: BLE001 - 테이블 부재는 실패가 아니라 대기로 취급
            print(f"■ {table}: 조회 불가({type(exc).__name__}) - 테이블 미생성? infra 테이블 생성 후 재실행.\n")
            pending.append(table)
            continue

        dates = missing[: args.limit] if args.limit else missing
        print(f"■ {table} (D<=오늘-{cutoff}): 결손 {len(missing)}일" +
              (f", 이번 실행 {len(dates)}일" if args.limit else ""))
        if not dates:
            print("  결손 없음 - skip\n")
            continue

        for d in dates:
            argv = [
                "--tables", table,
                "--partition-date", d,
                "--project", args.project,
                "--dataset", args.dataset,
                "--raw-dataset", args.raw_dataset,
            ]
            if not args.apply:
                argv.append("--dry-run")
            rc = feature_store_build.main(argv)
            status = "OK" if rc == 0 else f"FAIL(rc={rc})"
            print(f"  {d}: {status}")
            if rc != 0:
                failed.append(f"{table}@{d}")
        print()

    print("=" * 50)
    if pending:
        print(f"대기(테이블 미생성, 에러 아님): {', '.join(pending)} - infra 생성 후 재실행")
    if failed:
        print(f"실패: {', '.join(failed)}")
        return 1
    tail = "" if args.apply else " (dry-run - 실제 적재는 --apply)"
    print(f"실행 대상 전부 OK{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
