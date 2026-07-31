"""Vertex AI(text-multilingual-embedding-002) 기반 텍스트 임베딩 유틸리티.

이전에는 해시 기반 pseudo-embedding(PLACEHOLDER)을 썼으나, 실제 의미 기반
임베딩으로 교체했다 (#206, docs/guides/ctr-model-specification.md
Intermediate Artifacts 섹션 참고).

인코더 모델은 gemini-embedding-001이 아니라 text-multilingual-embedding-002를
쓴다 — gemini-embedding-001은 Vertex AI Batch Prediction 작업 자체를 지원하지
않는 유일한 텍스트 임베딩 모델이고 온라인 호출도 분당 토큰 기준의 복잡한
쿼터를 쓴다. text-multilingual-embedding-002는 Batch Prediction을 정식
지원하고(Phase 4의 대량 백필에 필요), 기본 출력이 768차원이라 truncate 없이
이미 정규화된 벡터가 나온다. MTEB 기준 성능 차이는 크지 않다.

task_type은 검색(retrieval) 비대칭성을 반영한다 — 무엇을 임베딩하는지에 따라
RETRIEVAL_QUERY(사용자 관심 키워드처럼 "질의" 역할)와 RETRIEVAL_DOCUMENT
(카테고리 설명문처럼 "검색 대상 문서" 역할)를 구분해서 호출해야 Vertex AI가
권장하는 정확도를 얻는다. 호출부(category_reference.py, feature_builder.py)가
각자의 역할에 맞는 task_type을 지정한다.

임베딩 호출(`embed_texts`)·코사인 유사도(`cosine_similarity`) 외에, Vertex AI
호출에 쓰이는 GCP 자격증명이 아직 유효한지 확인하는 사전점검
(`verify_vertex_ai_credentials`)을 제공한다(#426). 자격증명을 발급·갱신하는
주체는 gcloud ADC 또는 서비스 계정 키이며 이 모듈이 아니다 — 여기서는 만료를
감지해 배치 초반에 실패시키는 역할만 한다.
"""

import os

import numpy as np
from google.api_core.exceptions import (
    Aborted,
    DeadlineExceeded,
    InternalServerError,
    ResourceExhausted,
    ServiceUnavailable,
)
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

# 재시도할 가치가 있는 전송/서버 쪽 일시적 오류만 명시한다(#426). google.auth.
# exceptions.RefreshError(invalid_grant, 세션 만료)나 PermissionDenied 등은
# 재시도로 해결되지 않으므로 여기 없으면 즉시 호출자에게 전파된다.
_RECOVERABLE_ERRORS = (
    ResourceExhausted,  # 429 쿼터 초과
    ServiceUnavailable,  # 503
    DeadlineExceeded,
    InternalServerError,
    Aborted,
)

EMBEDDING_MODEL = "text-multilingual-embedding-002"
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

        # 기본값은 나머지 인프라(BigQuery·GCS·GKE·Redis)와 같은 서울 리전이다 (#276).
        # 미지정 시 조용히 다른 리전으로 나가면 쿼터가 리전별로 잡히는 특성상
        # 원인 파악이 어려워지므로, 기본값 자체를 맞춰 둔다.
        vertexai.init(
            project=os.environ["GCP_PROJECT_ID"],
            location=os.environ.get("GCP_LOCATION", "asia-northeast3"),
        )
        _model = TextEmbeddingModel.from_pretrained(EMBEDDING_MODEL)
    return _model


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=20),
    retry=retry_if_exception_type(_RECOVERABLE_ERRORS),
)
def _get_embeddings_chunk(model, texts: list[str], task_type: str) -> list[np.ndarray]:
    """단일 청크(최대 _MAX_BATCH_SIZE개)를 Vertex AI에 요청한다.

    _RECOVERABLE_ERRORS(429/503/DeadlineExceeded 등 일시적 오류)만 재시도한다(#426).
    text-multilingual-embedding-002의 기본(=768) 출력은 이미 정규화된 단위
    벡터이지만, cosine_similarity()가 내적만으로 코사인 유사도를 계산하는
    전제(단위 벡터)를 API 스펙 변경과 무관하게 항상 지키도록 여기서도 방어적으로
    L2 정규화한다(이미 단위 벡터면 값이 그대로 유지되는 idempotent 연산).
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


def verify_vertex_ai_credentials() -> None:
    """GCP 자격증명이 유효한지 가볍게 확인한다(#426) — 라운드 시작 시 1회.

    `gcloud auth application-default print-access-token`과 동일한 효과 — 실제
    토큰 갱신을 한 번 시도해 세션 만료(invalid_grant)를 정책 시뮬레이션 5단계를
    모두 실행한 뒤가 아니라 시작 시점에 감지한다. 성공하면 아무것도 반환하지
    않고, 실패하면 조치 방법을 담은 ValueError를 던진다.
    """
    import google.auth
    import google.auth.exceptions
    from google.auth.transport.requests import Request

    try:
        # scopes 지정은 서비스 계정 키 경로에 필수다(#426). 서비스 계정 자격증명은
        # requires_scopes=True로 돌아오는데, scope 없이 refresh하면 토큰 엔드포인트가
        # invalid_scope로 거부하고 그 RefreshError가 아래에서 "세션 만료"로 오인된다 —
        # 문서가 권장하는 서비스 계정 전환이 오히려 라운드를 막는 결과가 된다.
        # 사용자 ADC에는 영향이 없다(있으나 없으나 동일하게 갱신 성공).
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(Request())
    except google.auth.exceptions.RefreshError as error:
        raise ValueError(
            "GCP 자격증명 세션이 만료됐습니다. `gcloud auth application-default "
            "login`으로 재인증하거나, 서비스 계정 키를 GOOGLE_APPLICATION_CREDENTIALS로 "
            "설정하세요(재인증이 필요 없어집니다 — docs/guides/feast-gcp-setup.md 참고)."
        ) from error
    except google.auth.exceptions.DefaultCredentialsError as error:
        raise ValueError(
            "GCP 자격증명을 찾을 수 없습니다. `gcloud auth application-default login` "
            "또는 GOOGLE_APPLICATION_CREDENTIALS 설정이 필요합니다."
        ) from error


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two normalized vectors.

    Args:
        a: First embedding vector (assumed L2-normalized).
        b: Second embedding vector (assumed L2-normalized).

    Returns:
        Cosine similarity (range: -1 to 1, typical [0, 1] for normalized vectors).
    """
    return float(np.dot(a, b))
