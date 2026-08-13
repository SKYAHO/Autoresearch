"""전역 pytest fixture.

Vertex AI 임베딩 API를 기본적으로 mock한다 (#206). 실제 GCP 자격 증명·네트워크
없이 테스트가 결정론적으로 동작하도록, autoresearch/feature_engineering/embeddings.py가 lazy import
하는 vertexai.language_models를 가짜 모듈로 치환한다. 가짜 모델은 텍스트를
해시 시드로 삼은 재현 가능한 벡터를 반환한다 — 예전 pseudo-embedding
placeholder와 아이디어는 같지만, 이제는 프로덕션 코드가 아니라 테스트 더블로만
쓰인다.

같은 이유로 `google.auth.default()`도 대역으로 치환한다 — 라운드 시작
자격증명 사전점검(`verify_vertex_ai_credentials`, #426)이 실제 토큰 갱신을
시도하므로, 치환하지 않으면 ADC가 없는 CI에서 실패하고 개발자 머신에서는 매
테스트마다 네트워크 왕복이 생긴다. 사전점검 자체의 실패 분기를 검증하는
테스트는 이 위에 자체 monkeypatch를 다시 씌운다.

Vertex AI SDK 호출 자체의 정확한 형태(task_type/output_dimensionality/청킹)를
검증하는 테스트는 tests/feature_engineering/test_embeddings.py에서 이 fixture 위에 자체 monkeypatch를
추가로 씌운다(같은 monkeypatch 인스턴스에 다시 setitem하면 나중 설정이 이긴다).

`load_dotenv`도 전역으로 막는다 — 상세는 `block_local_dotenv` 참조.
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


class _FakeCredentials:
    """google.auth.default()가 돌려주는 자격증명 대역 — refresh가 항상 성공한다."""

    # 실제 Credentials는 refresh 후 token을 채운다. 대역에도 두어, 나중에 .token을
    # 읽는 코드가 생겨도 원인 불명의 AttributeError 대신 알아볼 수 있는 값이 나온다.
    token = "fake-token"

    def refresh(self, request):
        return None


@pytest.fixture(autouse=True)
def mock_vertex_embeddings(monkeypatch):
    import google.auth

    monkeypatch.setattr(
        google.auth, "default", lambda *args, **kwargs: (_FakeCredentials(), "test-project")
    )

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
    import autoresearch.feature_engineering.category_reference as category_reference_module
    import autoresearch.feature_engineering.embeddings as embeddings_module

    monkeypatch.setattr(embeddings_module, "_model", None)
    monkeypatch.setattr(category_reference_module, "_CATEGORY_EMBEDDINGS", {})


@pytest.fixture(autouse=True)
def block_local_dotenv(monkeypatch):
    """테스트가 개발자의 로컬 `.env`를 프로세스 환경에 싣지 못하게 막는다.

    `scripts/` 아래 10개 모듈이 `main()`에서 `load_dotenv()`를 부른다. 이것은 monkeypatch가
    아니라 **프로세스 환경을 직접** 바꾸므로, 그런 `main()`을 호출하는 테스트 하나가
    `.env`의 값을 세션 끝까지 남긴다. 그러면 **관계없는 다른 테스트**가 그 환경을 보고
    다르게 동작한다.

    실제로 그랬다 — #754에서 테스트 배치를 바꾸며 실행 순서가 달라지자
    `tests/jobs/test_feast_materialize.py` 3건이 깨졌다. 원인은 그 파일이 아니라
    `.env`의 `AUTORESEARCH_ENV=dev`를 실어 온 다른 모듈이었고, 증상은 "알 수 없는 이유로
    세 건이 깨진다"는 형태였다.

    CI에는 `.env`가 없어 이 결함이 드러나지 않는다. **로컬에서만 나고, 원인이 호출한
    테스트가 아니라 뒤에 오는 테스트에서 보이는** 종류다. 그래서 모듈마다 개별
    monkeypatch를 거는 대신 여기서 한 번 막는다 — 새로 추가되는 테스트도 자동으로
    보호된다.

    `.env` 로딩 자체를 검증하려는 테스트는 이 위에 자체 monkeypatch를 다시 씌운다.
    """
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    for module in list(sys.modules.values()):
        if getattr(module, "__name__", "").startswith("scripts") and hasattr(
            module, "load_dotenv"
        ):
            monkeypatch.setattr(module, "load_dotenv", lambda *args, **kwargs: False)
