"""
Interaction Feature 계산 함수들.

Training과 Serving에서 동일한 로직으로 사용하여 Training-Serving Skew를 방지한다.
"""

import hashlib
import json
import numpy as np


def compute_category_match(hist_cat_aff: str, category_id: str) -> int:
    """
    사용자의 과거 선호 카테고리와 현재 영상 카테고리가 일치하는지 여부.

    ⚠️ CRITICAL: 양쪽을 str()로 캐스팅하여 비교.
    - YouTube API 원본: 문자열("24")
    - CSV 읽기 후: int로 변환될 수 있음
    - 캐스팅 없으면 에러 없이 조용히 0만 반환하는 silent bug 발생

    Cold-start: hist_cat_aff == "unknown"이면 무조건 0 반환 (비교 불가 상태)
    """
    if str(hist_cat_aff) == "unknown":
        return 0
    return 1 if str(hist_cat_aff) == str(category_id) else 0


def compute_topic_similarity(preferred_topics_json: str, video_topic_json: str) -> float:
    """
    사용자의 선호 주제와 영상의 주제 간 유사도 (Jaccard similarity).

    Baseline 구현. 추후 다른 similarity 메트릭(Cosine, BM25 등)으로 교체 가능.
    """
    try:
        preferred_topics = set(json.loads(preferred_topics_json))
        video_topics = set(json.loads(video_topic_json))
    except (json.JSONDecodeError, TypeError):
        return 0.0

    if not preferred_topics or not video_topics:
        return 0.0

    intersection = len(preferred_topics & video_topics)
    union = len(preferred_topics | video_topics)
    return round(intersection / union, 4)


def _pseudo_embedding(text: str, dim: int = 32) -> np.ndarray:
    """
    PLACEHOLDER: 텍스트 해시 기반 결정론적 pseudo-embedding.

    ⚠️ 이 구현체의 embedding 값은 의미 없음 — 파이프라인 구조 검증 목적.
    실제 구현 시 Sentence Transformer(e.g. all-MiniLM-L6-v2) 또는 다른 텍스트 인코더로 교체 필수.
    """
    h = hashlib.sha256(text.encode("utf-8")).digest()
    rng = np.random.default_rng(int.from_bytes(h[:8], "big"))
    v = rng.normal(size=dim)
    return v / np.linalg.norm(v)


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """두 벡터의 cosine similarity."""
    return float(np.dot(a, b))


def compute_embedding_similarity(user_text: str, video_text: str) -> float:
    """
    사용자 텍스트와 영상 텍스트 간 의미 유사도 (cosine similarity).

    ⚠️ PLACEHOLDER: pseudo-embedding 기반 계산.
    실제 Sentence Transformer 임베딩으로 교체 예정.
    """
    user_emb = _pseudo_embedding(user_text)
    video_emb = _pseudo_embedding(video_text)
    return round(_cosine_similarity(user_emb, video_emb), 4)
