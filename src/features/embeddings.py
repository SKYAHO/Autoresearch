"""Vertex AI(gemini-embedding-001) 기반 텍스트 임베딩 유틸리티.

이전에는 해시 기반 pseudo-embedding(PLACEHOLDER)을 썼으나, 실제 의미 기반
임베딩으로 교체했다 (#206, docs/guides/ctr-model-specification.md
Intermediate Artifacts 섹션 참고).

task_type은 검색(retrieval) 비대칭성을 반영한다 — 무엇을 임베딩하는지에 따라
RETRIEVAL_QUERY(사용자 관심 키워드처럼 "질의" 역할)와 RETRIEVAL_DOCUMENT
(카테고리 설명문처럼 "검색 대상 문서" 역할)를 구분해서 호출해야 Vertex AI가
권장하는 정확도를 얻는다. 호출부(category_reference.py, feature_builder.py)가
각자의 역할에 맞는 task_type을 지정한다.
"""

__arch__ = {
    "stage": "training",
    "role": "Vertex AI gemini-embedding-001로 텍스트를 배치 임베딩합니다.",
    "owns": [
        "Vertex AI 배치 임베딩 호출(청크·재시도·정규화)",
    ],
    "not_owns": [
        "임베딩 결과의 유사도 계산이나 캐싱",
    ],
}

import os

import numpy as np
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIM = 768
# Vertex AI TextEmbeddingModel.get_embeddings() 요청당 입력 텍스트 상한.
_MAX_BATCH_SIZE = 250

_model = None


def _get_model():
    """TextEmbeddingModel을 프로세스당 1회만 로드해 재사용한다."""
    global _model
    if _model is None:
        import vertexai
        from vertexai.language_models import TextEmbeddingModel

        vertexai.init(
            project=os.environ["GCP_PROJECT_ID"],
            location=os.environ.get("GCP_LOCATION", "us-central1"),
        )
        _model = TextEmbeddingModel.from_pretrained(EMBEDDING_MODEL)
    return _model


@retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(initial=1, max=20))
def _get_embeddings_chunk(model, texts: list[str], task_type: str) -> list[np.ndarray]:
    """단일 청크(최대 _MAX_BATCH_SIZE개)를 Vertex AI에 요청한다. 일시적 오류는 재시도한다.

    gemini-embedding-001은 output_dimensionality=3072(기본값)일 때만 자동으로
    단위 벡터를 반환한다. 우리는 768로 truncate해서 요청하므로, API가 normalize를
    대신 해주지 않는다 — cosine_similarity()가 내적만으로 코사인 유사도를 계산하는
    전제(단위 벡터)를 지키려면 여기서 직접 L2 정규화해야 한다.
    """
    from vertexai.language_models import TextEmbeddingInput

    inputs = [TextEmbeddingInput(text, task_type) for text in texts]
    results = model.get_embeddings(inputs, output_dimensionality=EMBEDDING_DIM)
    vectors = [np.array(r.values, dtype=np.float64) for r in results]
    return [v / np.linalg.norm(v) for v in vectors]


def embed_texts(texts: list[str], task_type: str) -> list[np.ndarray]:
    """여러 텍스트를 Vertex AI로 배치 임베딩한다.

    빈 리스트는 API를 호출하지 않고 빈 리스트를 반환한다. 입력이
    _MAX_BATCH_SIZE(250)를 넘으면 Vertex AI 요청 상한에 맞춰 청크로 나눠
    호출한다. 반환 순서는 입력 순서와 동일하다.

    Args:
        texts: 임베딩할 텍스트 리스트.
        task_type: Vertex AI task type. 예: "RETRIEVAL_QUERY"(질의),
            "RETRIEVAL_DOCUMENT"(검색 대상 문서).

    Returns:
        각 텍스트의 임베딩 벡터 리스트(dim=EMBEDDING_DIM), 입력 순서와 동일.
    """
    if not texts:
        return []
    model = _get_model()
    vectors: list[np.ndarray] = []
    for start in range(0, len(texts), _MAX_BATCH_SIZE):
        chunk = texts[start : start + _MAX_BATCH_SIZE]
        vectors.extend(_get_embeddings_chunk(model, chunk, task_type))
    return vectors


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two normalized vectors.

    Args:
        a: First embedding vector (assumed L2-normalized).
        b: Second embedding vector (assumed L2-normalized).

    Returns:
        Cosine similarity (range: -1 to 1, typical [0, 1] for normalized vectors).
    """
    return float(np.dot(a, b))
