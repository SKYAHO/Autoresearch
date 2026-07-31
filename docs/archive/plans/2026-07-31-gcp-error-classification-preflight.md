# GCP 외부 API 실패 분류·사전점검·우회옵션 통일 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Vertex AI 임베딩 호출의 재시도 대상을 회복 가능한 예외로 좁히고,
정책시뮬레이션 라운드 시작 시 자격증명을 사전점검하며, `compute_interaction_columns`에
`skip_embedding` 우회 옵션을 추가한다.

**Architecture:** `src/features/embeddings.py`에 재시도 예외 화이트리스트와
`verify_vertex_ai_credentials()`를 추가하고, `src/pipeline/simulate_policy_round.py`의
`main()`이 `assembly_source=="duckdb"`일 때 이를 호출하도록 배선한다.
`src/features/assembly.py`의 `compute_interaction_columns`은 `compute_user_topic_features`와
같은 `skip_embedding` 패턴을 미러링한다.

**Tech Stack:** Python 3.11/3.12, `google-auth`/`google-api-core`(이미 `google-cloud-aiplatform`의
전이 의존성으로 설치돼 있음, 신규 의존성 추가 없음), `tenacity`(기존 의존성).

## Global Constraints

- 신규 pyproject.toml 의존성 추가 없음 — `google.auth`/`google.api_core`는 이미
  `google-cloud-aiplatform` 전이 의존성으로 설치돼 있다.
- 기존 재시도 횟수(3회)·대기시간(1~20초 지수 백오프)은 바꾸지 않는다 — 이번
  변경은 "무엇을 재시도할지"만 좁힌다.
- `compute_interaction_columns`의 `historical_category_match`/`preferred_category_match`
  계산 로직은 `skip_embedding` 값과 무관하게 완전히 동일해야 한다(#214/#245/#246과
  같은 원칙 — 로직을 복제하지 않고 같은 함수를 공유).
- 커밋 메시지 형식은 `<type>: <한국어 설명>` (`.claude/docs/agent-workflow-reference.md`).
- 관련 설계 문서: `docs/archive/specs/2026-07-31-gcp-error-classification-preflight.md`.
  관련 이슈: #426.

---

### Task 1: 재시도 대상을 회복 가능한 예외로 좁히기

**Files:**
- Modify: `src/features/embeddings.py`
- Test: `tests/test_embeddings.py`

**Interfaces:**
- Produces: `src.features.embeddings._RECOVERABLE_ERRORS` (tuple of exception
  types) — 이후 태스크가 참조하지 않으므로 이 모듈 내부 전용.
- `_get_embeddings_chunk`의 외부 시그니처(인자·반환 타입)는 변경 없음.

- [x] **Step 1: 실패하는 테스트 작성**

`tests/test_embeddings.py`에 추가한다(파일 상단에 이미 `from src.features import
embeddings as embeddings_module`, `_install_recording_fake` 헬퍼가 있다):

```python
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
```

(파일 상단에 `import pytest`가 없다면 추가한다 — 먼저 `grep -n "^import\|^from" tests/test_embeddings.py`로 확인.)

- [x] **Step 2: 테스트 실행해서 실패 확인**

Run: `uv run python -m pytest tests/test_embeddings.py -k "retries_resource_exhausted or does_not_retry_refresh_error" -v`
Expected: `test_get_embeddings_chunk_does_not_retry_refresh_error` FAIL — 현재는
`RefreshError`도 3번 재시도하므로 `call_count["n"] == 1` assertion이 3으로 실패.
(`test_get_embeddings_chunk_retries_resource_exhausted`는 이미 통과할 수 있다 —
기존 코드도 모든 예외를 재시도하기 때문. 그래도 이번 변경 후에도 계속 통과해야
하므로 함께 추가한다.)

- [x] **Step 3: `_get_embeddings_chunk`의 재시도 조건 좁히기**

`src/features/embeddings.py` 21~24번째 줄(import)과 52번째 줄(`@retry` 데코레이터)을 수정한다:

```python
# 변경 전 (21~24번째 줄)
import os

import numpy as np
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

# 변경 후
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
    ResourceExhausted,   # 429 쿼터 초과
    ServiceUnavailable,  # 503
    DeadlineExceeded,
    InternalServerError,
    Aborted,
)
```

