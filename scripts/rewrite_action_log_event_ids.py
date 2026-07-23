"""action log parquet의 event_id를 날짜 네임스페이스 형식으로 소급 재작성한다.

[파이프라인] 데이터 레이크 마이그레이션 구간(#295 A안) — GCS에서 내려받은
파티션 parquet 하나를 입력으로 받아 event_id만 재작성한 parquet을 출력한다.
업로드·BQ 재적재는 담당하지 않는다(runbook의 gcloud/bq 절차가 담당).

[기능] 레거시 ``{prefix}_{seq:08d}`` event_id를 파티션 날짜 네임스페이스
``{prefix}_{YYYYMMDD}_{seq:08d}``로 바꾼다. 이미 새 형식인 id는 그대로 두므로
재실행이 멱등하다. 인식할 수 없는 형식은 조용히 통과시키지 않고 실패한다.
입력 테이블이 ``--partition-date`` 하루치 단일 KST 슬라이스가 아니면(예: 30일
합성 전개 파티션) `event_timestamp` 기준으로 사전 검증해 fail-loud 한다.
"""
from __future__ import annotations

import argparse
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

_LEGACY = re.compile(r"^(?P<prefix>.+?)_(?P<seq>\d{8})$")
_NAMESPACED = re.compile(r"^.+_\d{8}_\d{8}$")
_KST = timezone(timedelta(hours=9))


def _to_kst_date(timestamp: datetime | None) -> date:
    """timestamp를 KST 기준 date로 변환한다. aware면 astimezone, naive면 UTC로 간주해 +9h."""
    if timestamp is None:
        raise ValueError(
            "event_timestamp 값에 null이 있어 단일 KST 날짜 슬라이스 검증을 할 수 없습니다."
        )
    if timestamp.tzinfo is not None:
        return timestamp.astimezone(_KST).date()
    return (timestamp + timedelta(hours=9)).date()


def _validate_single_kst_date_slice(table: pa.Table, partition_date: date) -> None:
    """테이블 전 행이 partition_date 하루치 KST 슬라이스인지 검증한다."""
    if "event_timestamp" not in table.column_names:
        raise ValueError(
            "event_timestamp 컬럼이 없어 단일 KST 날짜 슬라이스 검증을 할 수 없습니다."
        )
    kst_dates = [_to_kst_date(ts) for ts in table.column("event_timestamp").to_pylist()]
    mismatched = [d for d in kst_dates if d != partition_date]
    if mismatched:
        actual_min, actual_max = min(kst_dates), max(kst_dates)
        raise ValueError(
            "단일 KST 날짜 슬라이스 가드 위반: "
            f"파티션 날짜({partition_date.isoformat()})와 다른 행이 "
            f"{len(mismatched)}건 있습니다. 실제 KST 날짜 범위 "
            f"{actual_min.isoformat()}~{actual_max.isoformat()}"
        )


def rewrite_event_ids(table: pa.Table, partition_date: date) -> pa.Table:
    """레거시 event_id에 파티션 날짜 네임스페이스를 주입한 새 Table을 돌려준다."""
    _validate_single_kst_date_slice(table, partition_date)
    day = partition_date.strftime("%Y%m%d")
    rewritten: list[str] = []
    for event_id in table.column("event_id").to_pylist():
        if event_id is not None and _NAMESPACED.match(event_id):
            rewritten.append(event_id)
            continue
        match = _LEGACY.match(event_id or "")
        if not match:
            raise ValueError(f"인식할 수 없는 event_id 형식: {event_id!r}")
        rewritten.append(f"{match.group('prefix')}_{day}_{match.group('seq')}")
    index = table.column_names.index("event_id")
    return table.set_column(index, "event_id", pa.array(rewritten, pa.string()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--partition-date", required=True, type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    table = pq.read_table(args.input)
    result = rewrite_event_ids(table, args.partition_date)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(result, args.output)
    print(f"[완료] {args.input} -> {args.output} ({result.num_rows} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
