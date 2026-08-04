"""Streamlit Experiment Workbench의 KST 시각 표시 계약을 검증한다."""

from datetime import datetime, timezone

from agent_orchestration.ui.time import format_time


def test_format_time_converts_utc_to_kst() -> None:
    value = datetime(2026, 8, 4, 6, 30, tzinfo=timezone.utc)

    assert format_time(value) == "08-04 15:30 KST"
