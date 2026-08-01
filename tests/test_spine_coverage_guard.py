"""spine 커버리지 가드 테스트 (#464).

요청 기간에 데이터 없는 날이 섞여도 조립이 조용히 성공하던 구멍을 막는다.
집계·판정은 순수 함수라 BigQuery 없이 검증한다.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.pipeline.build_training_dataset import (  # noqa: E402
    require_spine_coverage,
    summarize_spine_coverage,
)


def _spine(day_rows: dict[str, tuple[int, int]], *, tz: str | None = "UTC") -> pd.DataFrame:
    """{KST날짜: (행수, 클릭수)}로 spine을 만든다.

    KST 정오(=03:00 UTC)에 이벤트를 몰아 둔다 — 날짜 변환이 KST 기준인지 보려면
    UTC 날짜와 KST 날짜가 같은 시각이어야 경계 테스트와 분리된다.
    """
    frames = []
    for day, (rows, clicks) in day_rows.items():
        ts = pd.Timestamp(f"{day} 03:00:00", tz="UTC")
        frames.append(
            pd.DataFrame(
                {
                    "user_id": [f"u{i}" for i in range(rows)],
                    "video_id": [f"v{i}" for i in range(rows)],
                    "event_timestamp": [ts] * rows,
                    "clicked": [1] * clicks + [0] * (rows - clicks),
                }
            )
        )
    spine = pd.concat(frames, ignore_index=True)
    if tz is None:
        spine["event_timestamp"] = spine["event_timestamp"].dt.tz_localize(None)
    return spine


def test_all_days_present_passes():
    spine = _spine({"2026-07-27": (6000, 100), "2026-07-28": (6000, 100), "2026-07-29": (6000, 100)})

    coverage = summarize_spine_coverage(spine, "2026-07-27", "2026-07-29")

    assert coverage.usable_days == ("2026-07-27", "2026-07-28", "2026-07-29")
    assert coverage.missing_days == ()
    assert coverage.total_rows == 18000
    assert coverage.total_clicks == 300
    require_spine_coverage(coverage, min_days=3)  # 예외 없음


def test_missing_days_are_reported_and_rejected():
    # 07-25/26이 통째로 빠진 실제 상황(2026-07 수집 구멍) 재현.
    spine = _spine({"2026-07-24": (6000, 50), "2026-07-27": (6000, 50)})

    coverage = summarize_spine_coverage(spine, "2026-07-24", "2026-07-27")

    assert coverage.missing_days == ("2026-07-25", "2026-07-26")
    assert len(coverage.usable_days) == 2
    with pytest.raises(ValueError) as error:
        require_spine_coverage(coverage, min_days=3)
    message = str(error.value)
    assert "2026-07-25" in message and "2026-07-26" in message
    assert "최소 3일" in message


def test_collapsed_day_is_not_counted_as_usable():
    # 2026-07-23/24는 유저 10명(240행)만 남았다 — 정상일과 같이 세면 판정이 무의미해진다.
    spine = _spine({"2026-07-23": (240, 6), "2026-07-27": (6000, 50), "2026-07-28": (6000, 50)})

    coverage = summarize_spine_coverage(spine, "2026-07-23", "2026-07-28")

    assert coverage.sparse_days == ("2026-07-23",)
    assert coverage.usable_days == ("2026-07-27", "2026-07-28")
    with pytest.raises(ValueError, match="행이 너무 적음"):
        require_spine_coverage(coverage, min_days=3)


def test_min_days_zero_is_explicit_bypass():
    # 백필·좁은 구간 재현을 막지 않는다.
    spine = _spine({"2026-07-27": (6000, 50)})

    coverage = summarize_spine_coverage(spine, "2026-07-20", "2026-07-27")

    require_spine_coverage(coverage, min_days=0)  # 예외 없음


def test_zero_click_day_warns_but_does_not_fail(capsys):
    # 2026-07-24는 클릭 0이었다 — 다른 날에 양성이 있으면 학습은 가능하나,
    # 분할이 몰리면 지표가 nan이 되므로(#445) 드러나야 한다.
    spine = _spine(
        {"2026-07-24": (6000, 0), "2026-07-27": (6000, 50), "2026-07-28": (6000, 50)}
    )

    coverage = summarize_spine_coverage(spine, "2026-07-24", "2026-07-28")

    assert coverage.zero_click_days == ("2026-07-24",)
    require_spine_coverage(coverage, min_days=3)
    assert "클릭이 0인 날" in capsys.readouterr().out


def test_empty_spine_reports_every_day_missing():
    coverage = summarize_spine_coverage(
        pd.DataFrame(columns=["user_id", "video_id", "event_timestamp", "clicked"]),
        "2026-07-27",
        "2026-07-29",
    )

    assert coverage.missing_days == ("2026-07-27", "2026-07-28", "2026-07-29")
    assert coverage.total_rows == 0
    with pytest.raises(ValueError):
        require_spine_coverage(coverage, min_days=1)


def test_naive_timestamps_are_treated_as_utc():
    # BigQuery는 tz-aware로 주지만 테스트·다른 소비자가 naive를 넘길 수 있다.
    aware = summarize_spine_coverage(
        _spine({"2026-07-27": (6000, 50)}), "2026-07-27", "2026-07-27"
    )
    naive = summarize_spine_coverage(
        _spine({"2026-07-27": (6000, 50)}, tz=None), "2026-07-27", "2026-07-27"
    )

    assert aware.usable_days == naive.usable_days == ("2026-07-27",)


def test_day_boundary_uses_kst_not_utc():
    # 07-27 16:00 UTC = 07-28 01:00 KST. 파티션 계약(#295)이 KST이므로 07-28로 세야 한다.
    spine = pd.DataFrame(
        {
            "user_id": ["u"] * 6000,
            "video_id": ["v"] * 6000,
            "event_timestamp": [pd.Timestamp("2026-07-27 16:00:00", tz="UTC")] * 6000,
            "clicked": [1] * 50 + [0] * 5950,
        }
    )

    coverage = summarize_spine_coverage(spine, "2026-07-27", "2026-07-28")

    assert coverage.usable_days == ("2026-07-28",)
    assert coverage.missing_days == ("2026-07-27",)


def test_undated_rows_are_counted_and_warned(capsys):
    # event_timestamp가 NaT면 어느 날짜에도 안 잡힌다. 조용히 버리면 "총 N행"과 일별
    # 합계가 어긋난 채로 남으므로 따로 세어 드러낸다(#464 리뷰).
    spine = _spine({"2026-07-27": (6000, 50), "2026-07-28": (6000, 50), "2026-07-29": (6000, 50)})
    spine.loc[0, "event_timestamp"] = pd.NaT

    coverage = summarize_spine_coverage(spine, "2026-07-27", "2026-07-29")

    assert coverage.undated_rows == 1
    assert coverage.total_rows == 18000
    assert "날짜 없음 1행" in coverage.describe()
    require_spine_coverage(coverage, min_days=3)
    assert "날짜에 귀속되지 않은 행" in capsys.readouterr().out


def test_lineage_params_expose_actual_coverage_and_guard_state():
    # 요청 구간만 기록하면 v12의 비대칭이 남는다 — 실측 커버리지와 적용 기준을 남긴다.
    spine = _spine({"2026-07-24": (6000, 50), "2026-07-27": (6000, 50)})

    coverage = summarize_spine_coverage(spine, "2026-07-24", "2026-07-27")
    params = coverage.as_lineage_params(min_days=3)

    assert params["spine_requested_days"] == "4"
    assert params["spine_usable_days"] == "2"
    assert params["spine_missing_days"] == "2"
    assert params["spine_missing_day_list"] == "2026-07-25,2026-07-26"
    assert params["spine_coverage_min_days_applied"] == "3"
    assert params["spine_coverage_guard"] == "on"
    # 우회 실행은 정상 실행과 run 파라미터만으로 구별돼야 한다.
    assert coverage.as_lineage_params(min_days=0)["spine_coverage_guard"] == "off"


def test_lineage_missing_day_list_is_truncated_with_marker():
    # 요청 기간이 길면 목록이 길어진다. 잘라 담되 "전부"인 것처럼 읽히면 안 된다.
    spine = _spine({"2026-07-31": (6000, 50)})

    coverage = summarize_spine_coverage(spine, "2026-07-01", "2026-07-31")
    value = coverage.as_lineage_params(min_days=3)["spine_missing_day_list"]

    assert value.count(",") == 10  # 10개 + 잘림 표시
    assert value.endswith(",+20more")
    assert coverage.as_lineage_params(min_days=3)["spine_missing_days"] == "30"


def test_lineage_missing_day_list_is_none_when_complete():
    spine = _spine({"2026-07-27": (6000, 50), "2026-07-28": (6000, 50)})

    coverage = summarize_spine_coverage(spine, "2026-07-27", "2026-07-28")

    assert coverage.as_lineage_params(min_days=2)["spine_missing_day_list"] == "none"


def test_reversed_range_is_rejected():
    with pytest.raises(ValueError, match="요청 기간이 비었습니다"):
        summarize_spine_coverage(
            _spine({"2026-07-27": (6000, 50)}), "2026-07-29", "2026-07-27"
        )
