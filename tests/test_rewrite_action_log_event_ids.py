"""scripts/rewrite_action_log_event_ids.py의 재작성 규칙 테스트."""
from datetime import date, datetime, timezone

import pyarrow as pa
import pytest

from scripts.rewrite_action_log_event_ids import rewrite_event_ids

_UTC = timezone.utc


def _table(event_ids: list[str], event_timestamps: list[datetime] | None = None) -> pa.Table:
    if event_timestamps is None:
        # KST 2026-07-18 정오에 해당하는 UTC aware timestamp (당일 슬라이스 기본값).
        event_timestamps = [datetime(2026, 7, 18, 3, 0, tzinfo=_UTC)] * len(event_ids)
    return pa.table({
        "event_id": pa.array(event_ids, pa.string()),
        "user_id": pa.array(["u"] * len(event_ids), pa.string()),
        "event_timestamp": pa.array(event_timestamps, pa.timestamp("us", tz="UTC")),
    })


def test_rewrites_legacy_ids_with_partition_date_namespace() -> None:
    table = rewrite_event_ids(_table(["evt_00000000", "evt_00001234"]), date(2026, 7, 18))
    assert table.column("event_id").to_pylist() == [
        "evt_20260718_00000000", "evt_20260718_00001234",
    ]
    # 다른 컬럼은 보존된다.
    assert table.column("user_id").to_pylist() == ["u", "u"]


def test_kst_midnight_boundary_is_recognized_as_same_day() -> None:
    # UTC 2026-07-17 15:00 = KST 2026-07-18 00:00 (자정 경계 직후, 당일로 인정).
    boundary_ts = datetime(2026, 7, 17, 15, 0, tzinfo=_UTC)
    table = rewrite_event_ids(_table(["evt_00000000"], [boundary_ts]), date(2026, 7, 18))
    assert table.column("event_id").to_pylist() == ["evt_20260718_00000000"]


def test_already_namespaced_ids_are_unchanged_idempotent() -> None:
    ids = ["evt_20260718_00000000", "evt_m_20260713_00000007"]
    table = rewrite_event_ids(_table(ids), date(2026, 7, 18))
    assert table.column("event_id").to_pylist() == ids


def test_unrecognized_id_format_fails_loudly() -> None:
    with pytest.raises(ValueError, match="event_id"):
        rewrite_event_ids(_table(["weird-id"]), date(2026, 7, 18))


def test_mixed_kst_dates_fail_slice_guard() -> None:
    # 30일 합성 전개 파티션(round_a)처럼 하루 슬라이스가 아닌 경우를 흉내낸다.
    same_day = datetime(2026, 7, 18, 3, 0, tzinfo=_UTC)
    other_day = datetime(2026, 7, 19, 3, 0, tzinfo=_UTC)
    table = _table(["evt_00000000", "evt_00000001"], [same_day, other_day])
    with pytest.raises(ValueError, match="슬라이스"):
        rewrite_event_ids(table, date(2026, 7, 18))


def test_missing_event_timestamp_column_fails_loudly() -> None:
    table = pa.table({
        "event_id": pa.array(["evt_00000000"], pa.string()),
        "user_id": pa.array(["u"], pa.string()),
    })
    with pytest.raises(ValueError, match="슬라이스"):
        rewrite_event_ids(table, date(2026, 7, 18))
