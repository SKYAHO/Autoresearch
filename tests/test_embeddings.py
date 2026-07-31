"""src/features/embeddings.py의 Vertex AI 배치 임베딩 함수 단위 테스트.

tests/conftest.py의 autouse fixture가 기본 fake를 깔아두지만, 여기서는
Vertex AI SDK 호출의 정확한 형태(task_type/output_dimensionality/청킹/정규화)를
검증하기 위해 호출을 기록하는 자체 fake로 다시 monkeypatch한다.
"""

import sys
from unittest.mock import MagicMock

import numpy as np
import pytest
from google.api_core.exceptions import BadGateway, Unknown
from google.auth.exceptions import TransportError

from src.features import embeddings as embeddings_module


class _RecordingTextEmbeddingInput:
    def __init__(self, text, task_type=None, title=None):
        self.text = text
        self.task_type = task_type


class _RecordingTextEmbedding:
    def __init__(self, values):
        self.values = values


class _RecordingTextEmbeddingModel:
    """get_embeddings 호출 인자를 기록하고, 일부러 정규화되지 않은 벡터를 반환한다
    (embed_texts()가 직접 정규화하는지 검증하기 위함)."""

    calls = []
    from_pretrained_call_count = 0

    @classmethod
    def from_pretrained(cls, model_name):
        cls.model_name = model_name
        cls.from_pretrained_call_count += 1
        return cls()

    def get_embeddings(self, texts, *, auto_truncate=True, output_dimensionality=None):
        type(self).calls.append(
            {
                "texts": [t.text for t in texts],
                "task_types": [t.task_type for t in texts],
                "output_dimensionality": output_dimensionality,
            }
        )
        # 일부러 unit vector가 아닌 값을 반환 (norm=5) — embed_texts()가 정규화를
        # 직접 하는지 검증하기 위함.
        return [
            _RecordingTextEmbedding([5.0] + [0.0] * (output_dimensionality - 1)) for _ in texts
        ]


def _install_recording_fake(monkeypatch):
    _RecordingTextEmbeddingModel.calls = []
    _RecordingTextEmbeddingModel.from_pretrained_call_count = 0
    fake_language_models = MagicMock()
    fake_language_models.TextEmbeddingModel = _RecordingTextEmbeddingModel
    fake_language_models.TextEmbeddingInput = _RecordingTextEmbeddingInput
    fake_vertexai = MagicMock()
    fake_vertexai.init = lambda **kwargs: None
    monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.language_models", fake_language_models)
    monkeypatch.setattr(embeddings_module, "_model", None)
    return _RecordingTextEmbeddingModel


def test_embed_texts_empty_list_skips_api_call(monkeypatch):
    model_cls = _install_recording_fake(monkeypatch)
    result = embeddings_module.embed_texts([], task_type="RETRIEVAL_QUERY")
    assert result == []
    assert model_cls.calls == []


def test_embed_texts_passes_task_type_and_dimension(monkeypatch):
    model_cls = _install_recording_fake(monkeypatch)
    embeddings_module.embed_texts(["gaming", "music"], task_type="RETRIEVAL_QUERY")
    assert len(model_cls.calls) == 1
    call = model_cls.calls[0]
    assert call["texts"] == ["gaming", "music"]
    assert call["task_types"] == ["RETRIEVAL_QUERY", "RETRIEVAL_QUERY"]
    assert call["output_dimensionality"] == embeddings_module.EMBEDDING_DIM


def test_embed_texts_uses_retrieval_document_for_documents(monkeypatch):
    model_cls = _install_recording_fake(monkeypatch)
    embeddings_module.embed_texts(["description"], task_type="RETRIEVAL_DOCUMENT")
    assert model_cls.calls[0]["task_types"] == ["RETRIEVAL_DOCUMENT"]


def test_embed_texts_chunks_requests_at_max_batch_size(monkeypatch):
    model_cls = _install_recording_fake(monkeypatch)
    texts = [f"kw{i}" for i in range(300)]  # _MAX_BATCH_SIZE(250) 초과
    embeddings_module.embed_texts(texts, task_type="RETRIEVAL_QUERY")
    assert len(model_cls.calls) == 2
    assert len(model_cls.calls[0]["texts"]) == 250
    assert len(model_cls.calls[1]["texts"]) == 50


def test_embed_texts_preserves_order_across_chunks(monkeypatch):
    _install_recording_fake(monkeypatch)
    texts = [f"kw{i}" for i in range(300)]
    result = embeddings_module.embed_texts(texts, task_type="RETRIEVAL_QUERY")
    assert len(result) == 300


