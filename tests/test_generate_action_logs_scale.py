"""scripts/generate_action_logs_scale.py의 event_id offset 로직 테스트.

#295로 event_id에 KST 날짜 네임스페이스(`evt_{YYYYMMDD}_{seq}`)가 추가되면서
`--event-offset`이 seq가 아니라 날짜 세그먼트에 offset을 더해 같은 날짜의
모든 이벤트를 하나의 event_id로 붕괴시키던 버그를 검증한다. `offset_event_id`는
마지막(`_` 기준 최우측) 세그먼트만 seq로 보고 offset을 더해, 레거시
`evt_{seq}`와 신형 `evt_{YYYYMMDD}_{seq}` 양쪽에서 네임스페이스를 보존해야
한다.
"""

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_action_logs_scale.py"
_spec = importlib.util.spec_from_file_location("generate_action_logs_scale", _SCRIPT)
gals = importlib.util.module_from_spec(_spec)
sys.modules["generate_action_logs_scale"] = gals
_spec.loader.exec_module(gals)


def test_offset_event_id_preserves_legacy_prefix():
    assert gals.offset_event_id("evt_00000001", 100) == "evt_00000101"


def test_offset_event_id_preserves_date_namespace():
    assert gals.offset_event_id("evt_20260718_00000001", 100) == "evt_20260718_00000101"
