#!/usr/bin/env python3
"""정책 시뮬레이션 라운드 배치.

키워드 휴리스틱 또는 Reranker Top-K로 구성한 여러 정책을 같은 유저·영상
pool에서 병행 노출하고, LLM 판정(합집합 1회)·합동 커트라인 판정을 거쳐 정책
태깅된 event log와 비교 리포트를 산출한다.

이 모듈이 담당하는 구간은 "노출 결정 → 판정 확보 → 커트라인 적용 → event log
산출"이다. 판정을 만드는 LLM 호출 규약과 클릭 선정 규칙 자체는
`autoresearch.action_logs`가 소유하며, 학습·평가와 GCS 적재는 담당하지 않는다.

제공 기능:

- 유저별 N개 정책 노출 결정과 정책별 스코어링 진단 수집
- LLM 판정 1회 실행(합집합 후보)과 판정 덤프
  (`action_log_drafts.parquet` + 계보·노출 인자·노출 키 집합 사이드카
  `action_log_drafts_meta.json`) — `click_threshold` 캘리브레이션 입력
- 저장된 판정 리플레이(`--replay-drafts`) — LLM 호출 없이 커트라인만 다시
  적용한다. 사이드카의 원본 노출 키 집합과 이번 노출이 다르면 fail-fast하고,
  동일하면 판정 없는 노출(원본 quarantine — chunk 부분 격리 포함)을 관용·계수
  한다(#274). 노출 키 집합이 없는 구버전 사이드카는 유저 단위 커버리지
  휴리스틱으로 폴백한다
- 정책별 event log(parquet/JSONL)·quarantine·비교 리포트(JSON/HTML) 산출

주의: 여러 정책이 같은 (user, video)를 노출하면 동일 판정을 공유하되 이벤트
행은 정책별로 분리 생성된다. 재학습 등 downstream은 반드시 policy 컬럼으로
필터링해야 한다(정책 간 attribution 오염 방지).

spec: docs/specs/2026-07-20-policy-simulation-round.md,
      docs/specs/2026-07-23-policy-round-draft-replay.md
"""

from __future__ import annotations

import argparse
import json
import os
import random
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING

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
    build_round_policy_event_id_prefix,
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
    validate_policy_name,
)
from src.features.assembly import (
    compute_interaction_columns,
    compute_point_in_time_user_features,
    compute_user_offline_features,
    compute_video_features,
)
from src.features.feast_retrieval import build_pool_feature_frame_feast
from src.features.model_contract import require_model_feature_columns
from src.pipeline.policy_selector import Exposure, select_exposures
from src.pipeline.report_html import render_report_html
from src.serving.model_loader import (
    load_model_settings_from_environment,
    load_reranker,
)
from src.serving.schemas import CandidateVideo
from src.serving.service import (
    MissingFeatureColumnsError,
    PredictionError,
    Reranker,
)

if TYPE_CHECKING:
    from feast import FeatureStore

BASELINE = "baseline"
MODEL = "model"

DRAFTS_FILENAME = "action_log_drafts.parquet"
DRAFTS_META_FILENAME = "action_log_drafts_meta.json"


@dataclass(frozen=True)
class PolicySpec:
    """정책 라운드에 투입할 baseline 또는 reranker 기반 정책 명세."""

    name: str
    reranker: Reranker | None
    version: str | None = None

    def __post_init__(self) -> None:
        """정책 이름이 event log와 sidecar에서 안전한 식별자인지 검증한다."""

        validate_policy_name(self.name)


