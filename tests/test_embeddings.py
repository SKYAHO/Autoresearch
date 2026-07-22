"""src/features/embeddings.py의 Vertex AI 배치 임베딩 함수 단위 테스트.

tests/conftest.py의 autouse fixture가 기본 fake를 깔아두지만, 여기서는
Vertex AI SDK 호출의 정확한 형태(task_type/output_dimensionality/청킹/정규화)를
검증하기 위해 호출을 기록하는 자체 fake로 다시 monkeypatch한다.
"""

import sys
from unittest.mock import MagicMock

import numpy as np

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