```python
# 변경 전 (52번째 줄)
@retry(stop=stop_after_attempt(3), wait=wait_exponential_jitter(initial=1, max=20))
def _get_embeddings_chunk(model, texts: list[str], task_type: str) -> list[np.ndarray]:
    """단일 청크(최대 _MAX_BATCH_SIZE개)를 Vertex AI에 요청한다. 일시적 오류는 재시도한다.

    text-multilingual-embedding-002의 기본(=768) 출력은 이미 정규화된 단위
    벡터이지만, cosine_similarity()가 내적만으로 코사인 유사도를 계산하는
    전제(단위 벡터)를 API 스펙 변경과 무관하게 항상 지키도록 여기서도 방어적으로
    L2 정규화한다(이미 단위 벡터면 값이 그대로 유지되는 idempotent 연산).
    """

# 변경 후
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
```

본문(61~66번째 줄)은 그대로 유지한다.

- [x] **Step 4: 테스트 실행해서 통과 확인**

Run: `uv run python -m pytest tests/test_embeddings.py -v`
Expected: PASS 전체(기존 테스트 포함, 신규 2건 포함)

- [x] **Step 5: lint 확인**

Run: `uv run --no-sync ruff check src/features/embeddings.py tests/test_embeddings.py`
Expected: `All checks passed!`

- [x] **Step 6: 커밋**

```bash
git add src/features/embeddings.py tests/test_embeddings.py
git commit -m "fix: Vertex AI 임베딩 재시도를 회복 가능한 예외로 한정 (#426)"
```

---

### Task 2: 라운드 시작 자격증명 사전점검 + 서비스 계정 문서화

**Files:**
- Modify: `src/features/embeddings.py`
- Modify: `src/pipeline/simulate_policy_round.py`
- Modify: `docs/guides/feast-gcp-setup.md`
- Test: `tests/test_embeddings.py`, `tests/test_simulate_policy_round.py`

**Interfaces:**
- Consumes: 없음(Task 1과 독립).
- Produces: `src.features.embeddings.verify_vertex_ai_credentials() -> None`
  (성공 시 반환값 없음, 실패 시 `ValueError`) — Task 2의 `simulate_policy_round.py`
  통합이 이 시그니처를 그대로 소비한다.

- [x] **Step 1: 실패하는 테스트 작성 (embeddings.py)**

`tests/test_embeddings.py`에 추가:

```python
def test_verify_vertex_ai_credentials_raises_on_refresh_error(monkeypatch):
    import google.auth
    from google.auth.exceptions import RefreshError

    class _FakeCredentials:
        def refresh(self, request):
            raise RefreshError("invalid_grant")

    monkeypatch.setattr(google.auth, "default", lambda: (_FakeCredentials(), "proj"))

    with pytest.raises(ValueError, match="세션이 만료"):
        embeddings_module.verify_vertex_ai_credentials()


def test_verify_vertex_ai_credentials_raises_on_missing_credentials(monkeypatch):
    import google.auth
    from google.auth.exceptions import DefaultCredentialsError

    def raise_default_credentials_error():
        raise DefaultCredentialsError("no ADC found")

    monkeypatch.setattr(google.auth, "default", raise_default_credentials_error)

    with pytest.raises(ValueError, match="찾을 수 없습니다"):
        embeddings_module.verify_vertex_ai_credentials()


def test_verify_vertex_ai_credentials_passes_when_refresh_succeeds(monkeypatch):
    import google.auth

    class _FakeCredentials:
        def refresh(self, request):
            pass  # 성공 — 아무것도 하지 않음

    monkeypatch.setattr(google.auth, "default", lambda: (_FakeCredentials(), "proj"))

    embeddings_module.verify_vertex_ai_credentials()  # 예외 없이 통과해야 한다
```

- [x] **Step 2: 테스트 실행해서 실패 확인**

Run: `uv run python -m pytest tests/test_embeddings.py -k verify_vertex_ai_credentials -v`
Expected: FAIL — `AttributeError: module 'src.features.embeddings' has no attribute 'verify_vertex_ai_credentials'`

- [x] **Step 3: `verify_vertex_ai_credentials()` 구현**

`src/features/embeddings.py`의 `cosine_similarity` 함수 앞(파일 끝)에 추가:

```python
def verify_vertex_ai_credentials() -> None:
    """GCP 자격증명이 유효한지 가볍게 확인한다(#426) — 라운드 시작 시 1회.

    `gcloud auth application-default print-access-token`과 동일한 효과 — 실제
    토큰 갱신을 시도해 세션 만료(invalid_grant)를 정책시뮬레이션 5단계 전체를
    실행하기 전에 빠르게 감지한다. 성공하면 아무것도 반환하지 않는다.
    """
    import google.auth
    import google.auth.exceptions
    from google.auth.transport.requests import Request

    try:
        credentials, _ = google.auth.default()
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
```

- [x] **Step 4: 테스트 실행해서 통과 확인 (embeddings.py)**

Run: `uv run python -m pytest tests/test_embeddings.py -v`
Expected: PASS 전체(Task 1의 2건 + 이번 3건 포함)

- [x] **Step 5: `simulate_policy_round.py`에 통합 — 실패하는 테스트 작성**

먼저 `grep -n "^def test_" tests/test_simulate_policy_round.py | head -5`와
`grep -n "^from\|^import" tests/test_simulate_policy_round.py`로 기존 임포트·
mocking 컨벤션을 확인한다(이 파일은 `main()`을 이미 다양한 조건으로 호출하는
테스트가 많으므로 그 패턴을 따른다). 다음을 추가한다:

```python
def test_main_verifies_credentials_when_assembly_source_is_duckdb(monkeypatch):
    # assembly_source="duckdb"(기본값)는 embed_texts를 실제로 호출하므로
    # 사전점검이 실행돼야 한다(#426).
    from src.pipeline import simulate_policy_round as spr

    calls = []
    monkeypatch.setattr(
        spr, "verify_vertex_ai_credentials", lambda: calls.append("called")
    )
    # 필요한 나머지 인자·mock은 기존 happy-path 테스트(예: 파일 상단 근처의
    # 기본 main() 호출 테스트)를 참고해 최소로 구성한다 — assembly_source를
    # 명시하지 않아 기본값(duckdb)을 그대로 쓰게 하거나 명시적으로
    # assembly_source="duckdb"를 전달한다. 사전점검 호출 여부만 검증하면
    # 되므로, verify가 호출된 직후 의도적으로 다른 예외(예: ValueError)를
    # 던져 이후 무거운 단계(reranker 로드 등)까지 실행하지 않게 만드는
    # 방식도 허용된다 — 이 테스트의 목적은 "호출 여부"이지 라운드 전체
    # 실행이 아니다.
    ...
    assert calls == ["called"]


def test_main_skips_credential_check_when_assembly_source_is_feast(monkeypatch):
    # assembly_source="feast"는 build_pool_feature_frame_feast가 BigQuery
    # 사전계산값을 읽으므로 embed_texts를 호출하지 않는다 — 사전점검이
    # 불필요하고, 실행되면 안 된다.
    from src.pipeline import simulate_policy_round as spr

    def fail_if_called():
        raise AssertionError("assembly_source='feast'인데 사전점검이 호출됨")

    monkeypatch.setattr(spr, "verify_vertex_ai_credentials", fail_if_called)
    ...
```

이 두 테스트의 `...` 부분은 `tests/test_simulate_policy_round.py`의 기존
`assembly_source="feast"`/기본(duckdb) 호출 테스트(파일을 grep해서 가장 가까운
예시를 찾을 것 — `feature_store=` mock 주입 패턴 포함)를 그대로 재사용해
`main()`을 실제로 호출하도록 완성한다. 사전점검 자체를 검증하는 것이 목적이므로,
`main()`이 그 이후 단계에서 실패해도(예: reranker 로드 실패) 상관없다 — `calls`가
채워졌는지/`fail_if_called`가 안 불렸는지만 확인하면 된다(필요하면 이후 단계의
예외를 `pytest.raises`나 `try/except`로 흡수한다).

- [x] **Step 6: 테스트 실행해서 실패 확인**

Run: `uv run python -m pytest tests/test_simulate_policy_round.py -k "verifies_credentials or skips_credential_check" -v`
Expected: FAIL — `main()`이 아직 `verify_vertex_ai_credentials`를 호출하지 않음

- [x] **Step 7: `main()`에 사전점검 배선**

`src/pipeline/simulate_policy_round.py` 상단 import에 추가(다른 `src.features.*`
import 근처):