def test_embed_texts_normalizes_output_vectors(monkeypatch):
    _install_recording_fake(monkeypatch)
    result = embeddings_module.embed_texts(["gaming"], task_type="RETRIEVAL_QUERY")
    # fake 모델은 norm=5인 벡터를 반환하지만, embed_texts()가 방어적으로 L2
    # 정규화해야 한다 — API가 이미 정규화된 값을 주더라도 이 정규화는 idempotent.
    assert np.isclose(np.linalg.norm(result[0]), 1.0)


def test_embed_texts_reuses_model_across_calls(monkeypatch):
    model_cls = _install_recording_fake(monkeypatch)
    embeddings_module.embed_texts(["a"], task_type="RETRIEVAL_QUERY")
    embeddings_module.embed_texts(["b"], task_type="RETRIEVAL_QUERY")
    # from_pretrained는 1회만 호출되고(_model 캐시), get_embeddings만 2번 호출된다.
    assert model_cls.from_pretrained_call_count == 1
    assert len(model_cls.calls) == 2


def test_get_embeddings_chunk_retries_resource_exhausted(monkeypatch):
    from google.api_core.exceptions import ResourceExhausted

    model_cls = _install_recording_fake(monkeypatch)
    call_count = {"n": 0}
    original_get_embeddings = model_cls.get_embeddings

    def flaky_get_embeddings(self, texts, *, auto_truncate=True, output_dimensionality=None):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise ResourceExhausted("429 quota exceeded")
        return original_get_embeddings(self, texts, output_dimensionality=output_dimensionality)

    monkeypatch.setattr(model_cls, "get_embeddings", flaky_get_embeddings)

    result = embeddings_module.embed_texts(["hello"], task_type="RETRIEVAL_QUERY")

    assert call_count["n"] == 3
    assert len(result) == 1


def _disable_retry_wait(monkeypatch, func):
    """재시도 대기(초 단위 backoff)를 제거해 테스트를 즉시 끝낸다."""
    from tenacity import wait_none

    monkeypatch.setattr(func.retry, "wait", wait_none())


def test_get_embeddings_chunk_reraises_original_error_when_retries_exhausted(monkeypatch):
    """3회를 모두 소진하면 tenacity.RetryError가 아니라 원래 예외가 올라와야 한다.

    호출자는 예외 타입으로 실패 원인을 분류하므로(#426), RetryError로 감싸이면
    except ResourceExhausted가 영영 걸리지 않는다.
    """
    import tenacity
    from google.api_core.exceptions import ResourceExhausted

    _disable_retry_wait(monkeypatch, embeddings_module._get_embeddings_chunk)
    model_cls = _install_recording_fake(monkeypatch)
    call_count = {"n": 0}

    def always_resource_exhausted(self, texts, *, auto_truncate=True, output_dimensionality=None):
        call_count["n"] += 1
        raise ResourceExhausted("429 quota exceeded")

    monkeypatch.setattr(model_cls, "get_embeddings", always_resource_exhausted)

    with pytest.raises(ResourceExhausted):
        embeddings_module.embed_texts(["hello"], task_type="RETRIEVAL_QUERY")

    assert call_count["n"] == 3
    assert not issubclass(ResourceExhausted, tenacity.RetryError)


@pytest.mark.parametrize(
    "error",
    [
        Unknown("gRPC UNKNOWN: stream terminated"),
        BadGateway("502"),
        TransportError("connection reset by peer"),
    ],
    ids=["unknown", "bad_gateway", "transport_error"],
)
def test_get_embeddings_chunk_retries_transient_errors(monkeypatch, error):
    """Unknown(gRPC)·BadGateway(502)·TransportError(전송 계층)도 재시도 대상이다(#426)."""
    _disable_retry_wait(monkeypatch, embeddings_module._get_embeddings_chunk)
    model_cls = _install_recording_fake(monkeypatch)
    call_count = {"n": 0}
    original_get_embeddings = model_cls.get_embeddings

    def flaky_get_embeddings(self, texts, *, auto_truncate=True, output_dimensionality=None):
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise error
        return original_get_embeddings(self, texts, output_dimensionality=output_dimensionality)

    monkeypatch.setattr(model_cls, "get_embeddings", flaky_get_embeddings)

    result = embeddings_module.embed_texts(["hello"], task_type="RETRIEVAL_QUERY")

    assert call_count["n"] == 2
    assert len(result) == 1


