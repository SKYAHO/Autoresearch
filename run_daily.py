"""매일 한국(KR) 인기 급상승 트렌딩을 수집해서

  1) 연/월 폴더 아래 일자별 parquet 으로 저장
        data/2026/06/youtube_trending_KR_2026-06-30.parquet
  2) 마스터 파일에 append
        data/youtube_trending_videos_global.parquet

같은 날짜로 다시 실행하면 그 날짜 데이터를 교체(중복 누적 방지)한다.

사용법:
  export YOUTUBE_API_KEY="발급받은_키"
  python run_daily.py                 # KR, 200개
  python run_daily.py --region US --max 50
"""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import fetch_trending_dataset as ft

KST = ZoneInfo("Asia/Seoul")
DATA_DIR = Path("data")
MASTER = DATA_DIR / "youtube_trending_videos_global.parquet"


def main() -> None:
    ap = argparse.ArgumentParser(description="일일 트렌딩 수집 + append")
    ap.add_argument("--region", default="KR", help="국가 코드 (기본 KR)")
    ap.add_argument("--max", type=int, default=200, help="수집할 영상 수 (기본 200)")
    ap.add_argument("--master", default=str(MASTER), help="append 대상 마스터 parquet")
    args = ap.parse_args()

    region = ft.normalize_region(args.region)
    max_results = ft.validate_max_results(args.max)
    key = ft.load_api_key()
    now = datetime.now(KST)
    dot_date = now.strftime("%Y.%m.%d")   # 마스터/일자 컬럼 포맷 (2026.06.30)
    iso_date = now.strftime("%Y-%m-%d")   # 파일명 포맷

    print(f"[{iso_date} KST] {region} 트렌딩 수집 (max {max_results})")
    rows = ft.build_rows(key, region, dot_date, max_results)
    if not rows:
        print("수집된 행이 없습니다. 종료.")
        return
    df = ft.to_dataframe(rows)

    # 1) 연/월 폴더 아래 일자별 parquet
    day_dir = DATA_DIR / now.strftime("%Y") / now.strftime("%m")
    day_dir.mkdir(parents=True, exist_ok=True)
    day_path = day_dir / f"youtube_trending_{region}_{iso_date}.parquet"
    df.to_parquet(day_path, index=False)
    print(f"  일자 파일 저장: {day_path}  ({len(df)} rows)")

    # 2) 마스터에 append (같은 날짜 데이터는 먼저 제거 후 추가 = 재실행 안전)
    country = ft.REGION_NAMES.get(region, region)
    today_ts = pd.to_datetime(iso_date)  # 날짜(datetime) 비교용
    master = Path(args.master)
    master.parent.mkdir(parents=True, exist_ok=True)
    if master.exists():
        old = ft.coerce_types(pd.read_parquet(master))  # 기존 파일도 동일 타입으로 정규화
        before = len(old)
        same_day = (
            (old["video_trending__date"] == today_ts)
            & (old["video_trending_country"] == country)
        )
        old = old[~same_day]
        removed = before - len(old)
        if removed:
            print(f"  마스터에서 같은 날짜 기존 {removed}행 제거(교체)")
        combined = pd.concat([old, df], ignore_index=True)
    else:
        combined = df

    combined = ft.coerce_types(combined)
    # 기존 파일을 직접 열어 덮어쓰면 파일 권한에 막힐 수 있으므로
    # 임시 파일에 쓴 뒤 원자적으로 교체한다(폴더 쓰기 권한만 있으면 됨).
    tmp = master.with_name(master.name + ".tmp")
    combined.to_parquet(tmp, index=False)
    os.replace(tmp, master)
    print(f"  마스터 append 완료: {master}  (총 {len(combined)} rows)")


if __name__ == "__main__":
    main()