```python
from src.features.embeddings import verify_vertex_ai_credentials
```

`main()`의 `_validate_replay_exposure_args` 호출 직후, `load_reranker` 호출 전
(383~387번째 줄 부근)에 추가:

```python
# 변경 전
    if replay is not None:
        _validate_replay_exposure_args(replay.exposure_args, exposure_args)

    if reranker is None:
        reranker = load_reranker(load_model_settings_from_environment())  # fail-fast

# 변경 후
    if replay is not None:
        _validate_replay_exposure_args(replay.exposure_args, exposure_args)

    # feast 경로는 build_pool_feature_frame_feast가 BigQuery 사전계산값을 읽어
    # embed_texts를 호출하지 않으므로 이 점검이 필요 없다(#426).
    if assembly_source == "duckdb":
        verify_vertex_ai_credentials()

    if reranker is None:
        reranker = load_reranker(load_model_settings_from_environment())  # fail-fast
```

- [x] **Step 8: 테스트 실행해서 통과 확인**

Run: `uv run python -m pytest tests/test_simulate_policy_round.py -v`
Expected: PASS 전체(기존 테스트 포함, 신규 2건 포함 — 회귀 없음)

- [x] **Step 9: 서비스 계정 인증 문서화**

`docs/guides/feast-gcp-setup.md`의 "## 1. 서비스 계정 생성" 절 바로 다음(또는
문서 끝)에 새 절을 추가한다:

```markdown
## 부록: 로컬 에이전트용 Vertex AI 서비스 계정 (재인증 없이)

`gcloud auth application-default login`으로 얻은 세션은 일정 기간 후 만료되며
(capability probe round_002에서 7일 뒤 만료 실측, #426), 만료되면 사람이 브라우저로
재인증해야 합니다. 자율 에이전트가 여러 라운드를 사람 개입 없이 이어 돌리려면
서비스 계정 키를 쓰는 편이 낫습니다 — 코드 변경은 필요 없습니다
(`google.auth.default()`가 `GOOGLE_APPLICATION_CREDENTIALS`를 gcloud ADC보다
먼저 확인하도록 이미 구현돼 있습니다).

1. 위 "1. 서비스 계정 생성" 절차로 서비스 계정을 만들거나 기존 것을 재사용합니다.
2. Vertex AI 임베딩 호출 권한을 부여합니다:
   ```bash
   gcloud projects add-iam-policy-binding "$GCP_PROJECT_ID" \
     --member="serviceAccount:${SA_EMAIL}" \
     --role="roles/aiplatform.user"
   ```
3. 키 파일 경로를 환경변수로 지정합니다:
   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json
   ```
4. 이후 `gcloud auth application-default login` 재인증 없이 계속 동작합니다.
```

(정확한 삽입 위치·기존 `SA_EMAIL` 변수명 등은 파일을 먼저 읽어 기존 절과 톤·
변수명을 맞출 것 — 위 내용은 뼈대이며 문서 전체 스타일에 맞춰 다듬는다.)

- [x] **Step 10: lint + 문서 검증**

Run: `uv run --no-sync ruff check src/features/embeddings.py src/pipeline/simulate_policy_round.py tests/test_embeddings.py tests/test_simulate_policy_round.py`
Expected: `All checks passed!`

Run: `git diff --check`
Expected: 출력 없음

- [x] **Step 11: 커밋**

```bash
git add src/features/embeddings.py src/pipeline/simulate_policy_round.py \
  docs/guides/feast-gcp-setup.md tests/test_embeddings.py tests/test_simulate_policy_round.py