@dataclass(frozen=True)
class DraftReplay:
    """저장된 LLM 판정과 그 계보.

    판정과 계보는 항상 함께 다뤄야 하므로(계보 없는 event log를 쓰지 않는다)
    한 값으로 묶는다. exposure_args는 판정 라운드의 노출 결정 인자이며 CLI가
    인자 상속·불일치 검사에 사용한다. policy_exposures는 정책별 순서·rank·
    score·exploration·version을 보존하는 엄격한 리플레이 스냅샷이다.
    exposure_keys는 이 스냅샷이 없는 구버전 사이드카의 유저별 합집합
    video_id 비교에 사용한다(#274).
    """

    drafts: list[ImpressionDraft]
    llm_model: str
    exposure_args: Mapping[str, object]
    exposure_keys: Mapping[str, frozenset[str]] | None = None
    round_id: str | None = None
    policy_versions: Mapping[str, str] | None = None
    policy_exposures: (
        Mapping[str, Mapping[str, Sequence[Mapping[str, object]]]] | None
    ) = None


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
    exposure_keys: Mapping[str, list[str]],
    policy_versions: Mapping[str, str],
    policy_exposures: Mapping[
        str, Mapping[str, Sequence[Mapping[str, object]]]
    ],
    policy_version: str,
    round_id: str,
    virtual_users: int,
    users: int,
    drafts: int,
    input_paths: Mapping[str, str] | None,
) -> None:
    """draft parquet 옆에 계보와 노출 결정 인자를 사이드카 JSON으로 남긴다.

    llm_model을 draft parquet 컬럼이 아니라 사이드카에 두는 이유는
    ACTION_LOG_DRAFT_PARQUET_SCHEMA가 daily.py shard/merge와 공유하는 계약이기
    때문이다. click_threshold는 리플레이에서 바꾸는 값이므로 exposure_args에
    넣지 않는다. exposure_keys(유저별 합집합 노출 video_id 목록)는 리플레이의
    커버리지 정확 비교 기준이다 — draft parquet은 격리된 청크의 판정을 담지
    않으므로, "무엇이 노출되었어야 했는가"는 사이드카만이 안다(#274).
    """
    payload = {
        "llm_model": llm_model,
        "prompt_version": PROMPT_VERSION,
        "schema_version": ACTION_LOG_SCHEMA_VERSION,
        "exposure_args": dict(exposure_args),
        "exposure_keys": {user: sorted(keys) for user, keys in exposure_keys.items()},
        "policy_versions": dict(policy_versions),
        "policy_exposures": {
            user: {
                policy: [dict(exposure) for exposure in exposures]
                for policy, exposures in by_policy.items()
            }
            for user, by_policy in policy_exposures.items()
        },
        "policy_version": policy_version,
        "round_id": round_id,
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


def _validate_replay_exposure_args(
    replay_exposure_args: Mapping[str, object],
    actual_exposure_args: Mapping[str, object],
) -> None:
    """리플레이 판정의 노출 인자가 이번 실행의 노출 인자와 일치하는지 검사한다.

    `_cli()`의 `resolve_exposure_args`는 인자 상속·불일치 검사를 CLI 계층에서만
    수행하므로, `main()`을 직접 호출하는 경로(테스트·후속 배치)에서는 불변식이
    강제되지 않는다. 노출이 달라지면 저장된 판정과 노출이 어긋나 CTR 분모가
    왜곡되므로 `main()` 자체에서도 검사한다. `resolve_exposure_args`와 동일하게
    `==` 비교를 쓴다 — JSON 왕복(예: `exploration_ratio` 0.0)과 파이썬 값은
    `==`로 동등하므로 과탐 없이 충분하다.
    """
    mismatches = [
        f"{key}: 판정 라운드={replay_exposure_args.get(key)!r}, 이번 실행={value!r}"
        for key, value in actual_exposure_args.items()
        if replay_exposure_args.get(key) != value
    ]
    if mismatches:
        raise ValueError(
            "replay.exposure_args가 이번 실행의 노출 인자와 다릅니다 — "
            + "; ".join(mismatches)
        )


def _validate_replay_exposure_keys(
    original: Mapping[str, frozenset[str]],
    exposures_by_user: Mapping[str, dict[str, list[Exposure]]],
) -> None:
    """이번 실행의 노출 키 집합이 판정 라운드와 동일한지 정확 비교한다.

    사이드카에 원본 노출 키 집합이 있으면 커버리지 휴리스틱(draft 전무=관용,
    일부=실패) 대신 이 비교를 쓴다. 노출 집합이 동일하면 판정 없는 노출은
    전부 원본 라운드에서 quarantine된 것이므로 관용해도 은폐가 아니다 —
    `chunk_size > 0`에서 유저의 청크 일부만 격리된 라운드도 리플레이가
    가능해진다(#274). 반대로 집합이 다르면 격리 구간에 국한된 차이까지
    포함해 전부 검출된다.
    """
    current_users = set(exposures_by_user)
    original_users = set(original)
    if current_users != original_users:
        missing = sorted(original_users - current_users)
        extra = sorted(current_users - original_users)
        raise ValueError(
            "replay 노출 유저 집합이 판정 라운드와 다릅니다 — "
            f"판정 라운드에만 {len(missing)}명"
            f"{f' (first: {missing[0]})' if missing else ''}, "
            f"이번 실행에만 {len(extra)}명"
            f"{f' (first: {extra[0]})' if extra else ''}"
        )
    mismatched: list[str] = []
    first_detail = ""
    for user_id, by_policy in exposures_by_user.items():
        current_keys = frozenset(
            exposure.video_id
            for exposures in by_policy.values()
            for exposure in exposures
        )
        if current_keys != original[user_id]:
            if not mismatched:
                only_original = sorted(original[user_id] - current_keys)[:3]
                only_current = sorted(current_keys - original[user_id])[:3]
                first_detail = (
                    f" (first {user_id}: 판정 라운드에만 {only_original}, "
                    f"이번 실행에만 {only_current})"
                )
            mismatched.append(user_id)
    if mismatched:
        raise ValueError(
            f"replay 노출 키 집합이 판정 라운드와 다른 유저가 {len(mismatched)}명 "
            "있습니다" + first_detail
        )


def _policy_exposure_snapshot(
    exposures_by_user: Mapping[str, Mapping[str, Sequence[Exposure]]],
    policy_versions: Mapping[str, str],
) -> dict[str, dict[str, list[dict[str, object]]]]:
    """리플레이용 유저별 정책 노출·계보 스냅샷을 만든다."""

    return {
        user_id: {
            policy: [
                {
                    "video_id": exposure.video_id,
                    "rank": exposure.rank,
                    "ctr_score": exposure.ctr_score,
                    "is_exploration": exposure.is_exploration,
                    "policy_version": policy_versions[policy],
                }
                for exposure in exposures
            ]
            for policy, exposures in by_policy.items()
        }
        for user_id, by_policy in exposures_by_user.items()
    }


def _validate_replay_policy_exposures(
    original: Mapping[
        str, Mapping[str, Sequence[Mapping[str, object]]]
    ],
    exposures_by_user: Mapping[str, Mapping[str, Sequence[Exposure]]],
    policy_versions: Mapping[str, str],
) -> None:
    """정책별 노출 순서와 모든 노출 메타데이터가 같은지 검증한다."""

    current = _policy_exposure_snapshot(exposures_by_user, policy_versions)
    original_by_user = {
        str(user_id): by_policy for user_id, by_policy in original.items()
    }
    if set(current) != set(original_by_user):
        raise ValueError(
            "replay 정책 노출 snapshot의 유저 집합이 판정 라운드와 다릅니다"
        )

    for user_id, current_by_policy in current.items():
        original_by_policy = original_by_user[user_id]
        current_policies = list(current_by_policy)
        original_policies = [str(policy) for policy in original_by_policy]
        if current_policies != original_policies:
            raise ValueError(
                "replay 정책 노출 snapshot의 정책 이름 또는 정책 순서가 "
                f"판정 라운드와 다릅니다 (user={user_id})"
            )
        for policy, current_exposures in current_by_policy.items():
            original_exposures = original_by_policy[policy]
            if len(current_exposures) != len(original_exposures):
                raise ValueError(
                    "replay 정책 노출 snapshot의 노출 수가 판정 라운드와 "
                    f"다릅니다 (user={user_id}, policy={policy})"
                )
            for index, (current_exposure, original_exposure) in enumerate(
                zip(current_exposures, original_exposures, strict=True)
            ):
                if not isinstance(original_exposure, Mapping):
                    raise ValueError(
                        "replay 정책 노출 snapshot 항목에 전체 노출 메타데이터가 "
                        f"없습니다 (user={user_id}, policy={policy}, index={index})"
                    )
                if current_exposure != dict(original_exposure):
                    raise ValueError(
                        "replay 정책 노출 snapshot이 판정 라운드와 다릅니다 "
                        f"(user={user_id}, policy={policy}, index={index})"
                    )


def _parse_policy_exposure_snapshot(
    raw: object,
) -> dict[str, dict[str, tuple[dict[str, object], ...]]] | None:
    """JSON sidecar의 정책 노출 스냅샷을 타입이 보존된 값으로 읽는다.

    Task 2가 쓴 video_id 문자열 전용 스냅샷은 새 엄격 스냅샷으로 취급하지 않고
    None을 반환해 exposure_keys/legacy 커버리지 검증으로 폴백한다.
    """

    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("policy_exposures 메타는 유저별 mapping이어야 합니다")

    parsed: dict[str, dict[str, tuple[dict[str, object], ...]]] = {}
    saw_rich_item = False
    saw_legacy_item = False
    for user_id, by_policy in raw.items():
        if not isinstance(by_policy, Mapping):
            raise ValueError(
                f"policy_exposures[{user_id!r}]는 정책별 mapping이어야 합니다"
            )
        parsed_by_policy: dict[str, tuple[dict[str, object], ...]] = {}
        for policy, exposures in by_policy.items():
            if isinstance(exposures, (str, bytes)) or not isinstance(
                exposures, Sequence
            ):
                raise ValueError(
                    f"policy_exposures[{user_id!r}][{policy!r}]는 노출 목록이어야 합니다"
                )
            parsed_exposures: list[dict[str, object]] = []
            for index, exposure in enumerate(exposures):
                if not isinstance(exposure, Mapping):
                    if isinstance(exposure, str):
                        saw_legacy_item = True
                        continue
                    raise ValueError(
                        "policy_exposures 스냅샷 항목에 video_id, rank, ctr_score, "
                        "is_exploration, policy_version이 필요합니다 "
                        f"(user={user_id}, policy={policy}, index={index})"
                    )
                saw_rich_item = True
                parsed_exposures.append(
                    {str(key): value for key, value in exposure.items()}
                )
            parsed_by_policy[str(policy)] = tuple(parsed_exposures)
        parsed[str(user_id)] = parsed_by_policy
    if saw_rich_item and saw_legacy_item:
        raise ValueError("policy_exposures 메타에 신규·구형 snapshot 형식이 섞였습니다")
    if saw_legacy_item:
        return None
    return parsed


def _validate_unique_event_ids(events: Sequence[EventLog]) -> None:
    """최종 event stream에 중복 event_id가 없음을 보장한다."""

    event_ids = [event.event_id for event in events]
    if len(event_ids) != len(set(event_ids)):
        raise ValueError("정책 라운드 event_id가 중복되었습니다")


def main(
    personas: pd.DataFrame,
    virtual_users: list[dict],
    videos_raw: pd.DataFrame,
    events: pd.DataFrame,
    generator: ActionLogGenerator | None = None,
    reranker: Reranker | None = None,
    *,
    policies: Sequence[PolicySpec] | None = None,
    replay: DraftReplay | None = None,
    k: int = 10,
    exploration_ratio: float = 0.1,
    click_threshold: float,
    seed: int = 42,
    judgment_repeats: int = 1,
    chunk_size: int = 0,
    max_concurrency: int = 1,
    policy_version: str = "local",
    round_id: str | None = None,
    as_of: str = "2026-07-20 00:00:00",
    output_dir: str = "data/generated/policy_round",
    input_paths: Mapping[str, str] | None = None,
    assembly_source: str = "duckdb",
    feature_store: FeatureStore | None = None,
) -> dict:
    """정책 시뮬레이션 라운드를 실행하고 리포트 dict를 반환한다.

    assembly_source: 모델 reranker가 보는 21피처의 출처(#359). "feast"는 학습과 같은
    offline PIT(``build_pool_feature_frame_feast``)를 써 train-serve skew를 없앤다 —
    이때 ``feature_store``(offline 조회 store) 주입이 필요하다. "duckdb"(기본)는 raw
    재계산(``assembly.py``)으로 기존과 동일하며 #359에서 제거 예정이다. 어느 경로든
    baseline 휴리스틱·LLM 후보 provider는 videos_raw(pool 정체)를 그대로 쓴다.
    """
    if (generator is None) == (replay is None):
        raise ValueError(
            "generator와 replay 중 정확히 하나만 지정해야 합니다 "
            "(replay는 저장된 판정을 재사용하므로 generator가 필요 없습니다)"
        )
    if assembly_source not in ("duckdb", "feast"):
        raise ValueError(f"assembly_source must be 'duckdb' or 'feast': {assembly_source!r}")
    if assembly_source == "feast" and feature_store is None:
        raise ValueError(
            "assembly_source='feast'는 feature_store 주입이 필요합니다 "
            "(offline PIT 조회 store) — _cli()가 env로 만들어 주입한다"
        )
    if judgment_repeats < 1:
        raise ValueError("judgment_repeats must be at least 1")

    if policies is None:
        if reranker is None:
            reranker = load_reranker(load_model_settings_from_environment())  # fail-fast
        policy_specs = (
            PolicySpec(name=BASELINE, reranker=None, version=policy_version),
            PolicySpec(name=MODEL, reranker=reranker, version=policy_version),
        )
    else:
        policy_specs = tuple(policies)
        if reranker is not None:
            raise ValueError("policies와 reranker는 함께 지정할 수 없습니다")

    names = [policy.name for policy in policy_specs]
    if len(policy_specs) < 2:
        raise ValueError("policy round requires at least two policies")
    if len(names) != len(set(names)):
        raise ValueError("policy names must be unique (중복 정책 이름 불가)")
    policy_versions = {
        policy.name: policy.version or policy_version for policy in policy_specs
    }
    if replay is not None and replay.round_id is not None and round_id is not None:
        if replay.round_id != round_id:
            raise ValueError(
                "replay round_id가 이번 실행과 다릅니다 "
                f"(판정 라운드={replay.round_id!r}, 이번 실행={round_id!r})"
            )
    if replay is not None and replay.policy_versions is not None:
        if (
            list(replay.policy_versions) != names
            or dict(replay.policy_versions) != policy_versions
        ):
            raise ValueError(
                "replay 정책 이름, 순서 또는 policy version이 판정 라운드와 다릅니다"
            )
    effective_round_id = (
        round_id
        or (replay.round_id if replay and replay.round_id else None)
        or f"round-{uuid.uuid4().hex}"
    )
    validate_policy_name(effective_round_id)

    exposure_args = {
        "seed": seed,
        "k": k,
        "exploration_ratio": exploration_ratio,
        "as_of": as_of,
    }
    # 인자 불일치는 모델 로드·유저별 피처 조립(임베딩 호출 포함) 전에 걸러낸다.
    if replay is not None:
        _validate_replay_exposure_args(replay.exposure_args, exposure_args)

    video_by_id = {str(v["video_id"]): v for v in videos_raw.to_dict("records")}
    candidate_video_ids = list(video_by_id)

    # 모델 reranker 입력(21피처) 조립 경로 선택(#359). 바뀌는 건 모델이 보는 피처의
    # 출처뿐 — feast는 학습과 같은 offline PIT라 skew가 없고, duckdb(기본)는 raw
    # 재계산이다. 두 경로 모두 pool 정체(video_by_id)·baseline 휴리스틱은 공통.
    if assembly_source == "feast":
        def _model_feature_frame(user_id: str) -> pd.DataFrame:
            return build_pool_feature_frame_feast(
                feature_store, user_id, candidate_video_ids, as_of
            )
    else:
        def _model_feature_frame(user_id: str) -> pd.DataFrame:
            return build_pool_feature_frame(personas, events, videos_raw, user_id, as_of)

    # 1) 유저별 모든 정책의 노출 결정 (+ 정책별 스코어링 진단 수집)
    exposures_by_user: dict[str, dict[str, list[Exposure]]] = {}
    unseen_counts: dict[str, int] = {}
    unseen_counts_by_policy: dict[str, dict[str, int]] = {
        policy.name: {} for policy in policy_specs
    }
    skipped_users: list[str] = []
    skipped_users_by_policy: dict[str, list[str]] = {
        policy.name: [] for policy in policy_specs
    }
    model_policy_specs = tuple(
        policy for policy in policy_specs if policy.reranker is not None
    )
    for index, virtual_user in enumerate(virtual_users):
        user_id = str(virtual_user.get("user_id", f"user_{index}"))
        frame: pd.DataFrame | None = None
        if model_policy_specs:
            try:
                # 한 유저의 raw/Feast 피처 조립은 모델 정책 수와 무관하게 한 번만 한다.
                frame = _model_feature_frame(user_id)
            except KeyError:
                if assembly_source == "feast":
                    # feast KeyError는 registry/FeatureService 구성 오류이므로 fail-fast.
                    raise
                skipped_users.append(user_id)
                for policy in model_policy_specs:
                    skipped_users_by_policy[policy.name].append(user_id)
                continue

        by_policy: dict[str, list[Exposure]] = {}
        candidates_by_contract: dict[tuple[str, ...], list[CandidateVideo]] = {}
        user_unseen: dict[str, dict[str, int]] = {}
        failed_policy: str | None = None
        for policy_spec in policy_specs:
            policy = policy_spec.name
            if policy_spec.reranker is None:
                # baseline 이름은 기존 seed 관례를 보존하고, 추가 baseline은 이름으로 격리한다.
                rng_seed = (
                    f"{seed}:{user_id}"
                    if policy == BASELINE
                    else f"{seed}:{policy}:{user_id}"
                )
                baseline_videos = build_candidates(
                    virtual_user,
                    list(video_by_id.values()),
                    k,
                    exploration_ratio,
                    random.Random(rng_seed),
                )
                by_policy[policy] = [
                    Exposure(
                        video_id=str(video["video_id"]),
                        rank=rank,
                        ctr_score=None,
                        is_exploration=None,
                    )
                    for rank, video in enumerate(baseline_videos, start=1)
                ]
                continue

            assert frame is not None
            reranker_for_policy = policy_spec.reranker
            try:
                candidates = candidates_by_contract.get(
                    reranker_for_policy.feature_columns
                )
                if candidates is None:
                    candidates = _to_candidate_videos(
                        frame, reranker_for_policy.feature_columns
                    )
                    candidates_by_contract[
                        reranker_for_policy.feature_columns
                    ] = candidates
                outcome = reranker_for_policy.rerank_with_diagnostics(candidates)
                by_policy[policy] = select_exposures(
                    outcome.items,
                    k,
                    exploration_ratio,
                    random.Random(f"{seed}:{policy}:{user_id}"),
                )
            except KeyError:
                if assembly_source == "feast":
                    raise
                failed_policy = policy
                break
            except (MissingFeatureColumnsError, PredictionError):
                failed_policy = policy
                break
            user_unseen[policy] = {
                column: len(values)
                for column, values in outcome.unseen_categories.items()
            }

        if failed_policy is not None:
            skipped_users.append(user_id)
            skipped_users_by_policy[failed_policy].append(user_id)
            continue

        exposures_by_user[user_id] = by_policy
        for policy, counts in user_unseen.items():
            policy_counts = unseen_counts_by_policy[policy]
            for column, count in counts.items():
                policy_counts[column] = policy_counts.get(column, 0) + count
                unseen_counts[column] = unseen_counts.get(column, 0) + count

    # 2) 판정 확보 — 신규 라운드는 LLM 1회, 리플레이는 저장된 판정 재사용
    request = EventGenerationRequest(
        click_threshold=click_threshold,
        candidates_per_user=max(1, len(policy_specs) * k),
        seed=seed,
        chunk_size=chunk_size,
        max_concurrency=max_concurrency,
        output_path=str(Path(output_dir) / "event_log.parquet"),
        warehouse_output_path=str(Path(output_dir) / "event_log.jsonl"),
        quarantine_output_path=str(Path(output_dir) / "event_log_quarantine.jsonl"),
    )
    if replay is None:
        assert generator is not None  # 위 XOR 검증이 보장한다
        union_by_user: dict[str, list[dict]] = {}
        for user_id, by_policy in exposures_by_user.items():
            seen: set[str] = set()
            union: list[dict] = []
            for policy_spec in policy_specs:
                for exposure in by_policy[policy_spec.name]:
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
            exposure_keys={
                user_id: [str(v["video_id"]) for v in union]
                for user_id, union in union_by_user.items()
            },
            policy_versions=policy_versions,
            policy_exposures=_policy_exposure_snapshot(
                exposures_by_user, policy_versions
            ),
            policy_version=policy_version,
            round_id=effective_round_id,
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
        # 아래 quarantine 관용 규칙의 전제를 먼저 검사한다. 판정이 있는 유저는
        # 전부 이번 노출에도 나타나야 한다 — 그렇지 않으면 유저 집합 자체가
        # 다른 것이고, 관용 규칙이 그 전량을 "원본 quarantine"으로 오인해
        # impressions=0·CTR=0 리포트를 에러 없이 만들어낸다.
        absent_judged_users = sorted({d.user_id for d in drafts} - set(exposures_by_user))
        if absent_judged_users:
            raise ValueError(
                f"replay drafts에 판정이 있는 유저 {len(absent_judged_users)}명이 이번 "
                f"노출에 없습니다 (first: {absent_judged_users[0]}) — virtual users가 "
                "판정 라운드와 다릅니다"
            )

        if replay.policy_exposures is not None:
            _validate_replay_policy_exposures(
                replay.policy_exposures,
                exposures_by_user,
                policy_versions,
            )
        elif replay.exposure_keys is not None:
            # 사이드카에 원본 노출 키 집합이 있으면 정확 비교한다. 통과하면
            # 판정 없는 노출은 전부 원본 quarantine(chunk 부분 격리 포함)이므로
            # 아래 4단계에서 dropped_exposures_without_judgment로 계수만 한다.
            _validate_replay_exposure_keys(replay.exposure_keys, exposures_by_user)
        else:
            # 구버전 사이드카(exposure_keys 없음) 폴백: 커버리지를 유저(슬레이트)
            # 단위 휴리스틱으로 검사한다. draft가 하나도 없는 유저는 원본 판정
            # 라운드에서 quarantine된 유저이므로(그 유저의 draft는 parquet에 아예
            # 없다) 관용하고 dropped로 계수하며, draft가 일부만 있는 유저는 노출
            # 집합이 어긋났다는 신호로 보고 실패한다. 이 휴리스틱은
            # chunk_size > 0의 부분 격리 라운드를 리플레이하지 못한다(#274) —
            # 신규 덤프는 exposure_keys를 항상 기록하므로 위 정확 비교를 탄다.
            partially_covered_users: list[str] = []
            for user_id, both in exposures_by_user.items():
                exposure_keys = {
                    (user_id, exposure.video_id)
                    for policy_exposures in both.values()
                    for exposure in policy_exposures
                }
                covered = sum(1 for key in exposure_keys if key in draft_by_key)
                if 0 < covered < len(exposure_keys):
                    partially_covered_users.append(user_id)
            if partially_covered_users:
                raise ValueError(
                    f"replay drafts partially cover {len(partially_covered_users)} user "
                    f"slate(s) (first: {partially_covered_users[0]}) — 판정이 하나도 없는 "
                    "유저는 원본 quarantine으로 간주해 관용하지만, 일부만 있는 유저는 "
                    "노출 집합(virtual users 등)이 판정 라운드와 다르다는 신호입니다"
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
    for policy_index, policy_spec in enumerate(policy_specs):
        policy = policy_spec.name
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
                    policy=policy,
                    rank=exposure.rank,
                    ctr_score=exposure.ctr_score,
                    is_exploration=exposure.is_exploration,
                    policy_version=policy_versions[policy],
                )
                if exposure.is_exploration:
                    exploration_imps += 1
                    if (user_id, exposure.video_id) in clicked_keys:
                        exploration_clicks += 1
        clicked_indices = {
            i for i, d in enumerate(policy_drafts) if (d.user_id, d.video_id) in clicked_keys
        }
        policy_request = request.model_copy(
            update={"seed": seed + policy_index * 1000}
        )
        events_out = _expand_events(
            policy_drafts, clicked_indices, policy_request,
            metadata=metadata,
            source=SOURCE_ONLINE_SIMULATED,
            event_id_prefix=build_round_policy_event_id_prefix(effective_round_id, policy),
        )
        all_events.extend(events_out)
        impressions = len(policy_drafts)
        clicks = len(clicked_indices)
        per_policy[policy] = {
            "policy_version": policy_versions[policy],
            "impressions": impressions,
            "clicks": clicks,
            "ctr": round(clicks / impressions, 4) if impressions else 0.0,
            "mean_click_propensity": (
                round(sum(propensities) / len(propensities), 4) if propensities else 0.0
            ),
            "exploration_impressions": exploration_imps,
            "exploration_clicks": exploration_clicks,
        }

    _validate_unique_event_ids(all_events)

    # 5) 모든 정책 쌍의 유저별 Jaccard 평균. "|"는 정책 이름에 허용되지 않아
    # pair key를 모호하지 않게 직렬화한다.
    overlap_by_pair: dict[str, float] = {}
    all_jaccards: list[float] = []
    for left, right in combinations(names, 2):
        pair_jaccards: list[float] = []
        for by_policy in exposures_by_user.values():
            left_ids = {exposure.video_id for exposure in by_policy[left]}
            right_ids = {exposure.video_id for exposure in by_policy[right]}
            if left_ids | right_ids:
                pair_jaccards.append(
                    len(left_ids & right_ids) / len(left_ids | right_ids)
                )
        overlap_by_pair[f"{left}|{right}"] = (
            round(sum(pair_jaccards) / len(pair_jaccards), 4)
            if pair_jaccards
            else 0.0
        )
        all_jaccards.extend(pair_jaccards)
    overlap = (
        round(sum(all_jaccards) / len(all_jaccards), 4)
        if all_jaccards
        else 0.0
    )

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
        "policy_versions": policy_versions,
        "round_id": effective_round_id,
        "replay": replay is not None,
        "llm_model": llm_model,
        "k": k,
        "exploration_ratio": exploration_ratio,
        "click_threshold": click_threshold,
        "seed": seed,
        "users": len(exposures_by_user),
        "skipped_users": skipped_users,
        "skipped_users_by_policy": skipped_users_by_policy,
        "dropped_exposures_without_judgment": dropped,
        "policies": per_policy,
        "overlap_jaccard_mean": overlap,
        "overlap_jaccard_by_pair": overlap_by_pair,
        "unseen_category_counts": unseen_counts,
        "unseen_category_counts_by_policy": unseen_counts_by_policy,
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
    parser.add_argument("--videos", required=True, help="사전 파싱된 videos.csv 경로")
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
    parser.add_argument(
        "--assembly-source", choices=["duckdb", "feast"], default="duckdb",
        help="모델 피처 조립 경로. feast=학습과 같은 offline PIT(#359, 권장), "
             "duckdb=raw 재계산(기존, #359에서 제거 예정). feast는 GCS_REGISTRY_PATH/"
             "GCS_STAGING_LOCATION env를 요구한다",
    )
    parser.add_argument("--log-mlflow", action="store_true")
    args = parser.parse_args()
    if args.replay_drafts is not None and args.generator is not None:
        parser.error("--generator는 --replay-drafts와 함께 쓸 수 없습니다 (저장된 판정을 재사용합니다)")

    # feast 경로: 학습(_assemble_via_feast)과 동일한 offline 전용 store를 env로 구성해
    # 주입한다(prod feature_store.yaml/Redis 불필요, registry는 배포 job이 apply한 GCS).
    feature_store = None
    if args.assembly_source == "feast":
        import atexit
        import shutil
        import tempfile

        from src.features.feast_retrieval import build_offline_feature_store
        from src.pipeline.build_training_dataset import (
            BIGQUERY_DATASET,
            BIGQUERY_PROJECT,
        )

        try:
            registry_path = os.environ["GCS_REGISTRY_PATH"]
            gcs_staging = os.environ["GCS_STAGING_LOCATION"]
        except KeyError as exc:
            parser.error(
                f"--assembly-source feast는 환경변수 {exc}가 필요합니다 "
                "(offline 레지스트리·GCS staging 경로)"
            )

        # offline 전용 store라 sqlite online.db는 실제로 안 쓰이지만 RepoConfig가 경로를
        # 요구한다. 반복·로컬 실행에서 임시 디렉토리가 쌓이지 않도록 종료 시 정리한다.
        store_dir = tempfile.mkdtemp(prefix="feast_sim_")
        atexit.register(shutil.rmtree, store_dir, ignore_errors=True)
        feature_store = build_offline_feature_store(
            registry_path,
            project=BIGQUERY_PROJECT,
            dataset=BIGQUERY_DATASET,
            gcs_staging=gcs_staging,
            online_db_path=os.path.join(store_dir, "online.db"),
        )

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
        raw_exposure_keys = meta.get("exposure_keys")  # 구버전 사이드카에는 없다
        raw_policy_exposures = meta.get("policy_exposures")
        raw_policy_versions = meta.get("policy_versions")
        if raw_policy_versions is not None and not isinstance(
            raw_policy_versions, Mapping
        ):
            raise ValueError("policy_versions 메타는 정책별 mapping이어야 합니다")
        replay = DraftReplay(
            drafts=read_action_log_draft_parquet(args.replay_drafts),
            llm_model=str(meta["llm_model"]),
            exposure_args=meta_exposure_args,
            exposure_keys=(
                {
                    str(user): frozenset(str(video) for video in videos)
                    for user, videos in raw_exposure_keys.items()
                }
                if raw_exposure_keys is not None
                else None
            ),
            round_id=(str(meta["round_id"]) if meta.get("round_id") is not None else None),
            policy_versions=(
                {
                    str(policy): str(version)
                    for policy, version in raw_policy_versions.items()
                }
                if raw_policy_versions is not None
                else None
            ),
            policy_exposures=_parse_policy_exposure_snapshot(
                raw_policy_exposures
            ),
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
        assembly_source=args.assembly_source,
        feature_store=feature_store,
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
                    "replay": report["replay"],
                    "llm_model": report["llm_model"],
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
