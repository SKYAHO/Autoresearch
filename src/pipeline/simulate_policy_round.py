#!/usr/bin/env python3
"""정책 시뮬레이션 라운드 배치.

baseline(키워드 휴리스틱) vs model(Reranker Top-K) 정책을 같은 유저·영상
pool에서 병행 노출하고, LLM 판정(합집합 1회)·합동 CTR 정규화를 거쳐 정책
태깅된 event log와 비교 리포트를 산출한다.

주의: 두 정책이 같은 (user, video)를 노출하면 동일 판정을 공유하되 이벤트
행은 정책별로 분리 생성된다. 재학습 등 downstream은 반드시 policy 컬럼으로
필터링해야 한다(정책 간 attribution 오염 방지).

spec: docs/specs/2026-07-20-policy-simulation-round.md
"""

from __future__ import annotations

__arch__ = {
    "stage": "training",
    "role": "두 정책의 노출·판정·정규화 결과를 배치 리포트로 조립합니다.",
    "owns": [
        "baseline/model 정책 비교 배치 실행",
        "합동 판정과 정책별 이벤트 확장",
        "JSON/HTML 정책 라운드 리포트 출력",
    ],
    "not_owns": [
        "Reranker 모델 학습 및 구현",
        "원천 데이터 수집",
    ],
}

import argparse
import json
import os
import random
from pathlib import Path

import pandas as pd

from autoresearch.action_logs.candidate import build_candidates
from autoresearch.action_logs.llm_generator import (
    OpenRouterActionLogGenerator,
    RuleBasedActionLogGenerator,
)
from autoresearch.action_logs.pipeline import (
    ActionLogGenerator,
    ExposureMetadata,
    _expand_events,
    generate_action_log_drafts,
    normalize_clicks,
    write_event_log_parquet,
    write_event_log_warehouse_jsonl,
    write_quarantine_jsonl,
)
from autoresearch.action_logs.schema import (
    ACTION_LOG_SCHEMA_VERSION,
    PROMPT_VERSION,
    SOURCE_ONLINE_SIMULATED,
    EventGenerationRequest,
    EventLog,
    EventLogBatch,
    ImpressionDraft,
)
from src.features.assembly import (
    compute_interaction_columns,
    compute_point_in_time_user_features,
    compute_user_offline_features,
    compute_video_features,
)
from src.pipeline.policy_selector import Exposure, select_exposures
from src.pipeline.report_html import render_report_html
from src.serving.model_loader import (
    load_model_settings_from_environment,
    load_reranker,
)
from src.serving.schemas import CandidateVideo
from src.serving.service import Reranker

BASELINE = "baseline"
MODEL = "model"


def build_pool_feature_frame(
    personas: pd.DataFrame,
    events: pd.DataFrame,
    videos_raw: pd.DataFrame,
    user_id: str,
    as_of: str,
    snapshot_date: str | None = None,
) -> pd.DataFrame:
    """유저 1명 × 전체 영상 pool의 15개 모델 피처 프레임을 학습과 동일 경로로 만든다.

    snapshot_date(YYYY-MM-DD)는 영상 나이(days_since_upload) 기준일이며, 유저
    이력 기준(as_of)과 다를 수 있다. 없으면 as_of의 날짜를 사용한다(기존 동작).
    """
    video_features = compute_video_features(videos_raw, snapshot_date or as_of.split(" ")[0])
    offline = compute_user_offline_features(personas)
    user_offline = offline[offline["user_id"] == user_id]
    if user_offline.empty:
        raise KeyError(f"persona not found for user_id={user_id}")
    query = pd.DataFrame({"user_id": [user_id], "as_of": [as_of]})
    online = compute_point_in_time_user_features(events, videos_raw, query)

    frame = video_features.copy()
    for column in ("age_group", "occupation"):
        frame[column] = user_offline.iloc[0][column]
    for column in (
        "historical_category_affinity",
        "recent_click_count_7d",
        "recent_watch_time_7d",
        "recent_like_count_7d",
    ):
        frame[column] = online.iloc[0][column]
    persona_row = personas[personas["uuid"] == user_id].iloc[0]
    frame["hobbies_and_interests_list"] = persona_row["hobbies_and_interests_list"]
    frame = compute_interaction_columns(frame)
    return frame