git commit -m "feat: 정책시뮬레이션 라운드 시작 GCP 자격증명 사전점검 추가 (#426)"
```

---

### Task 3: `compute_interaction_columns`에 `skip_embedding` 우회 옵션 추가

**Files:**
- Modify: `src/features/assembly.py`
- Modify: `src/pipeline/simulate_policy_round.py`
- Test: `tests/test_features_assembly.py`

**Interfaces:**
- Consumes: 없음(Task 1·2와 독립, 병렬 가능).
- Produces: `compute_interaction_columns(joined, skip_embedding=False)` —
  `build_pool_feature_frame`이 이 시그니처를 소비한다.

- [x] **Step 1: 실패하는 테스트 작성**

`tests/test_features_assembly.py`에 기존 `compute_user_topic_features` 테스트
(420~458번째 줄 부근의 `test_compute_user_topic_features_skip_embedding_*` 2건)를
미러링해 추가한다:

```python
def test_compute_interaction_columns_skip_embedding_never_calls_embed_texts(monkeypatch):
    def fail_if_called(texts, task_type):
        raise AssertionError("skip_embedding=True인데 embed_texts가 호출됨")

    monkeypatch.setattr(assembly_module, "embed_texts", fail_if_called)

    joined = pd.DataFrame(
        {
            "hobbies_and_interests_list": ['["gaming"]', '["music"]'],
            "historical_category_affinity": ["Gaming", "Music"],
            "category_id": ["Gaming", "Music"],
        }
    )
    out = compute_interaction_columns(joined, skip_embedding=True)

    assert out["topic_similarity"].isna().all()


def test_compute_interaction_columns_skip_embedding_preserves_other_matches():
    joined = pd.DataFrame(
        {
            "hobbies_and_interests_list": ['["gaming"]', '["music"]'],
            "historical_category_affinity": ["Gaming", "Music"],
            "category_id": ["Gaming", "Music"],
        }
    )
    with_embedding = compute_interaction_columns(joined)
    without_embedding = compute_interaction_columns(joined, skip_embedding=True)

    assert list(without_embedding["historical_category_match"]) == list(
        with_embedding["historical_category_match"]
    )
    assert list(without_embedding["preferred_category_match"]) == list(
        with_embedding["preferred_category_match"]
    )
```

(정확한 컬럼 값·타입은 파일 상단의 기존 `test_compute_interaction_columns_matches`
(257번째 줄 부근) 픽스처를 참고해 맞출 것 — 위 코드는 뼈대이며, 실제 테스트 데이터는
기존 테스트의 `joined` 픽스처와 일치시켜 재사용하는 편이 안전하다.)

- [x] **Step 2: 테스트 실행해서 실패 확인**

Run: `uv run python -m pytest tests/test_features_assembly.py -k "compute_interaction_columns_skip_embedding" -v`
Expected: FAIL — `TypeError: compute_interaction_columns() got an unexpected keyword argument 'skip_embedding'`

- [x] **Step 3: `compute_interaction_columns`에 `skip_embedding` 추가**

`src/features/assembly.py`의 `compute_interaction_columns`(466~508번째 줄)을 수정한다:

```python
# 변경 전
def compute_interaction_columns(joined: pd.DataFrame) -> pd.DataFrame:
    """preferred/topic/match 상호작용 피처를 계산해 컬럼으로 추가한다.

    입력 필수 컬럼: hobbies_and_interests_list, historical_category_affinity,
    category_id. (build_training_dataset.py Step 2의 계산을 그대로 이동한 것.)

    preferred_category는 joined에 primary_categories 컬럼이 있으면(virtual_users
    파이프라인의 실제 LLM 산출값, #205) 그 값을 그대로 쓰고, 없으면(구식 mock
    personas.csv 등) derive_preferred_category() 키워드 매핑 fallback을 쓴다.

    user_keyword_embeddings는 joined의 고유(unique) 키워드만 한 번씩 배치
    임베딩한다(#206) — joined는 유저 1명당 여러 행(impression마다 1행)을
    가지므로, 행마다 개별 임베딩하면 같은 키워드를 Vertex AI에 반복
    요청하게 된다.
    """
    out = joined.copy()
    out["preferred_topics"] = out["hobbies_and_interests_list"].apply(extract_keywords_safe)
    if "primary_categories" in out.columns:
        out["preferred_category"] = out["primary_categories"].apply(parse_primary_categories)
    else:
        out["preferred_category"] = out["preferred_topics"].apply(derive_preferred_category)

    unique_keywords = sorted({kw for kws in out["preferred_topics"] for kw in kws})
    keyword_vectors = embed_texts(unique_keywords, task_type="RETRIEVAL_QUERY")
    keyword_embedding_cache = dict(zip(unique_keywords, keyword_vectors))
    out["user_keyword_embeddings"] = out["preferred_topics"].apply(
        lambda kws: [keyword_embedding_cache[kw] for kw in kws]
    )
    out["topic_similarity"] = out.apply(
        lambda row: compute_topic_similarity(row["user_keyword_embeddings"], row["category_id"]),
        axis=1,
    )
    out["historical_category_match"] = out.apply(
        lambda row: compute_historical_category_match(
            row["historical_category_affinity"], row["category_id"]
        ),
        axis=1,
    )
    out["preferred_category_match"] = out.apply(
        lambda row: compute_preferred_category_match(row["preferred_category"], row["category_id"]),
        axis=1,
    )
    return out

