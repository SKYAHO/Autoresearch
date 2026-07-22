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

import argparse
import json
import os
import random
from collections.abc import Mapping
from dataclasses import dataclass
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
    read_action_log_draft_parquet,
    select_clicks_per_slate,
    write_action_log_draft_parquet,
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
from src.features.model_contract import require_model_feature_columns
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

DRAFTS_FILENAME = "action_log_drafts.parquet"
DRAFTS_META_FILENAME = "action_log_drafts_meta.json"


@dataclass(frozen=True)
class DraftReplay:
    """저장된 LLM 판정과 그 계보.

    판정과 계보는 항상 함께 다뤄야 하므로(계보 없는 event log를 쓰지 않는다)
    한 값으로 묶는다. exposure_args는 판정 라운드의 노출 결정 인자이며 CLI가
    인자 상속·불일치 검사에 사용한다.
    """

    drafts: list[ImpressionDraft]
    llm_model: str
    exposure_args: Mapping[str, object]


def build_pool_feature_frame(
    personas: pd.DataFrame,
    events: pd.DataFrame,
    videos_raw: pd.DataFrame,
    user_id: str,
    as_of: str,
    snapshot_date: str | None = None,
) -> pd.DataFrame:
    """유저 1명 × 전체 영상 pool의 21개 모델 피처 프레임을 학습과 동일 경로로 만든다.

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
    for column in ("age_group", "occupation", "watch_time_band"):
        frame[column] = user_offline.iloc[0][column]
    for column in (
        "historical_category_affinity",
        "recent_click_count_7d",
        "recent_view_count_7d",
        "recent_watch_time_7d",
        "recent_like_count_7d",
        "total_event_count_7d",
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
    columns = require_model_feature_columns(feature_columns)
    candidates: list[CandidateVideo] = []
    for _, row in frame.iterrows():
        features = {}
        for column in columns:
            value = row[column]
            if value is None or (isinstance(value, float) and pd.isna(value)):
                value = float("nan")
            elif pd.isna(value):
                value = float("nan")
            features[column] = value
        candidates.append(CandidateVideo(video_id=str(row["video_id"]), features=features))
    return candidates


def _write_drafts_meta(
    path: Path,
    *,
    llm_model: str,
    exposure_args: Mapping[str, object],
    policy_version: str,
    virtual_users: int,
    users: int,
    drafts: int,
    input_paths: Mapping[str, str] | None,
) -> None:
    """draft parquet 옆에 계보와 노출 결정 인자를 사이드카 JSON으로 남긴다.

    llm_model을 draft parquet 컬럼이 아니라 사이드카에 두는 이유는
    ACTION_LOG_DRAFT_PARQUET_SCHEMA가 daily.py shard/merge와 공유하는 계약이기
    때문이다. click_threshold는 리플레이에서 바꾸는 값이므로 exposure_args에
    넣지 않는다.
    """
    payload = {
        "llm_model": llm_model,
        "prompt_version": PROMPT_VERSION,
        "schema_version": ACTION_LOG_SCHEMA_VERSION,
        "exposure_args": dict(exposure_args),
        "policy_version": policy_version,
        "virtual_users": virtual_users,
        "users": users,
        "drafts": drafts,
        "inputs": dict(input_paths or {}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


DEFAULT_EXPOSURE_ARGS: dict[str, object] = {"seed": 42, "k": 10, "exploration_ratio": 0.1}


def _read_drafts_meta(path: Path) -> dict:
    """draft 사이드카 메타를 읽는다.

    사이드카가 없으면 판정의 계보(llm_model)를 알 수 없고, 계보 없는 event log를
    쓰지 않는다는 규칙에 따라 실패한다.
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"draft 사이드카 메타가 없습니다: {path} — "
            "계보(llm_model)를 알 수 없어 event log를 쓸 수 없습니다"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_exposure_args(
    explicit: Mapping[str, object | None],
    defaults: Mapping[str, object],
    meta_exposure_args: Mapping[str, object] | None,
) -> dict[str, object]:
    """노출 결정 인자를 확정한다.

    meta_exposure_args가 None(신규 라운드)이면 미명시 인자를 기본값으로 채운다.
    리플레이면 미명시 인자를 판정 라운드에서 상속하고, 명시한 인자가 판정
    라운드와 다르면 ValueError를 던진다 — 노출이 달라지면 저장된 판정이 노출을
    덮지 못하고, "같은 판정 분포에 커트라인을 적용한다"는 캘리브레이션 전제가
    깨지기 때문이다.
    """
    resolved: dict[str, object] = {}
    mismatches: list[str] = []
    for key, default in defaults.items():
        given = explicit.get(key)
        if meta_exposure_args is None:
            resolved[key] = default if given is None else given
            continue
        if key not in meta_exposure_args:
            raise ValueError(f"replay 메타에 노출 인자 '{key}'가 없습니다")
        inherited = meta_exposure_args[key]
        if given is None or given == inherited:
            resolved[key] = inherited
        else:
            mismatches.append(f"{key}: 지정={given!r}, 판정 라운드={inherited!r}")
    if mismatches:
        raise ValueError(
            "replay 인자가 판정 라운드와 다릅니다 — " + "; ".join(mismatches)
        )
    return resolved