def _to_candidate_videos(frame: pd.DataFrame, feature_columns: tuple[str, ...]) -> list[CandidateVideo]:
    """피처 프레임을 Reranker 입력(CandidateVideo 목록)으로 변환한다.

    None/NaN 수치는 float('nan')으로 통일한다(FeatureValue는 None을 허용하지 않는다).
    """
    candidates: list[CandidateVideo] = []
    for _, row in frame.iterrows():
        features = {}
        for column in feature_columns:
            value = row[column]
            if value is None or (isinstance(value, float) and pd.isna(value)):
                value = float("nan")
            elif pd.isna(value):
                value = float("nan")
            features[column] = value
        candidates.append(CandidateVideo(video_id=str(row["video_id"]), features=features))
    return candidates


def main(
    personas: pd.DataFrame,
    virtual_users: list[dict],
    videos_raw: pd.DataFrame,
    events: pd.DataFrame,
    generator: ActionLogGenerator,
    reranker: Reranker | None = None,
    *,
    k: int = 10,
    exploration_ratio: float = 0.1,
    target_ctr: float = 0.02,
    seed: int = 42,
    chunk_size: int = 0,
    max_concurrency: int = 1,
    policy_version: str = "local",
    as_of: str = "2026-07-20 00:00:00",
    output_dir: str = "data/generated/policy_round",
) -> dict:
    """정책 시뮬레이션 라운드를 실행하고 리포트 dict를 반환한다."""
    if reranker is None:
        reranker = load_reranker(load_model_settings_from_environment())  # fail-fast

    video_by_id = {str(v["video_id"]): v for v in videos_raw.to_dict("records")}

    # 1) 유저별 두 정책의 노출 결정 (+ 스코어링 진단 수집)
    exposures_by_user: dict[str, dict[str, list[Exposure]]] = {}
    unseen_counts: dict[str, int] = {}
    skipped_users: list[str] = []
    for index, virtual_user in enumerate(virtual_users):
        user_id = str(virtual_user.get("user_id", f"user_{index}"))
        try:
            frame = build_pool_feature_frame(personas, events, videos_raw, user_id, as_of)
            candidates = _to_candidate_videos(frame, reranker.feature_columns)
            outcome = reranker.rerank_with_diagnostics(candidates)
        except KeyError:
            skipped_users.append(user_id)  # 유저 단위 격리: persona 누락 등
            continue
        for column, values in outcome.unseen_categories.items():
            unseen_counts[column] = unseen_counts.get(column, 0) + len(values)

        model_rng = random.Random(f"{seed}:model:{user_id}")
        model_exposures = select_exposures(outcome.items, k, exploration_ratio, model_rng)

        baseline_rng = random.Random(f"{seed}:{user_id}")  # 기존 pipeline seed 관례와 동일
        baseline_videos = build_candidates(
            virtual_user, list(video_by_id.values()), k, exploration_ratio, baseline_rng
        )
        baseline_exposures = [
            Exposure(video_id=str(v["video_id"]), rank=i + 1, ctr_score=None, is_exploration=None)
            for i, v in enumerate(baseline_videos)
        ]
        exposures_by_user[user_id] = {MODEL: model_exposures, BASELINE: baseline_exposures}

    # 2) 유저별 합집합 후보로 LLM 판정 1회 (provider 주입)
    union_by_user: dict[str, list[dict]] = {}
    for user_id, both in exposures_by_user.items():
        seen: set[str] = set()
        union: list[dict] = []
        for exposure in both[MODEL] + both[BASELINE]:
            if exposure.video_id in seen:
                continue
            seen.add(exposure.video_id)
            union.append(video_by_id[exposure.video_id])
        union_by_user[user_id] = union

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        return union_by_user.get(str(virtual_user.get("user_id", "")), [])

    request = EventGenerationRequest(
        target_ctr=target_ctr,
        candidates_per_user=max(1, 2 * k),
        seed=seed,
        chunk_size=chunk_size,
        max_concurrency=max_concurrency,
        output_path=str(Path(output_dir) / "event_log.parquet"),
        warehouse_output_path=str(Path(output_dir) / "event_log.jsonl"),
        quarantine_output_path=str(Path(output_dir) / "event_log_quarantine.jsonl"),
    )
    draft_result = generate_action_log_drafts(
        request, virtual_users, list(video_by_id.values()), generator,
        candidate_provider=provider,
    )
    draft_by_key: dict[tuple[str, str], ImpressionDraft] = {
        (d.user_id, d.video_id): d for d in draft_result.drafts
    }

    # 3) 합동 정규화 1회 → clicked (user, video) 키셋
    clicked_keys = {
        (draft_result.drafts[i].user_id, draft_result.drafts[i].video_id)
        for i in normalize_clicks(draft_result.drafts, target_ctr)
    }

    # 4) 정책별 이벤트 확장 (판정 없는 노출은 quarantine 여파로 제외하고 계수)
    all_events: list[EventLog] = []
    dropped = 0
    per_policy: dict[str, dict[str, float]] = {}
    for policy, prefix, seed_offset in ((BASELINE, "evt_b", 0), (MODEL, "evt_m", 1000)):
        policy_drafts: list[ImpressionDraft] = []
        metadata: dict[tuple[str, str], ExposureMetadata] = {}
        propensities: list[float] = []
        exploration_clicks = 0
        exploration_imps = 0
        for user_id, both in exposures_by_user.items():
            for exposure in both[policy]:
                draft = draft_by_key.get((user_id, exposure.video_id))
                if draft is None:
                    dropped += 1
                    continue
                policy_drafts.append(draft)
                propensities.append(draft.click_propensity)
                metadata[(user_id, exposure.video_id)] = ExposureMetadata(
                    policy=policy,  # type: ignore[arg-type]
                    rank=exposure.rank,
                    ctr_score=exposure.ctr_score,
                    is_exploration=exposure.is_exploration,
                    policy_version=policy_version,
                )
                if exposure.is_exploration:
                    exploration_imps += 1
                    if (user_id, exposure.video_id) in clicked_keys:
                        exploration_clicks += 1
        clicked_indices = {
            i for i, d in enumerate(policy_drafts) if (d.user_id, d.video_id) in clicked_keys
        }
        policy_request = request.model_copy(update={"seed": seed + seed_offset})
        events_out = _expand_events(
            policy_drafts, clicked_indices, policy_request,
            metadata=metadata, source=SOURCE_ONLINE_SIMULATED, event_id_prefix=prefix,
        )
        all_events.extend(events_out)
        impressions = len(policy_drafts)
        clicks = len(clicked_indices)
        per_policy[policy] = {
            "impressions": impressions,
            "clicks": clicks,
            "ctr": round(clicks / impressions, 4) if impressions else 0.0,
            "mean_click_propensity": (
                round(sum(propensities) / len(propensities), 4) if propensities else 0.0
            ),
            "exploration_impressions": exploration_imps,
            "exploration_clicks": exploration_clicks,
        }

    # 5) 노출 겹침률 (유저별 Jaccard 평균)
    jaccards: list[float] = []
    for both in exposures_by_user.values():
        a = {e.video_id for e in both[BASELINE]}
        b = {e.video_id for e in both[MODEL]}
        if a | b:
            jaccards.append(len(a & b) / len(a | b))
    overlap = round(sum(jaccards) / len(jaccards), 4) if jaccards else 0.0

    # 6) 저장 + 리포트
    batch = EventLogBatch(
        schema_version=ACTION_LOG_SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        request=request,
        events=all_events,
    )
    output_path = Path(request.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_event_log_parquet(batch, generator.model_name, output_path)
    write_event_log_warehouse_jsonl(batch, request.warehouse_output_path)
    write_quarantine_jsonl(draft_result.quarantine, request.quarantine_output_path)

    report = {
        "policy_version": policy_version,
        "k": k,
        "exploration_ratio": exploration_ratio,
        "target_ctr": target_ctr,
        "seed": seed,
        "users": len(exposures_by_user),
        "skipped_users": skipped_users,
        "dropped_exposures_without_judgment": dropped,
        "policies": per_policy,
        "overlap_jaccard_mean": overlap,
        "unseen_category_counts": unseen_counts,
        "quarantined_chunks": len(draft_result.quarantine),
    }
    report_path = Path(output_dir) / "policy_round_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    html_path = Path(output_dir) / "policy_round_report.html"
    html_path.write_text(render_report_html(report), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


def _cli() -> None:
    """파일 경로 인자를 로드해 main()에 전달하는 CLI 어댑터."""
    parser = argparse.ArgumentParser(description="정책 시뮬레이션 라운드 실행")
    parser.add_argument("--personas", required=True, help="persona csv/parquet 경로")
    parser.add_argument("--virtual-users", required=True, help="virtual user parquet 경로")
    parser.add_argument("--videos", required=True, help="videos_raw csv 경로 (youtube_videos.csv 형식)")
    parser.add_argument("--events", required=True, help="historical wide events csv 경로")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--exploration-ratio", type=float, default=0.1)
    parser.add_argument("--target-ctr", type=float, default=0.02)
    parser.add_argument("--max-users", type=int, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--chunk-size", type=int, default=0)
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--policy-version", default="local")
    parser.add_argument("--as-of", default=None, help="기준 시각 (기본: 현재 UTC)")
    parser.add_argument("--output-dir", default="data/generated/policy_round")
    parser.add_argument("--generator", choices=["openrouter", "rule-based"], default="openrouter")
    parser.add_argument("--log-mlflow", action="store_true")
    args = parser.parse_args()

    from datetime import UTC, datetime

    import pyarrow.parquet as pq

    from src.pipeline.build_training_dataset import load_personas

    personas = load_personas(args.personas)
    virtual_users = pq.read_table(args.virtual_users).to_pylist()
    if args.max_users is not None:
        virtual_users = virtual_users[: args.max_users]
    videos_raw = pd.read_csv(args.videos)
    events = pd.read_csv(args.events)
    generator = (
        RuleBasedActionLogGenerator() if args.generator == "rule-based"
        else OpenRouterActionLogGenerator()
    )
    as_of = args.as_of or datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")

    report = main(
        personas=personas,
        virtual_users=virtual_users,
        videos_raw=videos_raw,
        events=events,
        generator=generator,
        k=args.k,
        exploration_ratio=args.exploration_ratio,
        target_ctr=args.target_ctr,
        seed=args.seed,
        chunk_size=args.chunk_size,
        max_concurrency=args.max_concurrency,
        policy_version=args.policy_version,
        as_of=as_of,
        output_dir=args.output_dir,
    )

    if args.log_mlflow:
        import mlflow

        from src.tracking.client import get_or_create_experiment, set_tracking_uri
        from src.tracking.logger import log_metrics, log_parameters

        set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000"))
        experiment_id = get_or_create_experiment("ctr-model-training")
        with mlflow.start_run(experiment_id=experiment_id, run_name="policy-simulation-round"):
            log_parameters(
                {
                    "round_type": "policy_simulation",
                    "policy_version": report["policy_version"],
                    "k": report["k"],
                    "exploration_ratio": report["exploration_ratio"],
                    "target_ctr": report["target_ctr"],
                    "seed": report["seed"],
                    "users": report["users"],
                }
            )
            log_metrics(
                {
                    "baseline_ctr": report["policies"]["baseline"]["ctr"],
                    "model_ctr": report["policies"]["model"]["ctr"],
                    "baseline_mean_propensity": report["policies"]["baseline"]["mean_click_propensity"],
                    "model_mean_propensity": report["policies"]["model"]["mean_click_propensity"],
                    "overlap_jaccard_mean": report["overlap_jaccard_mean"],
                }
            )


if __name__ == "__main__":
    _cli()