# 변경 후
def compute_interaction_columns(joined: pd.DataFrame, skip_embedding: bool = False) -> pd.DataFrame:
    """preferred/topic/match 상호작용 피처를 계산해 컬럼으로 추가한다.

    입력 필수 컬럼: hobbies_and_interests_list, historical_category_affinity,
    category_id. (build_training_dataset.py Step 2의 계산을 그대로 이동한 것.)

    preferred_category는 joined에 primary_categories 컬럼이 있으면(virtual_users
    파이프라인의 실제 LLM 산출값, #205) 그 값을 그대로 쓰고, 없으면(구식 mock
    personas.csv 등) derive_preferred_category() 키워드 매핑 fallback을 쓴다.

    user_keyword_embeddings는 joined의 고유(unique) 키워드만 한 번씩 배치
    임베딩한다(#206) — joined는 유저 1명당 여러 행(impression마다 1행)을
    가지므로, 행마다 개별 임베딩하면 같은 키워드를 Vertex AI에 반복
    요청하게 된다.

    Args:
        skip_embedding: True면 embed_texts() 호출(Vertex AI)을 건너뛰고
            topic_similarity를 전부 None으로 채운다(#426,
            compute_user_topic_features와 동일한 패턴). historical_category_match/
            preferred_category_match는 이 값과 무관하게 항상 계산된다.
    """
    out = joined.copy()
    out["preferred_topics"] = out["hobbies_and_interests_list"].apply(extract_keywords_safe)
    if "primary_categories" in out.columns:
        out["preferred_category"] = out["primary_categories"].apply(parse_primary_categories)
    else:
        out["preferred_category"] = out["preferred_topics"].apply(derive_preferred_category)

    if skip_embedding:
        out["user_keyword_embeddings"] = pd.Series([None] * len(out), index=out.index)
        out["topic_similarity"] = None
    else:
        unique_keywords = sorted({kw for kws in out["preferred_topics"] for kw in kws})
        keyword_vectors = embed_texts(unique_keywords, task_type="RETRIEVAL_QUERY")
        keyword_embedding_cache = dict(zip(unique_keywords, keyword_vectors))
        out["user_keyword_embeddings"] = out["preferred_topics"].apply(
            lambda kws: [keyword_embedding_cache[kw] for kw in kws]
        )
        out["topic_similarity"] = out.apply(
            lambda row: compute_topic_similarity(row["user_keyword_embeddings"], row["category_id"]),
            axis=1,
        )
    out["historical_category_match"] = out.apply(
        lambda row: compute_historical_category_match(
            row["historical_category_affinity"], row["category_id"]
        ),
        axis=1,
    )
    out["preferred_category_match"] = out.apply(
        lambda row: compute_preferred_category_match(row["preferred_category"], row["category_id"]),
        axis=1,
    )
    return out
```

- [x] **Step 4: `build_pool_feature_frame`도 인자 전달하도록 확장**

`src/pipeline/simulate_policy_round.py`의 `build_pool_feature_frame`(117~148번째
줄 부근)을 수정한다:

```python
# 변경 전
def build_pool_feature_frame(
    personas: pd.DataFrame,
    events: pd.DataFrame,
    videos_raw: pd.DataFrame,
    user_id: str,
    as_of: str,
    snapshot_date: str | None = None,
) -> pd.DataFrame:
    """유저 1명 × 전체 영상 pool의 21개 모델 피처 프레임을 학습과 동일 경로로 만든다.

    snapshot_date(YYYY-MM-DD)는 영상 나이(days_since_upload) 기준일이며, 유저
    이력 기준(as_of)과 다를 수 있다. 없으면 as_of의 날짜를 사용한다(기존 동작).
    """
    ...
    frame["hobbies_and_interests_list"] = persona_row["hobbies_and_interests_list"]
    frame = compute_interaction_columns(frame)
    return frame

