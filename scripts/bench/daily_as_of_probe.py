"""daily_recommendations 단일 as_of 정합 probe (#359 A3-1).

문제: offline PIT(``get_historical_features``)는 entity 행당 event_timestamp가 **하나**뿐이라,
daily_recommendations가 지금 쓰는 "영상 나이=candidate_dt / 유저 이력=events_dt" **2개 기준
시각 분리**를 단일 as_of로 못 준다. 이 스크립트는 같은 (user, video) 표본에 대해 두 후보
as_of(candidate_dt+1 / events_dt+1)로 각각 조회해, **무엇이 달라지는지**를 실측으로 보여준다
→ 단일 as_of 하나로 충분한지, 분리가 필요한지 판단하는 근거.

읽는 법:
- ``days_since_upload``(영상 나이)는 as_of가 고르는 영상 스냅샷에 따라 달라진다 → candidate_dt
  기준이 되려면 as_of=candidate_dt+1이어야 한다.
- ``recent_click_count_7d``(유저 동적) non-null 비율은 그 as_of 시점에 UserDynamic 스냅샷이
  있는지를 보여준다 → 유저 이력이 events_dt까지 반영되는지.
- 두 as_of에서 값이 사실상 같으면 단일 as_of로 충분, 크게 다르면 분리(또는 2단 조회)가 필요.

사용법(feast 격리 그룹):
  $env:PYTHONUTF8 = "1"   # Windows
  uv run --only-group feast python scripts/bench/daily_as_of_probe.py \
    --candidate-dt 2026-07-21 --events-dt 2026-07-21 --limit 500
  # action log 지연 상황을 보려면 events-dt를 candidate-dt보다 앞으로:
  #   --candidate-dt 2026-07-21 --events-dt 2026-07-19
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _as_of(date_str: str) -> str:
    """KST 날짜 D → 'D+1 00:00:00'(naive UTC, build_pool_feature_frame_feast와 같은 관례)."""
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime(
        "%Y-%m-%d 00:00:00"
    )


def _summarize(features: pd.DataFrame) -> dict:
    """영상 나이·유저 동적 커버리지 요약(cold-start 적용 전 raw 조회 결과 기준)."""
    n = len(features)
    dsu = pd.to_numeric(features.get("days_since_upload"), errors="coerce")
    rc = pd.to_numeric(features.get("recent_click_count_7d"), errors="coerce")
    cat = features.get("category_id")
    return {
        "rows": n,
        "days_since_upload_nonnull%": round(float(dsu.notna().mean()) * 100, 1),
        "days_since_upload_median": None if dsu.notna().sum() == 0 else float(dsu.median()),
        "days_since_upload_min": None if dsu.notna().sum() == 0 else float(dsu.min()),
        "days_since_upload_max": None if dsu.notna().sum() == 0 else float(dsu.max()),
        "recent_click_7d_nonnull%": round(float(rc.notna().mean()) * 100, 1),
        "category_id_nonnull%": round(float(cat.notna().mean()) * 100, 1) if cat is not None else 0.0,
    }


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-dt", required=True, help="추천 대상일 KST YYYY-MM-DD")
    parser.add_argument("--events-dt", required=True, help="이벤트(action log) 마지막 KST 날짜")
    parser.add_argument("--limit", type=int, default=500, help="표본 (user, video) 쌍 수")
    args = parser.parse_args()

    project = os.environ.get("GCP_PROJECT_ID") or os.environ.get("CTR_TRAINING_BQ_PROJECT")
    dataset = os.environ.get("BQ_DATASET") or os.environ.get(
        "CTR_TRAINING_BQ_DATASET", "feast_offline_store"
    )
    staging = os.environ.get("GCS_STAGING_LOCATION")
    if not project or not staging:
        raise SystemExit("GCP_PROJECT_ID, GCS_STAGING_LOCATION 환경변수가 필요합니다")
    os.environ.setdefault("GCP_PROJECT_ID", project)
    os.environ.setdefault("BQ_DATASET", dataset)

    from feature_repo import feature_definitions as fd
    from src.features.feast_retrieval import (
        build_offline_feature_store,
        retrieve_training_features,
    )
    from src.pipeline.build_training_dataset import load_training_entity_spine

    tmp = tempfile.mkdtemp(prefix="as_of_probe_")
    os.chdir(tmp)  # Windows file registry 드라이브레터 회피(validate와 동일)
    store = build_offline_feature_store(
        "registry.db", project=project, dataset=dataset,
        gcs_staging=staging, online_db_path="online.db",
    )
    # 정본 정의를 로컬 임시 레지스트리에 apply (정본: scripts/validate_feast_assembly.py).
    store.apply(
        [
            fd.user_entity, fd.video_entity, fd.category_entity,
            fd.user_static_view, fd.user_dynamic_view, fd.video_feature_view,
            fd.user_category_similarity_view, fd.category_match_view,
            fd.ctr_training_service,
        ]
    )

    # 표본 (user, video) 쌍: events_dt 하루치 spine에서 뽑는다.
    pairs = load_training_entity_spine(args.events_dt, args.events_dt)[["user_id", "video_id"]]
    pairs = pairs.drop_duplicates().head(args.limit).reset_index(drop=True)
    print(f"[probe] 표본 (user, video) 쌍: {len(pairs)}개 (events_dt={args.events_dt})")

    results = {}
    for label, dt in (("candidate_dt+1", args.candidate_dt), ("events_dt+1", args.events_dt)):
        as_of = _as_of(dt)
        spine = pairs.assign(event_timestamp=pd.Timestamp(as_of, tz="UTC"))
        features = retrieve_training_features(store, spine)
        results[f"{label} (as_of={as_of})"] = _summarize(features)

    print("\n" + "=" * 72)
    print("as_of별 조회 결과 (cold-start 적용 전)")
    print("=" * 72)
    summary = pd.DataFrame(results).T
    with pd.option_context("display.max_columns", None, "display.width", 200):
        print(summary.to_string())
    print("=" * 72)
    print(
        "판단: days_since_upload(영상 나이)가 두 as_of에서 다르면 → 영상 스냅샷 기준일이\n"
        "  as_of에 좌우된다는 뜻(candidate_dt 기준을 원하면 as_of=candidate_dt+1 필요).\n"
        "  recent_click_7d non-null%가 낮으면 → 그 as_of엔 UserDynamic 스냅샷이 없음(ttl/결손).\n"
        "  두 컬럼이 사실상 같으면 단일 as_of로 충분, 크게 갈리면 분리(2단 조회) 필요."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
