"""전역 pytest fixture.

Vertex AI 임베딩 API를 기본적으로 mock한다 (#206). 실제 GCP 자격 증명·네트워크
없이 테스트가 결정론적으로 동작하도록, src/features/embeddings.py가 lazy import
하는 vertexai.language_models를 가짜 모듈로 치환한다. 가짜 모델은 텍스트를
해시 시드로 삼은 재현 가능한 벡터를 반환한다 — 예전 pseudo-embedding
placeholder와 아이디어는 같지만, 이제는 프로덕션 코드가 아니라 테스트 더블로만
쓰인다.

Vertex AI SDK 호출 자체의 정확한 형태(task_type/output_dimensionality/청킹)를
검증하는 테스트는 tests/test_embeddings.py에서 이 fixture 위에 자체 monkeypatch를
추가로 씌운다(같은 monkeypatch 인스턴스에 다시 setitem하면 나중 설정이 이긴다).
"""

import hashlib
import sys
from unittest.mock import MagicMock

import numpy as np
import pytest


def _deterministic_unit_vector(text: str, dim: int) -> np.ndarray:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    rng = np.random.default_rng(int.from_bytes(h[:8], "big"))
    v = rng.normal(size=dim)
    return v / np.linalg.norm(v)


class _FakeTextEmbeddingInput:
    def __init__(self, text, task_type=None, title=None):
        self.text = text
        self.task_type = task_type
        self.title = title


class _FakeTextEmbedding:
    def __init__(self, values):
        self.values = values


class _FakeTextEmbeddingModel:
    @classmethod
    def from_pretrained(cls, model_name):
        return cls()

    def get_embeddings(self, texts, *, auto_truncate=True, output_dimensionality=None):
        dim = output_dimensionality or 768
        vectors = []
        for item in texts:
            text = item.text if hasattr(item, "text") else str(item)
            vectors.append(_FakeTextEmbedding(_deterministic_unit_vector(text, dim).tolist()))
        return vectors


@pytest.fixture(autouse=True)
def mock_vertex_embeddings(monkeypatch):
    fake_language_models = MagicMock()
    fake_language_models.TextEmbeddingModel = _FakeTextEmbeddingModel
    fake_language_models.TextEmbeddingInput = _FakeTextEmbeddingInput

    fake_vertexai = MagicMock()
    fake_vertexai.init = lambda **kwargs: None
    fake_vertexai.language_models = fake_language_models

    monkeypatch.setitem(sys.modules, "vertexai", fake_vertexai)
    monkeypatch.setitem(sys.modules, "vertexai.language_models", fake_language_models)
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project")

    # embeddings.py/category_reference.py는 프로세스 수명 동안 모델·임베딩을
    # 전역 캐시에 담아둔다 — 테스트 간 격리를 위해 매 테스트마다 초기화한다.
    import src.features.category_reference as category_reference_module
    import src.features.embeddings as embeddings_module

    monkeypatch.setattr(embeddings_module, "_model", None)
    monkeypatch.setattr(category_reference_module, "_CATEGORY_EMBEDDINGS", {})
