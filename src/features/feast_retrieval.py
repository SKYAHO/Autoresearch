"""학습 데이터셋 조립의 Feast PIT 조회 경로 (#358, [EPIC] #299 Phase 2).

[파이프라인] 피처 구간 — ``build_training_dataset``의 ``--assembly-source feast`` 경로가
spine(``training_entity``)에 21피처를 Feast ``get_historical_features``(point-in-time)로
붙이는 조회 로직을 담당한다. DuckDB 재계산 경로(``assembly.py``)와 병존하며(#359에서
DuckDB 제거), offline store가 정본(#357)이므로 그 값을 그대로 읽는다.

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

from src.features.model_contract import (
    CATEGORICAL_FEATURE_COLUMNS,
    COLD_START_CATEGORICAL_DEFAULT,
    MODEL_FEATURE_COLUMNS,
)

if TYPE_CHECKING:
    from feast import FeatureStore

logger = logging.getLogger(__name__)

DEFAULT_SERVICE = "ctr_training_v1"
# 1차 video PIT로 확정할 피처(영상의 category_id).
_VIDEO_CATEGORY_FEATURE = "VideoFeatureView:category_id"
_JOIN_KEYS = ["video_id", "event_timestamp"]
# similarity(UserCategorySimilarityView) 조인 키. category_id(모델 피처)와 이름이
# 겹치면 offline SQL이 ambiguous로 죽으므로 조인 배관은 category_key로 구분한다(#358).
_CATEGORY_JOIN_KEY = "category_key"


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

    ``registry_path``는 배포 apply job(#346)이 갱신하는 prod 레지스트리(GCS)를 가리킨다.
    ``project``는 Feast 프로젝트명(feature_store.yaml과 동일, 변경 시 registry 분리).
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
