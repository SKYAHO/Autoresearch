"""feast 조립 경로 로컬 검증 (#358 작업 6, [EPIC] #299 Phase 2).

prod 레지스트리(GCS, GKE apply Job이 관리 #346)와 Redis(VPC 전용)를 건드리지 않고
머지 전에 feast 경로를 실데이터로 검증한다:

- 로컬 임시 레지스트리 + BigQuery offline store로 feature_definitions를 그대로 apply
  (online은 sqlite라 apply에 Redis 불필요, RepoConfig를 코드로 만들어 yaml cp949도 회피)
- 실제 offline 테이블에 retrieve_training_features(staged 조회)를 돌려 21피처를 붙인다
- spine 대비 손실(ttl 밖 행 드롭, 발견 #1)과 조회 시간(ODFV scale, 발견 #2)을 실측 보고

이 스크립트는 검증용이다 — 운영 조립은 build_training_dataset --assembly-source feast가
prod 레지스트리(deploy job이 apply)를 읽어 수행한다.

사용법:
  $env:PYTHONUTF8="1"   # 리눅스는 불필요
  uv run --no-sync --group feast python scripts/validate_feast_assembly.py \
    --start 2026-07-07 --end 2026-07-21 --out data/processed/ds_feast.csv
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time

from dotenv import load_dotenv

# 파일로 직접 실행(python scripts/...)해도 feature_repo/src를 찾도록 repo root 추가.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="KST 시작일 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="KST 종료일 YYYY-MM-DD(포함)")
    parser.add_argument("--out", default=None, help="21피처 CSV 저장 경로(선택)")
    parser.add_argument("--limit", type=int, default=None, help="spine 상한(빠른 스모크용)")
    args = parser.parse_args()

    project = os.environ.get("GCP_PROJECT_ID") or os.environ.get("CTR_TRAINING_BQ_PROJECT")
    dataset = os.environ.get("BQ_DATASET") or os.environ.get("CTR_TRAINING_BQ_DATASET", "feast_offline_store")
    staging = os.environ.get("GCS_STAGING_LOCATION")
    if not project or not staging:
        raise SystemExit("GCP_PROJECT_ID, GCS_STAGING_LOCATION 환경변수가 필요합니다")
    # feature_definitions가 import 시 요구.
    os.environ.setdefault("GCP_PROJECT_ID", project)
    os.environ.setdefault("BQ_DATASET", dataset)

    from feature_repo import feature_definitions as fd
    from autoresearch.feature_engineering.feast_retrieval import (
        build_offline_feature_store,
        retrieve_training_features,
    )
    from autoresearch.model_training.build_training_dataset import load_training_entity_spine

    out_abs = os.path.abspath(args.out) if args.out else None
    tmp = tempfile.mkdtemp(prefix="feast_validate_")
    # Feast file registry가 Windows 절대경로의 드라이브레터(C:)를 URI scheme로 오인하므로,
    # tmp로 chdir해 상대경로("registry.db")를 쓴다(스모크 테스트와 동일 회피).
    os.chdir(tmp)
    print(f"[validate] 로컬 레지스트리={tmp} / offline=bigquery {project}.{dataset}")
    # 프로덕션(_assemble_via_feast)과 동일한 store 빌더를 쓴다 — 검증이 곧 정본 경로.
    # 다만 레지스트리는 로컬 임시(prod은 GCS를 배포 job이 apply). 그래서 여기서만 apply한다.
    store = build_offline_feature_store(
        "registry.db", project=project, dataset=dataset,
        gcs_staging=staging, online_db_path="online.db",
    )
    store.apply(
        [
            fd.user_entity, fd.video_entity, fd.category_entity,
            fd.user_static_view, fd.user_dynamic_view, fd.video_feature_view,
            fd.user_category_similarity_view, fd.category_match_view,
            fd.ctr_training_service,
        ]
    )
    print("[validate] apply 완료(로컬 레지스트리) — 정의 9종")

    spine = load_training_entity_spine(args.start, args.end)
    if args.limit:
        spine = spine.head(args.limit)
    print(f"[validate] spine: {len(spine)} rows ({args.start}~{args.end})")

    t0 = time.time()
    features = retrieve_training_features(store, spine)
    elapsed = time.time() - t0

    from autoresearch.feature_engineering.feast_retrieval import (
        apply_cold_start_defaults,
        drop_user_dynamic_gap_rows,
    )
    from autoresearch.feature_engineering.model_contract import MODEL_FEATURE_COLUMNS

    missing = [c for c in MODEL_FEATURE_COLUMNS if c not in features.columns]
    print("\n" + "=" * 60)
    print(f"조회 시간: {elapsed:.1f}s  (ODFV scale 발견 #2)")
    print(f"spine {len(spine)} → 결과 {len(features)} rows "
          f"(손실 {len(spine) - len(features)}, ttl 밖 드롭 발견 #1)")
    print(f"21피처 누락: {missing if missing else '없음'}")
    _watch = ["topic_similarity", "category_id", "preferred_category_match",
              "historical_category_match", "recent_click_count_7d"]
    if not missing:
        raw = features[list(MODEL_FEATURE_COLUMNS)].notna().mean().round(3).to_dict()
        print("raw non-null 비율(발견 #3, video 미발견 갭):", {k: raw[k] for k in _watch})
        # (C) 결손 가시화: UserDynamic 전체 null(ttl 초과/#365)은 드롭. good days=0 기대.
        kept = drop_user_dynamic_gap_rows(features)
        print(f"UserDynamic 결손 드롭: {len(features) - len(kept)} 행 (#365 gap, good days 기대=0)")
        # 그 뒤 남는 null(영상 미발견 등)만 cold-start로 채운다(서빙과 같은 규칙).
        filled = apply_cold_start_defaults(kept)
        post = filled[list(MODEL_FEATURE_COLUMNS)].notna().mean().round(3).to_dict()
        print("cold-start 후 non-null 비율:", {k: post[k] for k in _watch})
        features = filled
    if out_abs and not missing:
        out = features[[*MODEL_FEATURE_COLUMNS, "clicked"]].copy()
        out["clicked"] = out["clicked"].astype(int)
        os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
        out.to_csv(out_abs, index=False)
        print(f"[저장] {out_abs} ({len(out)} rows)")
    print("=" * 60)
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