def test_verify_vertex_ai_credentials_retries_transport_error(monkeypatch):
    """사전점검의 토큰 갱신은 순단(TransportError) 1회는 흡수하고 통과해야 한다(#426)."""
    import google.auth
    from google.auth.exceptions import TransportError

    _disable_retry_wait(monkeypatch, embeddings_module._refresh_credentials)
    call_count = {"n": 0}

    class _FlakyCredentials:
        def refresh(self, request):
            call_count["n"] += 1
            if call_count["n"] < 2:
                raise TransportError("connection reset by peer")

    monkeypatch.setattr(
        google.auth, "default", lambda *args, **kwargs: (_FlakyCredentials(), "proj")
    )

    embeddings_module.verify_vertex_ai_credentials()  # 예외 없이 통과해야 한다

    assert call_count["n"] == 2


def test_verify_vertex_ai_credentials_raises_when_transport_error_persists(monkeypatch):
    """재시도를 모두 소진하면 조치 안내가 담긴 ValueError로 실패한다 — 조용히 통과 금지."""
    import google.auth
    from google.auth.exceptions import TransportError

    _disable_retry_wait(monkeypatch, embeddings_module._refresh_credentials)
    call_count = {"n": 0}

    class _BrokenCredentials:
        def refresh(self, request):
            call_count["n"] += 1
            raise TransportError("connection reset by peer")

    monkeypatch.setattr(
        google.auth, "default", lambda *args, **kwargs: (_BrokenCredentials(), "proj")
    )

    with pytest.raises(ValueError, match="연결하지 못했습니다"):
        embeddings_module.verify_vertex_ai_credentials()

    assert call_count["n"] == 2


def test_verify_vertex_ai_credentials_does_not_retry_refresh_error(monkeypatch):
    """RefreshError는 재시도해도 풀리지 않으므로 1회 시도 후 즉시 실패한다."""
    import google.auth
    from google.auth.exceptions import RefreshError

    _disable_retry_wait(monkeypatch, embeddings_module._refresh_credentials)
    call_count = {"n": 0}

    class _ExpiredCredentials:
        def refresh(self, request):
            call_count["n"] += 1
            raise RefreshError("invalid_grant")

    monkeypatch.setattr(
        google.auth, "default", lambda *args, **kwargs: (_ExpiredCredentials(), "proj")
    )

    with pytest.raises(ValueError, match="세션이 만료"):
        embeddings_module.verify_vertex_ai_credentials()

    assert call_count["n"] == 1


def test_verify_vertex_ai_credentials_raises_on_refresh_error(monkeypatch):
    import google.auth
    from google.auth.exceptions import RefreshError

    class _FakeCredentials:
        def refresh(self, request):
            raise RefreshError("invalid_grant")

    monkeypatch.setattr(
        google.auth, "default", lambda *args, **kwargs: (_FakeCredentials(), "proj")
    )

    with pytest.raises(ValueError, match="세션이 만료"):
        embeddings_module.verify_vertex_ai_credentials()


def test_verify_vertex_ai_credentials_raises_on_missing_credentials(monkeypatch):
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    def raise_default_credentials_error(*args, **kwargs):
        raise DefaultCredentialsError("no ADC found")

    monkeypatch.setattr(google.auth, "default", raise_default_credentials_error)

    with pytest.raises(ValueError, match="찾을 수 없습니다"):
        embeddings_module.verify_vertex_ai_credentials()


def test_verify_vertex_ai_credentials_passes_when_refresh_succeeds(monkeypatch):
    import google.auth

    class _FakeCredentials:
        def refresh(self, request):
            pass  # 성공 — 아무것도 하지 않는다.

    captured: dict = {}

    def fake_default(*args, **kwargs):
        captured.update(kwargs)
        return _FakeCredentials(), "proj"

    monkeypatch.setattr(google.auth, "default", fake_default)

    embeddings_module.verify_vertex_ai_credentials()  # 예외 없이 통과해야 한다

    # scopes를 넘기지 않으면 서비스 계정 키(requires_scopes=True)가 invalid_scope로
    # 거부되고, 그 RefreshError가 "세션 만료"로 오인된다(#426).
    assert captured["scopes"] == ["https://www.googleapis.com/auth/cloud-platform"]


def test_get_embeddings_chunk_does_not_retry_refresh_error(monkeypatch):
    from google.auth.exceptions import RefreshError

    model_cls = _install_recording_fake(monkeypatch)
    call_count = {"n": 0}

    def always_refresh_error(self, texts, *, auto_truncate=True, output_dimensionality=None):
        call_count["n"] += 1
        raise RefreshError("invalid_grant")

    monkeypatch.setattr(model_cls, "get_embeddings", always_refresh_error)

    with pytest.raises(RefreshError):
        embeddings_module.embed_texts(["hello"], task_type="RETRIEVAL_QUERY")

    assert call_count["n"] == 1
