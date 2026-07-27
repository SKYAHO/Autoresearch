"""diff_feature_contract 하네스의 순수 비교 로직 단위 테스트 (#357).

BigQuery/DuckDB 없이 도는 부분만 검증한다: 컬럼별 일치/불일치 집계, affinity
불일치 top pair, (user, KST 날짜) 하루내 변동 그룹 카운트, video latest-per-video
정렬. 실제 두 경로 diff는 BigQuery가 있는 환경에서 스크립트를 돌려 측정한다.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from scripts.diff_feature_contract import (
    _kst_date,
    _numeric_stats,
    _string_stats,
    _variance_within_day,
    compare_user_dynamic,
    compare_video,
)


def test_kst_date_utc_midnight_boundary() -> None:
    # spec의 핵심 주장("KST 하루가 UTC 두 날짜로 쪼개진다")이 서 있는 경계.
    # UTC 15:00 = KST 다음날 00:00. 이 순간부터 KST 날짜가 넘어간다.
    ts = pd.Series(
        pd.to_datetime(["2026-07-06T15:00:00", "2026-07-06T14:59:59", "2026-07-06T05:30:00"])
    )
    got = list(_kst_date(ts))
    assert got == [date(2026, 7, 7), date(2026, 7, 6), date(2026, 7, 6)]


def _duck_row(user, kst, click, view, watch, like, total, aff):
    return {
        "user_id": user, "kst_date": kst,
        "recent_click_count_7d": click, "recent_view_count_7d": view,
        "recent_watch_time_7d": watch, "recent_like_count_7d": like,
        "total_event_count_7d": total, "historical_category_affinity": aff,
    }


def test_compare_user_dynamic_join_and_coverage() -> None:
    D = date(2026, 7, 7)
    # u1: 같은 (user, KST일)에 임프레션 2개 → offline 1행에 다대일 broadcast.
    duck = pd.DataFrame([
        _duck_row("u1", D, 3, 3, 100, 0, 6, "Music"),
        _duck_row("u1", D, 3, 3, 100, 0, 6, "Music"),
        _duck_row("u2", D, 1, 1, 10, 0, 2, "Gaming"),
    ])
    # offline: u1은 total이 다름(6 vs 7), u3는 무활동이라 임프레션 없이 스냅샷만 존재.
    offline = pd.DataFrame([
        _duck_row("u1", D, 3, 3, 100, 0, 7, "Music"),
        _duck_row("u2", D, 1, 1, 10, 0, 2, "Gaming"),
        _duck_row("u3", D, 0, 0, 0, 0, 0, "unknown"),
    ])
    stats, coverage, merged = compare_user_dynamic(duck, offline)

    # 다대일 broadcast: 임프레션 3개 전부 offline과 매칭.
    assert coverage["compared_impressions"] == 3
    # 지점 4: offline-only(u3) 1 user-day, duck-only 0.
    assert coverage["offline_only_user_days"] == 1
    assert coverage["duck_only_user_days"] == 0
    # total_event만 u1 두 행 불일치 → 2/3.
    total_stat = next(s for s in stats if s["column"] == "total_event_count_7d")
    assert total_stat["n_mismatch"] == 2
    click_stat = next(s for s in stats if s["column"] == "recent_click_count_7d")
    assert click_stat["n_mismatch"] == 0


def test_string_stats_survives_one_sided_nan() -> None:
    # compare_video의 category_id는 duck 쪽 COALESCE가 없어 NaN이 올 수 있다.
    # astype("string")이 NaN→pd.NA로 바꾸는데, 그대로 마스크 인덱싱하면 크래시났다(#369 리뷰).
    merged = pd.DataFrame(
        {
            "category_id__duck": ["Music", None, None],
            "category_id__off": ["Music", "Gaming", None],  # 3행: 양쪽 다 NA
        }
    )
    s = _string_stats(merged, "category_id")
    assert s["n"] == 3
    # 1행 일치(Music) · 2행 불일치(NA vs Gaming) · 3행 일치(양쪽 NA는 일치).
    assert s["n_mismatch"] == 1
    assert s["n_match"] == 2


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
