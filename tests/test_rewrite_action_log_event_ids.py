"""scripts/rewrite_action_log_event_ids.py의 재작성 규칙 테스트."""
from datetime import date

import pyarrow as pa
import pytest

from scripts.rewrite_action_log_event_ids import rewrite_event_ids


def _table(event_ids: list[str]) -> pa.Table:
    return pa.table({
        "event_id": pa.array(event_ids, pa.string()),
        "user_id": pa.array(["u"] * len(event_ids), pa.string()),
    })


def test_rewrites_legacy_ids_with_partition_date_namespace() -> None:
    table = rewrite_event_ids(_table(["evt_00000000", "evt_00001234"]), date(2026, 7, 18))
    assert table.column("event_id").to_pylist() == [
        "evt_20260718_00000000", "evt_20260718_00001234",
    ]
    # 다른 컬럼은 보존된다.
    assert table.column("user_id").to_pylist() == ["u", "u"]


def test_already_namespaced_ids_are_unchanged_idempotent() -> None:
    ids = ["evt_20260718_00000000", "evt_m_20260713_00000007"]
    table = rewrite_event_ids(_table(ids), date(2026, 7, 18))
    assert table.column("event_id").to_pylist() == ids


def test_unrecognized_id_format_fails_loudly() -> None:
    with pytest.raises(ValueError, match="event_id"):
        rewrite_event_ids(_table(["weird-id"]), date(2026, 7, 18))
