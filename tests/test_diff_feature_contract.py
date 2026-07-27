"""diff_feature_contract 하네스의 순수 비교 로직 단위 테스트 (#357).

BigQuery/DuckDB 없이 도는 부분만 검증한다: 컬럼별 일치/불일치 집계, affinity
불일치 top pair, (user, KST 날짜) 하루내 변동 그룹 카운트, video latest-per-video
정렬. 실제 두 경로 diff는 BigQuery가 있는 환경에서 스크립트를 돌려 측정한다.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from scripts.diff_feature_contract import (
    _numeric_stats,
    _string_stats,
    _variance_within_day,
    compare_video,
)


def test_numeric_stats_counts_mismatch_and_diff() -> None:
    merged = pd.DataFrame(
        {
            "recent_click_count_7d__duck": [1, 2, 5],
            "recent_click_count_7d__off": [1, 3, 5],
        }
    )
    s = _numeric_stats(merged, "recent_click_count_7d")
    assert s["n"] == 3
    assert s["n_mismatch"] == 1
    assert s["mismatch_rate"] == round(1 / 3, 4)
    # duck - off = [0, -1, 0]
    assert s["diff_abs_max"] == 1.0


def test_numeric_stats_treats_both_nan_as_match() -> None:
    merged = pd.DataFrame(
        {"x__duck": [float("nan"), 1.0], "x__off": [float("nan"), 2.0]}
    )
    s = _numeric_stats(merged, "x")
    assert s["n_mismatch"] == 1  # NaN==NaN은 일치, 1.0!=2.0만 불일치


def test_string_stats_reports_top_mismatch_pairs() -> None:
    merged = pd.DataFrame(
        {
            "historical_category_affinity__duck": ["Music", "Gaming", "Music", "unknown"],
            "historical_category_affinity__off": ["Music", "Music", "Music", "Gaming"],
        }
    )
    s = _string_stats(merged, "historical_category_affinity")
    assert s["n_mismatch"] == 2
    assert s["top_mismatch_pairs"]["Gaming != Music"] == 1
    assert s["top_mismatch_pairs"]["unknown != Gaming"] == 1


def test_variance_within_day_flags_shifting_group() -> None:
    # user u1/day D에 임프레션 2개인데 값이 다르면(=하루가 UTC 두 날에 걸침) 변동 그룹.
    duck = pd.DataFrame(
        {
            "user_id": ["u1", "u1", "u2"],
            "kst_date": [date(2026, 7, 7)] * 3,
            "recent_click_count_7d": [3, 4, 9],
            "recent_view_count_7d": [1, 1, 2],
            "recent_watch_time_7d": [0, 0, 0],
            "recent_like_count_7d": [0, 0, 0],
            "total_event_count_7d": [5, 5, 9],
            "historical_category_affinity": ["Music", "Music", "Gaming"],
        }
    )
    var = _variance_within_day(duck)
    assert var["recent_click_count_7d"] == 1  # u1이 3!=4로 흔들림
    assert var["recent_view_count_7d"] == 0  # 아무도 안 흔들림


def test_compare_video_aligns_latest_per_video() -> None:
    duck_video = pd.DataFrame(
        {
            "video_id": ["v1", "v1"],
            "video_trending_date": ["2026-07-06", "2026-07-07"],
            "category_id": ["Gaming", "Music"],  # latest(07-07)=Music
            "duration_sec": [300, 300],  # 파싱 실패 시 DuckDB 기본값 300
            "view_count": [10, 10],
            "like_ratio": [0.1, 0.1],
            "comment_ratio": [0.0, 0.0],
        }
    )
    offline_video = pd.DataFrame(
        {
            "video_id": ["v1"],
            "event_timestamp": ["2026-07-07T00:00:00"],
            "category_id": ["Music"],
            "duration_sec": [0],  # offline 기본값 0 → duration diff
            "view_count": [10],
            "like_ratio": [0.1],
            "comment_ratio": [0.0],
        }
    )
    stats = {s["column"]: s for s in compare_video(duck_video, offline_video)}
    # latest-per-video로 07-07 Music이 골라져 category는 일치
    assert stats["category_id"]["n_mismatch"] == 0
    # duration 기본값 300(duck) vs 0(offline) → 불일치 1
    assert stats["duration_sec"]["n_mismatch"] == 1
    assert stats["duration_sec"]["diff_abs_max"] == 300.0
