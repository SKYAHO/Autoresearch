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

if TYPE_CHECKING:
    from feast import FeatureStore

logger = logging.getLogger(__name__)

DEFAULT_SERVICE = "ctr_training_v1"
# 1차 video PIT로 확정할 피처(similarity 조인 키인 category_id).
_VIDEO_CATEGORY_FEATURE = "VideoFeatureView:category_id"
_JOIN_KEYS = ["video_id", "event_timestamp"]


def retrieve_training_features(
    store: "FeatureStore",
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

    NOTE: ttl 밖 엔티티는 Feast가 결과에서 제외하므로(행 드롭, NaN 아님), 반환 행 수가
    spine보다 적을 수 있다. 손실이 있으면 경고를 남긴다 — 호출부가 학습 데이터 결손으로
    인지하도록.
    """
    # Stage 1: (video_id, ts)별 category_id를 video PIT로 확정.
    video_keys = spine[_JOIN_KEYS].drop_duplicates()
    video_category = store.get_historical_features(
        entity_df=video_keys,
        features=[video_category_feature],
    ).to_df()
    entity_df = spine.merge(
        video_category[[*_JOIN_KEYS, "category_id"]], on=_JOIN_KEYS, how="left"
    )

    # Stage 2: category_id가 채워진 entity_df로 전체 FeatureService 조회.
    result = store.get_historical_features(
        entity_df=entity_df, features=store.get_feature_service(service)
    ).to_df()

    if len(result) < len(spine):
        logger.warning(
            "feast 조회에서 spine %d행 중 %d행이 빠짐(ttl 밖 등) — 학습 데이터 결손",
            len(spine),
            len(spine) - len(result),
        )
    return result
