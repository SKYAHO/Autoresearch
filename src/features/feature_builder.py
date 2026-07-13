"""Interaction Feature 계산 함수들.

Training과 Serving에서 동일한 로직으로 사용하여 Training-Serving Skew를 방지한다.
See: CTR_Model_Specification.md (Interaction Feature section)
"""

import json
from typing import Union, List
import numpy as np

from src.features.embeddings import embed_text, cosine_similarity
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
    통째로 합쳐서 임베딩하지 않음 (스펙 규칙 준수).

    Args:
        keywords: 키워드 문자열 리스트.

    Returns:
        각 키워드의 embedding 벡터 리스트 (L2-normalized).
    """
    return [embed_text(kw) for kw in keywords if kw and isinstance(kw, str)]


def compute_topic_similarity(user_keyword_embeddings: List[np.ndarray], category_id: str) -> float:
    """Float feature: 사용자 키워드 임베딩과 카테고리 설명 임베딩 간 유사도 (max-pool).

    각 사용자 키워드를 카테고리 설명과 비교한 후, 가장 높은 cosine similarity 반환.

    Args:
        user_keyword_embeddings: 사용자 preferred_topics에서 추출된 키워드 임베딩 리스트.
        category_id: 영상의 YouTube 카테고리 ID (문자열).

    Returns:
        Cosine similarity 최댓값 (range: [0, 1]).
        빈 리스트 → 0.0.
    """
    if not user_keyword_embeddings:
        return 0.0

    cat_embedding = get_category_description_embedding(category_id)
    similarities = [cosine_similarity(kw_emb, cat_embedding) for kw_emb in user_keyword_embeddings]
    return round(max(similarities), 4) if similarities else 0.0
