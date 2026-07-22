"""Interaction Feature 계산 함수들.

Training과 Serving에서 동일한 로직으로 사용하여 Training-Serving Skew를 방지한다.
See: docs/guides/ctr-model-specification.md (Interaction Feature section)
"""

import json
from typing import Union, List
import numpy as np

from src.features.embeddings import embed_texts, cosine_similarity
from src.features.category_reference import get_category_description_embedding


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
    embed_texts()를 호출해야 한다 (src/features/assembly.py 참고).

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
