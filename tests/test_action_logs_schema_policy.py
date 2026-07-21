"""EventLog 정책 메타데이터 additive 확장·하위 호환 테스트."""

from datetime import UTC, datetime

from autoresearch.action_logs.schema import SOURCE_ONLINE_SIMULATED, EventLog


def _base_kwargs() -> dict:
    return {
        "event_id": "evt_00000000",
        "event_timestamp": datetime(2026, 7, 20, tzinfo=UTC),
        "user_id": "u1",
        "event_type": "impression",
        "video_id": "v1",
    }


def test_historical_event_without_policy_fields_still_validates():
    event = EventLog(**_base_kwargs())  # 기존 historical 로그 형태 그대로
    assert event.policy is None
    assert event.ctr_score is None
    assert event.is_exploration is None
    assert event.policy_version is None


def test_policy_fields_round_trip_to_warehouse_row():
    event = EventLog(
        **_base_kwargs(),
        rank=3,
        source=SOURCE_ONLINE_SIMULATED,
        policy="model",
        ctr_score=0.87,
        is_exploration=False,
        policy_version="run-abc123",
    )
    row = event.to_warehouse_row()
    assert row["source"] == "online_simulated"
    assert row["policy"] == "model"
    assert row["ctr_score"] == 0.87
    assert row["is_exploration"] is False
    assert row["policy_version"] == "run-abc123"


def test_baseline_policy_allows_null_score():
    event = EventLog(**_base_kwargs(), policy="baseline")
    assert event.ctr_score is None
