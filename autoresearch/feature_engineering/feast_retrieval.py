"""학습·서빙 공용 Feast PIT 조회 경로 (#358 학습, #359 서빙 정합, [EPIC] #299 Phase 2/3).

[파이프라인] 피처 구간 — 소비자가 **같은 offline PIT 조회**로 21피처를 얻어 skew를
없앤다: ① ``build_training_dataset``의 학습 조립이 spine(``training_entity``)에 붙이는
경로, ② **시뮬레이션 — 유저 단건** pool 조립(``build_pool_feature_frame_feast``), ③ **일일추천 —
전 유저 × 공통 pool 1회 배치** 조립(``build_pool_feature_frames_feast``, #359 C1: 유저마다
staged 2회 = ~2N BQ 잡 회피). 셋 다 Feast ``get_historical_features``(point-in-time)를 쓰고
offline store가 정본(#357)이라 그 값을 그대로 읽는다. DuckDB 재계산 경로(``assembly.py``)와
병존하며 #359에서 제거된다.

[학습 vs 서빙 후처리] 결손 처리가 갈린다: 학습은 ``drop_user_dynamic_gap_rows``로
활동 유저의 결손을 드러내 드롭((C) 결손 가시화)하지만, 서빙(pool 채점)은 콜드 유저·미발견
영상도 반드시 채점 대상이라 ``apply_cold_start_defaults``만 적용하고 드롭하지 않는다.
컬럼 계약도 갈린다: 학습 조립만 ``require_extra_feature_columns``로 실험 피처(#454)의
존재를 확인하며, 서빙 pool 프레임(``_finalize_pool_frame``)은 prod 21피처 계약 그대로다.

[staged 조회] ``topic_similarity``는 (user, **영상 category_id**) 키라 닭-달걀이다
(#357 (B)). 1차로 video PIT로 category_id를 확정해 entity_df에 붙이고, 2차로 전체
FeatureService를 조회한다.

[비책임] FeatureView/Service/ODFV 정의는 ``feature_repo``가, DuckDB 재계산은
``assembly.py``가, spine 적재는 ``feature_store_build``가 소유한다. Feast import는 이
경로가 실제로 선택될 때만 필요하므로, 이 모듈을 쓰는 쪽(feast 격리 이미지)에서만 로드한다.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pandas as pd

from autoresearch.feature_engineering.model_contract import (
    CATEGORICAL_FEATURE_COLUMNS,
    COLD_START_CATEGORICAL_DEFAULT,
    MODEL_FEATURE_COLUMNS,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from feast import FeatureStore

logger = logging.getLogger(__name__)

DEFAULT_SERVICE = "ctr_training_v1"
# 1차 video PIT로 확정할 피처(영상의 category_id).
_VIDEO_CATEGORY_FEATURE = "VideoFeatureView:category_id"
_JOIN_KEYS = ["video_id", "event_timestamp"]
# similarity(UserCategorySimilarityView) 조인 키. category_id(모델 피처)와 이름이
# 겹치면 offline SQL이 ambiguous로 죽으므로 조인 배관은 category_key로 구분한다(#358).
_CATEGORY_JOIN_KEY = "category_key"

# UserDynamicView가 제공하는 동적 피처. 이 전부가 null이면 그 (user, ts) 스냅샷이
# 없다는 뜻 = ttl 초과/파이프라인 결손(#365)이다. drop_user_dynamic_gap_rows 참고.
_USER_DYNAMIC_COLUMNS = (
    "recent_click_count_7d",
    "recent_view_count_7d",
    "recent_watch_time_7d",
    "recent_like_count_7d",
    "historical_category_affinity",
    "total_event_count_7d",
)


def retrieve_training_features(
    store: FeatureStore,
    spine: pd.DataFrame,
    *,
    service: str = DEFAULT_SERVICE,
    video_category_feature: str = _VIDEO_CATEGORY_FEATURE,
) -> pd.DataFrame:
    """spine에 21피처를 PIT 조회로 붙인다(staged: video category → 전체 service).

    Args:
        store: Feast FeatureStore.
        spine: 최소 ``user_id, video_id, event_timestamp``를 가진 entity dataframe.
            ``clicked`` 등 추가 컬럼은 Feast가 passthrough로 결과에 실어 준다.
        service: 조회할 FeatureService 이름(기본 ctr_training_v1).
        video_category_feature: 1차로 확정할 category 피처 참조.

    Returns:
        21피처 + entity 키 + spine passthrough 컬럼을 가진 DataFrame.

    NOTE: ttl 밖/미발견 엔티티의 처리는 **offline store에 따라 다르다** — File store는 행을
    드롭하지만, **BigQuery는 행을 보존하고 피처를 NULL로 채운다**(실측: spine 대비 손실 0,
    영상 미발견 3.2%가 NULL 행으로 돌아옴). 따라서 아래 행 수 가드는 File 기준 보조 신호이고,
    실질 결손(UserDynamic ttl 초과 등)은 드롭이 아니라 **NULL로 드러난다** — 그 NULL의
    후처리(cold-start)는 `apply_cold_start_defaults`가 담당한다.
    """
    # Stage 1: (video_id, ts)별 영상 category를 video PIT로 확정.
    video_keys = spine[_JOIN_KEYS].drop_duplicates()
    video_category = store.get_historical_features(
        entity_df=video_keys,
        features=[video_category_feature],
    ).to_df()
    # 그 category를 similarity 조인 키 category_key로 붙인다. entity_df에는 category_id를
    # 두지 않는다 — stage 2가 VideoFeatureView:category_id(피처)를 다시 조회하므로,
    # 같은 이름 컬럼이 entity_df에 있으면 ambiguous가 재발한다.
    video_category = video_category.rename(columns={"category_id": _CATEGORY_JOIN_KEY})
    entity_df = spine.merge(
        video_category[[*_JOIN_KEYS, _CATEGORY_JOIN_KEY]], on=_JOIN_KEYS, how="left"
    )

    # stage 1↔2 배관 건전성: category_key 결측률. BigQuery는 entity_df를 임시테이블로
    # 올렸다 읽으므로 event_timestamp 값·tz·해상도가 어긋나면 이 merge가 전 행 NaN이 되어
    # topic_similarity가 통째로 null→cold-start로 조용히 0이 된다(행 수는 그대로라 손실
    # 경고도 안 뜬다). 영상 미발견(정상)과 합산이라 분리는 못 하지만, 비정상적으로 높은
    # 결측률은 merge 어긋남 신호다.
    cat_null_rate = float(entity_df[_CATEGORY_JOIN_KEY].isna().mean()) if len(entity_df) else 0.0
    if cat_null_rate:
        logger.warning(
            "category_key 결측률 %.4f (video 미발견 + stage1↔2 merge 어긋남 합산) — "
            "비정상적으로 높으면 stage1/spine event_timestamp 정합 확인",
            cat_null_rate,
        )

    # Stage 2: category_key가 채워진 entity_df로 전체 FeatureService 조회.
    result = store.get_historical_features(
        entity_df=entity_df, features=store.get_feature_service(service)
    ).to_df()

    if len(result) < len(spine):
        logger.warning(
            "feast 조회에서 spine %d행 중 %d행이 빠짐(File store 등 드롭) — 학습 데이터 결손",
            len(spine),
            len(spine) - len(result),
        )
    return result


def require_extra_feature_columns(
    features: pd.DataFrame, extra: Sequence[str], *, service: str
) -> None:
    """조회 결과에 선언한 실험 피처가 모두 있는지 확인한다(#454, 학습 조립 전용).

    ``_require_model_features``가 prod 계약 21피처에 대해 하는 일을 실험 피처에 대해
    한다. 호출부(``_assemble_via_feast``)가 **CSV를 쓰기 전에** 부르므로, 선언한 컬럼이
    없으면 잘린 데이터셋이 만들어지지 않는다 — 그대로 저장하면 학습의
    ``--extra-features`` 승격 단계에서야 실패해 조립 비용이 통째로 버려진다.

    실패 메시지에 FeatureService 이름을 담는다. 컬럼이 없는 가장 흔한 원인이
    "그 피처를 가진 서비스가 아니라 기본 서비스로 조회했다"이기 때문이다.

    Args:
        features: PIT 조회 결과.
        extra: 이 실행이 보존을 선언한 실험 피처 이름.
        service: 실제로 조회에 쓴 FeatureService 이름.

    Raises:
        ValueError: 선언한 컬럼이 조회 결과에 하나라도 없으면.
    """
    missing = [column for column in extra if column not in features.columns]
    if missing:
        raise ValueError(
            f"FeatureService {service!r} 조회 결과에 선언한 실험 피처가 없습니다: {missing}. "
            "해당 피처를 제공하는 FeatureService를 --feature-service로 지정했는지 "
            "확인하세요(ODFV 포함 여부는 feature_repo가 정본)."
        )


def drop_user_dynamic_gap_rows(features: pd.DataFrame) -> pd.DataFrame:
    """UserDynamic 피처가 전부 null인 행을 드롭한다(#358, #357 (C) 결손 가시화 관철).

    영상 미발견 cold-start(``apply_cold_start_defaults``가 0/unknown으로 채움)와 **다르게**
    처리한다. UserDynamic 전체 null은 "정보가 원래 없는" 게 아니라, **실제로 활동한 유저**
    (라벨 clicked이 그 활동 유저 것)의 기록을 파이프라인 결손(#365, 60h ttl 초과)으로 못
    가져온 것이다. 이를 0/unknown으로 채우면 "이력 없는 신규 유저"라는 거짓을 학습에
    주입하는 셈 — ttl 없던 시절 stale이 몰래 섞이던 것과 같은 피해를 '0'이라는 값으로
    재현한다. (C)가 ttl로 "결손을 null로 드러내기로" 한 의도를 학습 단계까지 지키려면,
    채우지 말고 **그 행을 학습에서 제외**해야 한다.

    드롭 건수를 별도로 경고에 남긴다 — good days엔 0이지만, #365가 gap을 만들면 이
    숫자가 비정상적으로 커져 바로 드러나도록.
    """
    cols = [c for c in _USER_DYNAMIC_COLUMNS if c in features.columns]
    if not cols:
        return features
    gap = features[cols].isna().all(axis=1)
    n_gap = int(gap.sum())
    if n_gap:
        logger.warning(
            "UserDynamic 결손(전 피처 null)으로 %d/%d 행 드롭 — #365 gap 신호 "
            "(영상 cold-start와 구분: 이건 활동 유저의 기록 유실이라 0으로 안 채운다)",
            n_gap,
            len(features),
        )
    return features[~gap]


def apply_cold_start_defaults(features: pd.DataFrame) -> pd.DataFrame:
    """조회 결과의 null 모델 피처를 **서빙과 같은** cold-start 기본값으로 채운다(#358).

    PIT 조회는 영상이 offline video_feature에 없으면(스냅샷 부재) 그 행의 영상 피처를
    null로 준다. null category_id 등은 학습(categorical 인코딩)을 깨므로, 서빙
    (``online_features``)이 쓰는 것과 동일한 규칙(카테고리→'unknown', 수치→0)으로
    채운다. 기본값 상수·카테고리 분류를 서빙/계약과 공유해 skew를 막는다(복제 금지).
    """
    # 대형 프레임(1.77M) 중복 상주를 줄이려 컬럼 단위로 제자리 채운다(#358 리뷰 OOM).
    for column in MODEL_FEATURE_COLUMNS:
        if column not in features.columns:
            continue
        default = (
            COLD_START_CATEGORICAL_DEFAULT
            if column in CATEGORICAL_FEATURE_COLUMNS
            else 0
        )
        features[column] = features[column].fillna(default)
    return features


def _normalize_as_of(as_of: str) -> pd.Timestamp:
    """as_of(UTC wall-clock 문자열)를 tz-aware UTC로 정규화한다 — 학습 spine과 PIT 경계 정렬."""
    timestamp = pd.Timestamp(as_of)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


def _require_model_features(features: pd.DataFrame) -> None:
    """조회 결과에 21개 모델 피처가 모두 있는지 확인(없으면 조용히 넘기지 않고 실패)."""
    missing = [c for c in MODEL_FEATURE_COLUMNS if c not in features.columns]
    if missing:
        raise ValueError(f"feast 조회 결과에 누락된 모델 피처: {missing}")


def _finalize_pool_frame(
    features: pd.DataFrame, video_ids: list[str], diagnostics: dict | None
) -> pd.DataFrame:
    """한 유저의 조회 결과를 서빙 계약 프레임으로 마감한다(단일·배치 경로 공용).

    후보 순서로 reindex해 (a) 반환 순서를 결정론으로 고정하고(시뮬 재현성·replay 정합 —
    get_historical_features는 ORDER BY가 없어 조회 순서가 흔들리면 exploration·tie-break가
    달라진다), (b) File store가 드롭했거나 offline에 없는 영상을 NaN 행으로 복원해 cold-start
    대상으로 되돌린다(후보가 순위에서 조용히 빠지지 않음, 설계결정 3). cold-start로 채우기
    **전에** 결손을 diagnostics로 남긴다 — 채운 뒤엔 안 보인다.
    """
    features = (
        features.drop_duplicates(subset="video_id")
        .set_index("video_id")
        .reindex(video_ids)
        .reset_index()
    )
    if diagnostics is not None:
        dyn_cols = [c for c in _USER_DYNAMIC_COLUMNS if c in features.columns]
        diagnostics["user_dynamic_cold"] = (
            bool(features[dyn_cols].isna().all(axis=None)) if dyn_cols else False
        )
        diagnostics["video_missing"] = (
            int(features["category_id"].isna().sum()) if "category_id" in features.columns else 0
        )
        diagnostics["pool_size"] = int(len(features))
    features = apply_cold_start_defaults(features)  # 서빙 후처리: cold-start만(드롭 금지)
    return features[["video_id", *MODEL_FEATURE_COLUMNS]]


def build_pool_feature_frame_feast(
    store: FeatureStore,
    user_id: str,
    candidate_video_ids: Sequence[str],
    as_of: str,
    *,
    service: str = DEFAULT_SERVICE,
    diagnostics: dict | None = None,
) -> pd.DataFrame:
    """유저 1명 × 영상 pool의 21피처 채점 프레임을 학습과 **동일한** offline PIT로 만든다(#359).

    시뮬레이션(``simulate_policy_round``)·일일추천(``daily_recommendations``)의 reranker
    입력 조립이 raw DuckDB 재계산(``assembly.py``) 대신 이 경로를 쓰게 해, 학습(같은
    ``retrieve_training_features``)과 값이 어긋나지 않게 한다.

    Args:
        store: Feast FeatureStore(offline 조회 전용, ``build_offline_feature_store``).
        user_id: 채점 대상 유저.
        candidate_video_ids: 채점할 영상 pool의 video_id들(각 1행).
        as_of: PIT 기준 시각. UTC wall-clock 문자열(예: ``"2026-07-20 00:00:00"``)로
            받아 tz-aware UTC로 정규화한다 — 학습 spine(``training_entity``)의
            event_timestamp가 tz-aware UTC라 그와 정렬해야 PIT 경계가 어긋나지 않는다.
        service: 조회할 FeatureService 이름(기본 ``ctr_training_v1``).
        diagnostics: 주어지면(dict) cold-start **채우기 전** 결손 신호를 기록한다 — 서빙은
            드롭 대신 채우므로(설계결정 3) 채운 뒤엔 결손이 안 보인다. 호출부(일일추천 등)가
            개인화 결손을 관측·집계하도록 ``user_dynamic_cold``(이 유저의 UserDynamic 전 피처
            null 여부), ``video_missing``(영상 미발견 = category_id null 행 수), ``pool_size``를 채운다.

    Returns:
        ``video_id`` + 21개 모델 피처를 가진 DataFrame. 행은 ``candidate_video_ids``와
        **정확히 같은 순서·같은 개수**(영상당 1행) — 아래 reindex가 store 구현과 무관하게 보장한다.

    NOTE: 후처리는 서빙 규칙 — ``apply_cold_start_defaults``**만** 적용하고
    ``drop_user_dynamic_gap_rows``는 쓰지 않는다. 콜드 유저·미발견 영상의 후보를 드롭하면
    그 영상이 순위에서 조용히 빠지므로, online 서빙과 같은 규칙(카테고리→'unknown',
    수치→0)으로 채워 전 후보를 채점 대상으로 남긴다(모듈 docstring [학습 vs 서빙 후처리]).
    """
    video_ids = [str(video_id) for video_id in candidate_video_ids]
    if not video_ids:
        # 빈 pool은 BigQuery에 빈 entity_df를 올리지 않고 즉시 빈 계약 프레임을 돌려준다.
        return pd.DataFrame(columns=["video_id", *MODEL_FEATURE_COLUMNS])

    spine = pd.DataFrame(
        {"user_id": str(user_id), "video_id": video_ids, "event_timestamp": _normalize_as_of(as_of)}
    )
    features = retrieve_training_features(store, spine, service=service)
    _require_model_features(features)
    return _finalize_pool_frame(features, video_ids, diagnostics)


def build_pool_feature_frames_feast(
    store: FeatureStore,
    user_ids: Sequence[str],
    candidate_video_ids: Sequence[str],
    as_of: str,
    *,
    service: str = DEFAULT_SERVICE,
    diagnostics: dict | None = None,
) -> dict[str, pd.DataFrame]:
    """여러 유저 × **공통** 영상 pool을 offline PIT **1회 조회**로 조립한다(#359 C1, 일일추천용).

    daily처럼 pool이 전 유저 공통일 때, 유저마다 ``build_pool_feature_frame_feast``를 부르면
    staged 2회 × N = 하루 ~2N BQ 잡(각 entity_df 업로드)이 된다. 여기서는 (유저 × 후보) 곱집합
    spine 1회로 접어 조회를 1회(staged 2단)로 줄인다(PR #381 리뷰 4). 부수로 registry/service
    불일치 같은 전 유저 공통 오류가 유저별 격리로 위장되지 않고 이 한 번의 조회에서 fail-fast한다.

    Returns:
        ``{user_id: DataFrame}`` — 각 프레임은 ``candidate_video_ids``와 같은 순서·개수(영상당 1행).

    diagnostics: 주어지면 **집계**를 채운다 — ``cold_start_users``(UserDynamic 전량 결손 유저 수),
        ``video_missing``(전체 미발견 영상 행 수), ``pool_total``(전체 행 수). 후처리 규칙은
        ``build_pool_feature_frame_feast``와 동일(cold-start만, 드롭 없음).
    """
    video_ids = [str(video_id) for video_id in candidate_video_ids]
    users = [str(user_id) for user_id in user_ids]
    if not video_ids or not users:
        return {u: pd.DataFrame(columns=["video_id", *MODEL_FEATURE_COLUMNS]) for u in users}

    spine = pd.DataFrame(
        [(u, v) for u in users for v in video_ids], columns=["user_id", "video_id"]
    )
    spine["event_timestamp"] = _normalize_as_of(as_of)
    features = retrieve_training_features(store, spine, service=service)
    _require_model_features(features)

    by_user = {str(u): sub for u, sub in features.groupby("user_id", sort=False)}
    empty = features.iloc[0:0]
    frames: dict[str, pd.DataFrame] = {}
    agg = {"cold_start_users": 0, "video_missing": 0, "pool_total": 0}
    for user in users:
        per: dict | None = {} if diagnostics is not None else None
        frames[user] = _finalize_pool_frame(by_user.get(user, empty), video_ids, per)
        if per is not None:
            agg["cold_start_users"] += int(bool(per.get("user_dynamic_cold")))
            agg["video_missing"] += int(per.get("video_missing", 0))
            agg["pool_total"] += int(per.get("pool_size", 0))
    if diagnostics is not None:
        diagnostics.update(agg)
    return frames


def build_offline_feature_store(
    registry_path: str,
    *,
    project: str,
    dataset: str,
    gcs_staging: str,
    online_db_path: str,
):
    """offline 조회 전용 FeatureStore를 코드로 구성한다(#358).

    prod ``feature_store.yaml``(Redis online store + registry)을 로드하지 않는다 — 학습
    조립은 ``get_historical_features``(offline=BigQuery)만 쓰므로 Redis가 필요 없고, online
    store를 sqlite 더미로 둔다. 덕분에 offline 학습 잡이 REDIS_* / redis 어댑터에 의존하지
    않으며, 검증 스크립트가 실제로 돌린 것과 동일한 store 구성이 정본 경로가 된다.

    ``registry_path``는 배포 apply job(#346)이 갱신하는 레지스트리(GCS)를 가리킨다 —
    prod/dev 환경 분리(#399)에서 배포가 환경별 경로(prod/dev)를 주입한다. Feast
    ``project`` 네임스페이스는 registry 경로가 이미 환경을 가르므로 prod/dev 공통이다.
    ``project`` 인자는 offline BigQuery의 GCP project_id 이며 네임스페이스와는 별개다.
    """
    from feast import FeatureStore, RepoConfig
    from feast.repo_config import RegistryConfig

    return FeatureStore(
        config=RepoConfig(
            project="autoresearch_feature_store",
            provider="gcp",
            registry=RegistryConfig(path=registry_path),
            offline_store={
                "type": "bigquery",
                "dataset": dataset,
                "project_id": project,
                "gcs_staging_location": gcs_staging,
            },
            online_store={"type": "sqlite", "path": online_db_path},
            entity_key_serialization_version=3,
        )
    )