def main(
    personas: pd.DataFrame,
    virtual_users: list[dict],
    videos_raw: pd.DataFrame,
    events: pd.DataFrame,
    generator: ActionLogGenerator | None = None,
    reranker: Reranker | None = None,
    *,
    replay: DraftReplay | None = None,
    k: int = 10,
    exploration_ratio: float = 0.1,
    click_threshold: float,
    seed: int = 42,
    chunk_size: int = 0,
    max_concurrency: int = 1,
    policy_version: str = "local",
    as_of: str = "2026-07-20 00:00:00",
    output_dir: str = "data/generated/policy_round",
    input_paths: Mapping[str, str] | None = None,
) -> dict:
    """정책 시뮬레이션 라운드를 실행하고 리포트 dict를 반환한다."""
    if (generator is None) == (replay is None):
        raise ValueError(
            "generator와 replay 중 정확히 하나만 지정해야 합니다 "
            "(replay는 저장된 판정을 재사용하므로 generator가 필요 없습니다)"
        )
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

    # 2) 판정 확보 — 신규 라운드는 LLM 1회, 리플레이는 저장된 판정 재사용
    request = EventGenerationRequest(
        click_threshold=click_threshold,
        candidates_per_user=max(1, 2 * k),
        seed=seed,
        chunk_size=chunk_size,
        max_concurrency=max_concurrency,
        output_path=str(Path(output_dir) / "event_log.parquet"),
        warehouse_output_path=str(Path(output_dir) / "event_log.jsonl"),
        quarantine_output_path=str(Path(output_dir) / "event_log_quarantine.jsonl"),
    )
    exposure_args = {
        "seed": seed,
        "k": k,
        "exploration_ratio": exploration_ratio,
        "as_of": as_of,
    }

    if replay is None:
        assert generator is not None  # 위 XOR 검증이 보장한다
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

        draft_result = generate_action_log_drafts(
            request, virtual_users, list(video_by_id.values()), generator,
            candidate_provider=provider,
        )
        drafts = draft_result.drafts
        quarantine = draft_result.quarantine
        llm_model = generator.model_name

        _write_drafts_meta(
            Path(output_dir) / DRAFTS_META_FILENAME,
            llm_model=llm_model,
            exposure_args=exposure_args,
            policy_version=policy_version,
            virtual_users=len(virtual_users),
            users=len(exposures_by_user),
            drafts=len(drafts),
            input_paths=input_paths,
        )
        write_action_log_draft_parquet(drafts, Path(output_dir) / DRAFTS_FILENAME)
    else:
        drafts = replay.drafts
        quarantine = []  # 이번 실행에서 새로 격리된 판정이 없다
        llm_model = replay.llm_model

    draft_by_key: dict[tuple[str, str], ImpressionDraft] = {
        (d.user_id, d.video_id): d for d in drafts
    }

    if replay is not None:
        missing = [
            (user_id, exposure.video_id)
            for user_id, both in exposures_by_user.items()
            for exposure in both[MODEL] + both[BASELINE]
            if (user_id, exposure.video_id) not in draft_by_key
        ]
        if missing:
            raise ValueError(
                f"replay drafts do not cover {len(missing)} exposure(s) "
                f"(first missing: {missing[0]}) — 노출 결정 인자나 유저 집합이 "
                "판정 라운드와 다를 수 있습니다"
            )

    # 3) 합동 per-slate 선정 1회 → clicked (user, video) 키셋
    clicked_keys = {
        (drafts[i].user_id, drafts[i].video_id)
        for i in select_clicks_per_slate(drafts, click_threshold)
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
    write_event_log_parquet(batch, llm_model, output_path)
    write_event_log_warehouse_jsonl(batch, request.warehouse_output_path)
    write_quarantine_jsonl(quarantine, request.quarantine_output_path)

    report = {
        "policy_version": policy_version,
        "k": k,
        "exploration_ratio": exploration_ratio,
        "click_threshold": click_threshold,
        "seed": seed,
        "users": len(exposures_by_user),
        "skipped_users": skipped_users,
        "dropped_exposures_without_judgment": dropped,
        "policies": per_policy,
        "overlap_jaccard_mean": overlap,
        "unseen_category_counts": unseen_counts,
        "quarantined_chunks": len(quarantine),
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
    parser.add_argument("--k", type=int, default=None, help="기본 10 (리플레이면 판정 라운드에서 상속)")
    parser.add_argument("--exploration-ratio", type=float, default=None, help="기본 0.1 (리플레이면 상속)")
    parser.add_argument("--click-threshold", type=float, required=True)
    parser.add_argument("--max-users", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None, help="기본 42 (리플레이면 상속)")
    parser.add_argument("--chunk-size", type=int, default=0)
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--policy-version", default="local")
    parser.add_argument("--as-of", default=None, help="기준 시각 (기본: 현재 UTC)")
    parser.add_argument("--output-dir", default="data/generated/policy_round")
    parser.add_argument(
        "--generator", choices=["openrouter", "rule-based"], default=None,
        help="기본 openrouter. --replay-drafts와 함께 쓸 수 없습니다",
    )
    parser.add_argument(
        "--replay-drafts", default=None,
        help="저장된 draft parquet 경로. 지정하면 LLM 호출 없이 커트라인만 다시 적용합니다",
    )
    parser.add_argument("--log-mlflow", action="store_true")
    args = parser.parse_args()
    if args.replay_drafts is not None and args.generator is not None:
        parser.error("--generator는 --replay-drafts와 함께 쓸 수 없습니다 (저장된 판정을 재사용합니다)")

    from datetime import UTC, datetime

    import pyarrow.parquet as pq

    from src.pipeline.build_training_dataset import load_personas

    personas = load_personas(args.personas)
    virtual_users = pq.read_table(args.virtual_users).to_pylist()
    if args.max_users is not None:
        virtual_users = virtual_users[: args.max_users]
    videos_raw = pd.read_csv(args.videos)
    events = pd.read_csv(args.events)

    replay = None
    generator = None
    meta_exposure_args = None
    if args.replay_drafts is not None:
        meta = _read_drafts_meta(Path(args.replay_drafts).with_name(DRAFTS_META_FILENAME))
        if len(virtual_users) != meta["virtual_users"]:
            parser.error(
                f"virtual user 수가 판정 라운드와 다릅니다 "
                f"(지정={len(virtual_users)}, 판정 라운드={meta['virtual_users']}) "
                "— --max-users를 확인하세요"
            )
        meta_exposure_args = meta["exposure_args"]
        replay = DraftReplay(
            drafts=read_action_log_draft_parquet(args.replay_drafts),
            llm_model=str(meta["llm_model"]),
            exposure_args=meta_exposure_args,
        )
    else:
        generator = (
            RuleBasedActionLogGenerator() if args.generator == "rule-based"
            else OpenRouterActionLogGenerator()
        )

    resolved = resolve_exposure_args(
        explicit={
            "seed": args.seed,
            "k": args.k,
            "exploration_ratio": args.exploration_ratio,
            "as_of": args.as_of,
        },
        defaults={
            **DEFAULT_EXPOSURE_ARGS,
            "as_of": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        },
        meta_exposure_args=meta_exposure_args,
    )

    report = main(
        personas=personas,
        virtual_users=virtual_users,
        videos_raw=videos_raw,
        events=events,
        generator=generator,
        replay=replay,
        k=int(resolved["k"]),
        exploration_ratio=float(resolved["exploration_ratio"]),
        click_threshold=args.click_threshold,
        seed=int(resolved["seed"]),
        chunk_size=args.chunk_size,
        max_concurrency=args.max_concurrency,
        policy_version=args.policy_version,
        as_of=str(resolved["as_of"]),
        output_dir=args.output_dir,
        input_paths={
            "personas": args.personas,
            "virtual_users": args.virtual_users,
            "videos": args.videos,
            "events": args.events,
        },
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
                    "click_threshold": report["click_threshold"],
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
