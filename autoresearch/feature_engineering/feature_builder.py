"""Interaction Feature 계산 함수들.

[파이프라인] 피처 구간 — (유저, 영상) 쌍에서만 정해지는 파생 피처의 **계산 본체**를
담당한다. 세 소비자가 이 한 벌을 공유해 Training-Serving Skew를 막는다: ① 학습·시뮬의
Feast ODFV(``category_match_view``) 변환, ② 서빙 online 조회 후처리
(``src/serving/online_features.py``), ③ DuckDB 재계산 경로(``assembly.py``).
See: docs/guides/ctr-model-specification.md (Interaction Feature section)

[Feast ODFV 계약 — #409] ODFV UDF가 부르는 헬퍼는 **반드시 이 모듈처럼 feature_repo
바깥**에 있어야 한다. ``feast apply``는 정의 파일을 cwd 기준 모듈명으로 import하는데
(apply job의 cwd는 ``/app/feature_repo``라 bare ``feature_definitions``), UDF가 그 모듈의
전역을 참조하면 dill이 그 이름을 by-reference로 레지스트리에 박아 ``/app``에서 도는
소비자가 역직렬화에 실패한다. 게이트: ``tests/test_odfv_registry_portability_feast.py``.

[비책임] FeatureView/ODFV **정의**(스키마·소스·ttl)는 ``feature_repo/
feature_definitions.py``가, PIT 조회 배관은 ``autoresearch/feature_engineering/feast_retrieval.py``가 소유한다.
"""

import json
from typing import Union, List
import numpy as np
import pandas as pd

from autoresearch.feature_engineering.embeddings import embed_texts, cosine_similarity
from autoresearch.feature_engineering.category_reference import get_category_description_embedding


def compute_historical_category_match(hist_cat_aff: str, category_id: str) -> int:
    """Binary feature: 과거 행동 기반 선호 카테고리와 현재 영상 카테고리 일치 여부.

    Args:
        hist_cat_aff: 사용자 과거 클릭 기반 선호 카테고리 (문자열, "unknown"은 cold-start).
        category_id: 영상의 YouTube 카테고리 ID (문자열).

    Returns:
        1 if hist_cat_aff == category_id (both as str), else 0.
        hist_cat_aff == "unknown" → always 0 (비교 불가 상태).

    NOTE: 양쪽을 str()로 캐스팅하여 type mismatch 방지 (int vs str).
    """
    if str(hist_cat_aff) == "unknown":
        return 0
    return 1 if str(hist_cat_aff) == str(category_id) else 0


def compute_preferred_category_match(preferred_category: Union[List[str], str], category_id: str) -> int:
    """Binary feature: Persona 기반 선호 카테고리와 현재 영상 카테고리 일치 여부.

    Args:
        preferred_category: 사용자 선호 카테고리 (JSON 리스트 문자열 또는 리스트, 최대 3개).
        category_id: 영상의 YouTube 카테고리 ID (문자열).

    Returns:
        1 if category_id ∈ preferred_category, else 0.
        빈 리스트나 파싱 실패 → 0.
    """
    try:
        if isinstance(preferred_category, str):
            cats = json.loads(preferred_category)
        else:
            cats = preferred_category
        return 1 if str(category_id) in [str(c) for c in cats] else 0
    except (json.JSONDecodeError, TypeError):
        return 0


def compute_category_matches(inputs: pd.DataFrame) -> pd.DataFrame:
    """ODFV 변환 본체 — 위 두 매칭을 프레임 단위로 계산한다(스토어 없이 단위 테스트 가능).

    inputs는 세 소스 뷰를 조인한 컬럼(preferred_category, historical_category_affinity,
    category_id)을 가진다. 두 파생 매칭을 계산해 반환한다.

    ODFV(``feature_repo/feature_definitions.py``의 ``category_match_view``)가 이 함수를
    호출한다. 정의 파일이 아니라 **여기**에 두는 이유는 모듈 docstring의 [Feast ODFV 계약]
    참조 — dill이 by-reference로 기록할 모듈명이 apply 실행 위치에 묶이면 안 된다(#409).
    """
    out = pd.DataFrame(index=inputs.index)
    out["preferred_category_match"] = [
        compute_preferred_category_match(pref, cat)
        for pref, cat in zip(inputs["preferred_category"], inputs["category_id"])
    ]
    out["historical_category_match"] = [
        compute_historical_category_match(hist, cat)
        for hist, cat in zip(inputs["historical_category_affinity"], inputs["category_id"])
    ]
    return out


def embed_keywords(keywords: List[str]) -> List[np.ndarray]:
    """Convert keyword list to individual embeddings.

    각 키워드를 별개로 임베딩하여 keyword-level granularity 유지.
    통째로 합쳐서 임베딩하지 않음 (스펙 규칙 준수). 사용자 관심 키워드는
    "질의" 역할이므로 task_type=RETRIEVAL_QUERY로 임베딩한다 — 카테고리
    설명문(RETRIEVAL_DOCUMENT, category_reference.py)과 비대칭이다.

    호출 1건당 1번의 배치 API 요청으로 처리한다(키워드마다 개별 호출하지
    않음). 다만 이 함수 자체는 호출 단위(보통 유저 1명)를 넘어선 dedup은
    하지 않는다 — 여러 행(row)에 걸쳐 반복 호출을 피해야 하는 경우(예:
    학습 데이터셋 조립)는 호출부가 고유 키워드를 먼저 모아 직접
    embed_texts()를 호출해야 한다 (autoresearch/feature_engineering/assembly.py 참고).

    Args:
        keywords: 키워드 문자열 리스트.

    Returns:
        각 키워드의 embedding 벡터 리스트 (L2-normalized).
    """
    valid_keywords = [kw for kw in keywords if kw and isinstance(kw, str)]
    return embed_texts(valid_keywords, task_type="RETRIEVAL_QUERY")


def compute_topic_similarity(user_keyword_embeddings: List[np.ndarray], category_id: str) -> float:
    """Float feature: 사용자 키워드 임베딩과 카테고리 설명 임베딩 간 유사도 (max-pool).

    각 사용자 키워드를 카테고리 설명과 비교한 후, 가장 높은 cosine similarity 반환.

    Args:
        user_keyword_embeddings: 사용자 preferred_topics에서 추출된 키워드 임베딩 리스트.
        category_id: 영상의 YouTube 카테고리 ID (문자열).

    Returns:
        Cosine similarity 최댓값 (수학적 범위: [-1, 1]. 실제 임베딩에서는
        관련 있는 텍스트끼리 대체로 양수가 나오는 경향이 있을 뿐, 음수가
        나오지 않는다는 보장은 아니다).
        빈 리스트 → 0.0.
    """
    if not user_keyword_embeddings:
        return 0.0

    cat_embedding = get_category_description_embedding(category_id)
    similarities = [cosine_similarity(kw_emb, cat_embedding) for kw_emb in user_keyword_embeddings]
    return round(max(similarities), 4) if similarities else 0.0
