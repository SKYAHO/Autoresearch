"""학습(DuckDB) ↔ offline store(BigQuery SQL) 피처 값 diff 하네스 (#357, [EPIC] #299 Phase 1).

전체 파이프라인에서 이 스크립트가 담당하는 구간: **학습 데이터셋 조립 직전의 계약
검증**이다. 학습 경로(``build_training_dataset``의 DuckDB 재계산)와 offline store
정의(``feature_store_build``의 BigQuery SQL)가 *같은 피처를 다르게 계산하는 지점*을
실측 diff로 드러낸다. 어느 정의를 정본으로 채택할지 판단(offline 채택)은 사람이 하고,
실제 조회 경로 교체(#358)나 DuckDB 제거(#359)는 이 스크립트가 담당하지 않는다.

담당하지 않는 인접 책임:
- 적재 커버리지·결손일 측정은 ``scripts/verify_offline_coverage.py``가 담당한다.
- offline feature 테이블 적재 자체는 ``autoresearch.jobs.feature_store_build``.
- 학습 데이터셋 생성은 ``src/pipeline/build_training_dataset.py``.

제공 기능:
- offline 정의(``_USER_DYNAMIC_SELECT`` / ``_VIDEO_SELECT``)를 partition_date별로
  BigQuery에서 **재실행**해 값을 얻는다(적재본 read가 아님 → #356 ttl stale 회피).
- 같은 raw로 DuckDB 학습 경로(``compute_point_in_time_user_features`` /
  ``compute_video_features``)를 실행한다.
- 두 결과를 ``(user_id, KST 날짜)`` / ``video_id``로 조인해 컬럼별 일치/불일치를 센다.
- 같은 ``(user_id, KST 날짜)`` 안에서 임프레션 간 DuckDB 값이 흔들리는지까지 세어
  "당일 제외 가정"을 실측 검증한다(흔들리면 그게 timezone 버킷 diff의 직접 증거).

정렬 원칙: 두 경로는 query point가 다르다(DuckDB=임프레션 시각별, offline=일 스냅샷
1개). KST 날짜 D의 모든 임프레션은 Feast PIT 조회 시 D-00:00(KST) 스냅샷을 받으므로,
``(user_id, KST 날짜)``로 조인하면 apples-to-apples가 된다.

**실행 환경**: BigQuery 접근이 필요하다. 개발 컨테이너(GCP 도메인 차단)에서는 돌지
않고, GCP 자격이 있는 환경에서 실행한다. 의존성(bigquery/db-dtypes/duckdb/pandas)은
전부 base + dev에 있어 **기본 ``uv run``(dev 그룹 자동 포함)으로 돈다** — feast
패키지를 import하지 않으므로 ``--group feast``를 붙이면 dev와 충돌만 난다.

사용법:
  uv run python scripts/diff_feature_contract.py
  uv run python scripts/diff_feature_contract.py --days 2026-07-07,2026-07-12
  uv run python scripts/diff_feature_contract.py --dump-dir .tmp/diff --include-video

주의(결과 해석): 정상 데이터는 11일뿐이다(#365 폐루프 구멍). 이 diff는 **n=11일,
참고용**이며 결론 확정용이 아니다. 결손일을 섞으면 stale 값이 diff를 거짓 "일치"로
보이게 하므로 기본 대상일은 결손 없는 11일로 고정한다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from typing import TYPE_CHECKING

import pandas as pd
from dotenv import load_dotenv

# 파일로 직접 실행(python scripts/...)해도 src/autoresearch 패키지를 찾도록 repo root 추가.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import src.pipeline.build_training_dataset as btd  # noqa: E402
from autoresearch.jobs.feature_store_build import (  # noqa: E402
    _USER_DYNAMIC_SELECT,
    _VIDEO_SELECT,
)
from src.features.assembly import compute_video_features  # noqa: E402

if TYPE_CHECKING:
    from google.cloud import bigquery

# #356 Phase 0 백필로 확보된 결손 없는 정상 11일(KST). 나머지는 폐루프 수집 구멍(#365).
GOOD_DAYS: tuple[str, ...] = (
    "2026-07-07",
    "2026-07-12",
    "2026-07-13",
    "2026-07-14",
    "2026-07-15",
    "2026-07-16",
    "2026-07-17",
    "2026-07-18",
    "2026-07-19",
    "2026-07-20",
    "2026-07-21",
)

# offline user_dynamic_feature가 내는 7일/30일 집계 + affinity 컬럼. 두 경로가 같은
# 이름으로 출력하므로 이 목록으로 컬럼별 대조한다.
_USER_NUMERIC_COLS: tuple[str, ...] = (
    "recent_click_count_7d",
    "recent_view_count_7d",
    "recent_watch_time_7d",
    "recent_like_count_7d",
    "total_event_count_7d",
)
_USER_STRING_COL = "historical_category_affinity"

# raw action_log dt 파티션과 KST 자정의 오프셋. DuckDB as_of(naive UTC)를 KST 날짜로
# 환산할 때 쓴다.
_KST = timedelta(hours=9)

# affinity 30일 + 여유. query points보다 이만큼 이전 raw까지 로드해야 룩백이 온전하다.
_LOOKBACK_DAYS = 31


def _duck_parse_duration(duration_str: object) -> int:
    """build_training_dataset.main()의 중첩 parse_iso8601_duration을 복제한 것.

    compute_video_features 호출 전 프로덕션이 하는 전처리와 동일하게 ISO8601
    duration을 초로 바꾼다. PT#H#M#S만 처리하고 P#D(일)는 못 잡아 0이 된다 —
    프로덕션 동작 그대로다(offline은 P#D도 처리하므로 이 차이가 diff 후보).
    """
    import re

    if pd.isna(duration_str) or not isinstance(duration_str, str):
        return 0
    match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration_str)
    if match:
        hours, minutes, seconds = match.groups()
        return int(hours or 0) * 3600 + int(minutes or 0) * 60 + int(seconds or 0)
    return 0


def _kst_date(ts: pd.Series) -> pd.Series:
    """naive UTC timestamp 시리즈를 KST 캘린더 날짜(date)로 환산한다."""
    return (pd.to_datetime(ts) + _KST).dt.date


def _bq(client: bigquery.Client, sql: str) -> pd.DataFrame:
    return client.query(sql).to_dataframe()


def fetch_offline_user_dynamic(
    client: bigquery.Client,
    *,
    project: str,
    dataset: str,
    raw_dataset: str,
    day: str,
) -> pd.DataFrame:
    """offline 정의(``_USER_DYNAMIC_SELECT``)를 partition_date=day로 재실행한다.

    적재된 ``user_dynamic_feature`` 테이블을 읽지 않고 raw에서 재계산하므로, 결손
    파티션의 ttl stale fallback(#356)이 값에 섞이지 않는다. ``users`` CTE가 기존
    피처 테이블 user_id를 UNION하지만 값은 raw에서 다시 계산되고, 아래 조인이
    (user_id, KST 날짜)로 inner join이라 임프레션 없는 잉여 유저는 자연히 빠진다.
    """
    sql = _USER_DYNAMIC_SELECT.format(
        project=project, dataset=dataset, raw_dataset=raw_dataset, partition_date=day
    )
    df = _bq(client, sql)
    # 모든 행의 event_timestamp = day 00:00 KST 스냅샷 1개이므로 KST 날짜는 day 고정.
    df["kst_date"] = date.fromisoformat(day)
    return df


def fetch_offline_video(
    client: bigquery.Client, *, project: str, raw_dataset: str, day: str
) -> pd.DataFrame:
    """offline 정의(``_VIDEO_SELECT``)를 partition_date=day로 재실행한다."""
    sql = _VIDEO_SELECT.format(project=project, raw_dataset=raw_dataset, partition_date=day)
    return _bq(client, sql)


def compute_duck_user_dynamic(
    wide_events: pd.DataFrame, videos_raw: pd.DataFrame, days: tuple[str, ...]
) -> pd.DataFrame:
    """DuckDB 학습 경로로 대상일 임프레션별 user_dynamic 피처를 계산한다.

    query_points = 대상일(KST)에 발생한 실제 임프레션(as_of=impression 시각).
    event_log = 전체 wide 이벤트(룩백 포함). 한 (user, KST 날짜)에 임프레션이 여러
    개면 행도 여러 개 나오며, 그 변동을 호출부가 직접 센다.
    """
    target = {date.fromisoformat(d) for d in days}
    kst = _kst_date(wide_events["timestamp"])
    impressions = wide_events[kst.isin(target)].copy()
    query_points = impressions[["user_id", "timestamp", "event_id", "video_id"]].rename(
        columns={"timestamp": "as_of"}
    )
    duck = btd.compute_point_in_time_user_features(wide_events, videos_raw, query_points)
    duck["kst_date"] = _kst_date(duck["as_of"])
    return duck


def _numeric_stats(merged: pd.DataFrame, col: str) -> dict[str, object]:
    duck = pd.to_numeric(merged[f"{col}__duck"], errors="coerce")
    off = pd.to_numeric(merged[f"{col}__off"], errors="coerce")
    diff = duck - off
    match = (duck == off) | (duck.isna() & off.isna())
    n = len(merged)
    return {
        "column": col,
        "kind": "numeric",
        "n": n,
        "n_match": int(match.sum()),
        "n_mismatch": int((~match).sum()),
        "mismatch_rate": round(float((~match).mean()), 4) if n else 0.0,
        "diff_mean": round(float(diff.mean()), 4) if n else 0.0,
        "diff_abs_max": round(float(diff.abs().max()), 4) if n else 0.0,
    }


def _string_stats(merged: pd.DataFrame, col: str) -> dict[str, object]:
    duck = merged[f"{col}__duck"].astype("string")
    off = merged[f"{col}__off"].astype("string")
    match = duck == off
    n = len(merged)
    mism = merged[~match]
    top_pairs = (
        mism.assign(_pair=duck[~match] + " != " + off[~match])["_pair"]
        .value_counts()
        .head(5)
        .to_dict()
    )
    return {
        "column": col,
        "kind": "string",
        "n": n,
        "n_match": int(match.sum()),
        "n_mismatch": int((~match).sum()),
        "mismatch_rate": round(float((~match).mean()), 4) if n else 0.0,
        "top_mismatch_pairs": {str(k): int(v) for k, v in top_pairs.items()},
    }


def _variance_within_day(duck: pd.DataFrame) -> dict[str, int]:
    """같은 (user_id, KST 날짜)에서 임프레션 간 값이 흔들리는 그룹 수를 컬럼별로 센다.

    0이면 "당일 제외라 하루 안에선 값이 같다"는 가정의 실측 증거. >0이면 하루가 두
    UTC 날짜에 걸쳐 DuckDB day 버킷(naive UTC)이 갈린 것 → timezone diff의 직접 증거.
    """
    grp = duck.groupby(["user_id", "kst_date"])
    cols = list(_USER_NUMERIC_COLS) + [_USER_STRING_COL]
    return {col: int((grp[col].nunique() > 1).sum()) for col in cols}


def compare_user_dynamic(
    duck: pd.DataFrame, offline: pd.DataFrame
) -> tuple[list[dict[str, object]], dict[str, object], pd.DataFrame]:
    """DuckDB(임프레션별) ↔ offline(일 스냅샷)을 (user_id, KST 날짜)로 조인해 대조한다."""
    keep = ["user_id", "kst_date", *_USER_NUMERIC_COLS, _USER_STRING_COL]
    off = offline[keep].drop_duplicates(["user_id", "kst_date"])
    merged = duck.merge(
        off, on=["user_id", "kst_date"], how="inner", suffixes=("__duck", "__off")
    )
    stats = [_numeric_stats(merged, c) for c in _USER_NUMERIC_COLS]
    stats.append(_string_stats(merged, _USER_STRING_COL))

    duck_keys = set(map(tuple, duck[["user_id", "kst_date"]].drop_duplicates().to_numpy()))
    off_keys = set(map(tuple, off[["user_id", "kst_date"]].to_numpy()))
    coverage = {
        "compared_impressions": len(merged),
        "duck_user_days": len(duck_keys),
        "offline_user_days": len(off_keys),
        "offline_only_user_days": len(off_keys - duck_keys),
        "duck_only_user_days": len(duck_keys - off_keys),
        "within_day_variance_groups": _variance_within_day(duck),
    }
    return stats, coverage, merged


def compare_video(duck_video: pd.DataFrame, offline_video: pd.DataFrame) -> list[dict[str, object]]:
    """video_feature를 video_id로 대조한다(2차·근사).

    정렬 근사: DuckDB는 video_trending_date, offline은 collected_at 기준 최신을 고르므로
    (지점 6의 스냅샷 선택 차이) latest-per-video로 축약해 붙인다 — 값 diff와 선택 diff가
    함께 잡힌다. days_since_upload는 DuckDB가 run-date(snapshot_date), offline이
    collected_at 기준이라 정의가 달라 equality에서 제외하고 분포만 참고 출력한다.
    """
    d = duck_video.copy()
    if "video_trending_date" in d.columns:
        d = d.sort_values("video_trending_date").drop_duplicates("video_id", keep="last")
    o = offline_video.sort_values("event_timestamp").drop_duplicates("video_id", keep="last")
    merged = d.merge(o, on="video_id", how="inner", suffixes=("__duck", "__off"))
    cols = ["category_id", "duration_sec", "view_count", "like_ratio", "comment_ratio"]
    out: list[dict[str, object]] = []
    for c in cols:
        if f"{c}__duck" not in merged.columns:
            continue
        stat = (
            _string_stats(merged, c)
            if c == "category_id"
            else _numeric_stats(merged, c)
        )
        out.append(stat)
    return out


def _print_user_report(stats: list[dict[str, object]], coverage: dict[str, object]) -> None:
    print("■ user_dynamic_feature (DuckDB ↔ offline, (user_id, KST 날짜) 조인)")
    print(f"  비교 임프레션 {coverage['compared_impressions']} / "
          f"duck user-day {coverage['duck_user_days']} / offline user-day {coverage['offline_user_days']}")
    print(f"  커버리지 delta: offline-only {coverage['offline_only_user_days']} user-day, "
          f"duck-only {coverage['duck_only_user_days']} user-day (무활동 유저 기본값·지점4)")
    for s in stats:
        head = f"  - {s['column']}: 불일치 {s['n_mismatch']}/{s['n']} ({s['mismatch_rate']:.1%})"
        if s["kind"] == "numeric":
            print(f"{head}  diff_mean={s['diff_mean']} abs_max={s['diff_abs_max']}")
        else:
            pairs = ", ".join(f"{k}×{v}" for k, v in s["top_mismatch_pairs"].items())
            print(f"{head}")
            if pairs:
                print(f"      top: {pairs}")
    var = coverage["within_day_variance_groups"]
    hot = {k: v for k, v in var.items() if v}
    print("  하루내 변동 그룹(당일제외 가정 검증·지점1/2): "
          + ("없음 → 가정 성립" if not hot else f"{hot}  ⚠️ timezone 버킷 diff 신호"))
    print()


def _print_video_report(stats: list[dict[str, object]]) -> None:
    print("■ video_feature (video_id 조인, 2차·latest-per-video 근사)")
    for s in stats:
        head = f"  - {s['column']}: 불일치 {s['n_mismatch']}/{s['n']} ({s['mismatch_rate']:.1%})"
        if s["kind"] == "numeric":
            print(f"{head}  diff_mean={s['diff_mean']} abs_max={s['diff_abs_max']}")
        else:
            print(head)
    print()


def _dump(dump_dir: str, name: str, df: pd.DataFrame) -> None:
    os.makedirs(dump_dir, exist_ok=True)
    path = os.path.join(dump_dir, name)
    df.to_csv(path, index=False)
    print(f"  [dump] {path} ({len(df)} rows)")


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.environ.get("CTR_TRAINING_BQ_PROJECT")
                        or os.environ.get("GCP_PROJECT_ID"))
    parser.add_argument("--dataset", default=os.environ.get("CTR_TRAINING_BQ_DATASET", "feast_offline_store"))
    parser.add_argument("--raw-dataset", default=os.environ.get("CTR_TRAINING_BQ_RAW_DATASET", "data_lake_raw"))
    parser.add_argument("--days", type=lambda v: tuple(x.strip() for x in v.split(",") if x.strip()),
                        default=GOOD_DAYS, help="대상 KST 날짜 CSV (기본: 결손 없는 11일)")
    parser.add_argument("--include-video", action="store_true", help="video_feature 2차 대조 포함")
    parser.add_argument("--dump-dir", default=None, help="불일치 표본 CSV 저장 위치")
    parser.add_argument("--out", default=None, help="요약 통계 JSON 저장 경로")
    args = parser.parse_args(argv)
    if not args.project:
        raise SystemExit("--project (또는 CTR_TRAINING_BQ_PROJECT/GCP_PROJECT_ID)가 필요합니다")

    # build_training_dataset의 모듈 전역을 호출 시점에 읽으므로 여기서 재지정한다
    # (그 모듈 docstring이 명시한 monkeypatch 지점).
    btd.BIGQUERY_PROJECT = args.project
    btd.BIGQUERY_RAW_DATASET = args.raw_dataset

    days = args.days
    print(f"[diff] {args.project}: offline={args.dataset} raw={args.raw_dataset} / 대상 {len(days)}일 (n={len(days)}, 참고용)\n")
    from google.cloud import bigquery

    client = bigquery.Client(project=args.project)

    # 1) raw 로드: 대상일 최소 ~ 최대 + 룩백/크로스미드나잇 여유. 한 번만 읽고 아래서 슬라이스.
    day_dates = sorted(date.fromisoformat(d) for d in days)
    lo = (day_dates[0] - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    hi = (day_dates[-1] + timedelta(days=1)).isoformat()  # 크로스미드나잇 click 귀속용
    long_events = btd.load_events_from_bigquery(lo, hi)
    if getattr(long_events["event_timestamp"].dtype, "tz", None) is not None:
        long_events["event_timestamp"] = (
            long_events["event_timestamp"].dt.tz_convert("UTC").dt.tz_localize(None)
        )
    videos = btd.load_videos_from_bigquery()
    wide = btd.derive_wide_events(long_events)
    del long_events

    # 2) DuckDB 경로 1회 계산(대상일 임프레션 전체).
    duck = compute_duck_user_dynamic(wide, videos, days)

    # 3) offline 경로: partition_date별 재실행 후 concat.
    offline = pd.concat(
        [
            fetch_offline_user_dynamic(
                client, project=args.project, dataset=args.dataset,
                raw_dataset=args.raw_dataset, day=d,
            )
            for d in days
        ],
        ignore_index=True,
    )

    stats, coverage, merged = compare_user_dynamic(duck, offline)
    _print_user_report(stats, coverage)

    payload: dict[str, object] = {
        "days": list(days),
        "user_dynamic": {"stats": stats, "coverage": coverage},
    }

    if args.dump_dir:
        mask = pd.Series(False, index=merged.index)
        for c in _USER_NUMERIC_COLS:
            mask |= pd.to_numeric(merged[f"{c}__duck"], errors="coerce") != pd.to_numeric(
                merged[f"{c}__off"], errors="coerce"
            )
        mask |= merged[f"{_USER_STRING_COL}__duck"].astype("string") != merged[
            f"{_USER_STRING_COL}__off"
        ].astype("string")
        _dump(args.dump_dir, "user_dynamic_mismatch.csv", merged[mask])

    if args.include_video:
        # compute_video_features는 duration이 이미 초 단위 정수라고 가정한다.
        # build_training_dataset.main이 호출 전에 하는 ISO8601 파싱을 그대로 재현한다
        # (그 파서는 main 내부 중첩 함수라 import 불가 → 복제). 그쪽 파서가 바뀌면
        # 여기도 같이 고쳐야 한다(drift 주의). PT#H#M#S만 처리하고 P#D는 못 잡아 0이
        # 되는 것까지 프로덕션과 동일 — 이 자체가 offline(P#D 처리)과의 diff 후보다.
        vids = videos.copy()
        if "duration" in vids.columns:
            vids["duration"] = vids["duration"].apply(_duck_parse_duration)
        duck_video = compute_video_features(vids, day_dates[-1].isoformat())
        offline_video = pd.concat(
            [fetch_offline_video(client, project=args.project, raw_dataset=args.raw_dataset, day=d)
             for d in days],
            ignore_index=True,
        )
        video_stats = compare_video(duck_video, offline_video)
        _print_video_report(video_stats)
        payload["video"] = {"stats": video_stats}

    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        print(f"[out] {args.out}")

    total_mismatch = sum(int(s["n_mismatch"]) for s in stats)
    print("=" * 60)
    print(f"결과: user_dynamic 불일치 총 {total_mismatch}건 "
          + ("→ 완전 일치(정의 동형)" if total_mismatch == 0 else "→ 정본(offline) 채택 대상, 원인 기록 필요"))
    return 0


if __name__ == "__main__":
    # Windows 콘솔 기본 cp949는 출력/도움말의 em-dash·화살표·이모지를 인코딩 못 해
    # UnicodeEncodeError로 죽는다. 크래시를 막으려 UTF-8로 재설정한다(리다이렉트된
    # 파이프처럼 reconfigure가 없는 스트림은 건너뛴다).
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