# 변경 후
def build_pool_feature_frame(
    personas: pd.DataFrame,
    events: pd.DataFrame,
    videos_raw: pd.DataFrame,
    user_id: str,
    as_of: str,
    snapshot_date: str | None = None,
    skip_embedding: bool = False,
) -> pd.DataFrame:
    """유저 1명 × 전체 영상 pool의 21개 모델 피처 프레임을 학습과 동일 경로로 만든다.

    snapshot_date(YYYY-MM-DD)는 영상 나이(days_since_upload) 기준일이며, 유저
    이력 기준(as_of)과 다를 수 있다. 없으면 as_of의 날짜를 사용한다(기존 동작).
    skip_embedding은 compute_interaction_columns에 그대로 전달한다(#426).
    """
    ...
    frame["hobbies_and_interests_list"] = persona_row["hobbies_and_interests_list"]
    frame = compute_interaction_columns(frame, skip_embedding=skip_embedding)
    return frame
```

(`...`은 기존 본문 그대로 — 이 태스크는 마지막 두 줄과 시그니처만 바꾼다. 이
함수를 호출하는 다른 곳(`main()` 안의 `assembly_source == "duckdb"` 분기, 약
402번째 줄)이 `skip_embedding`을 넘기지 않으면 기본값 `False`라 기존 동작과
100% 동일하다 — 이번 태스크는 새 우회 경로를 "제공"만 하고 아직 아무도
호출하지 않는다. 실제로 언제 `skip_embedding=True`로 호출할지는 이 이슈의
범위 밖이다.)

- [x] **Step 5: 테스트 실행해서 통과 확인**

Run: `uv run python -m pytest tests/test_features_assembly.py tests/test_simulate_policy_round.py -v`
Expected: PASS 전체(신규 2건 포함, 회귀 없음)

- [x] **Step 6: lint 확인**

Run: `uv run --no-sync ruff check src/features/assembly.py src/pipeline/simulate_policy_round.py tests/test_features_assembly.py`
Expected: `All checks passed!`

- [x] **Step 7: 커밋**

```bash
git add src/features/assembly.py src/pipeline/simulate_policy_round.py tests/test_features_assembly.py
git commit -m "feat: compute_interaction_columns에 skip_embedding 우회 옵션 추가 (#426)"
```

---

## Self-Review 체크리스트 (구현 착수 전 확인 완료)

- **Spec coverage**: 설계 문서의 완료 조건 4개 중 앞 3개(재시도 분류, 사전점검
  배선, skip_embedding 추가)는 Task 1·2·3이 각각 구현한다. 4번째(서비스 계정
  문서화)는 Task 2 Step 9가 구현한다.
- **Placeholder scan**: 없음 — 모든 스텝에 실제 코드/명령이 있다. Task 2 Step 5,
  Task 3 Step 1의 "정확한 값은 기존 테스트를 참고해 맞출 것"이라는 지시는
  placeholder가 아니라 — 기존 테스트 파일의 실제 픽스처 값(현재 이 플랜
  작성 시점에 정확히 알 수 없는 세부 컬럼 값)을 그대로 재사용하라는 명시적
  지시다.
- **Type consistency**: `verify_vertex_ai_credentials() -> None`이 Task 2 전체에서
  동일하게 쓰인다. `compute_interaction_columns(joined, skip_embedding=False)`
  시그니처가 Task 3의 `build_pool_feature_frame` 호출부와 일치한다.
- **태스크 독립성**: Task 1(embeddings.py 재시도)·Task 2(embeddings.py 사전점검 +
  simulate_policy_round.py 배선 + 문서)·Task 3(assembly.py + simulate_policy_round.py
  시그니처 확장)은 서로 다른 함수를 건드리며, Task 2와 3이 같은 파일
  (`simulate_policy_round.py`)을 건드리지만 서로 다른 함수(`main()`의 다른 지점
  vs `build_pool_feature_frame`)라 병합 충돌 위험이 낮다. 그래도 병렬 실행 시
  마지막에 머지하는 태스크가 먼저 머지된 태스크 위에서 rebase가 필요할 수 있다.

## 다음 단계 (이 플랜 범위 밖)

3개 태스크가 모두 끝나고 전체 테스트가 통과하면,
`.claude/docs/agent-workflow-reference.md`의 PR 워크플로우로 리뷰를 요청한다.
