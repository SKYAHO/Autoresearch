# GCP/Vertex AI 외부 API 실패 분류·사전점검·우회 옵션 통일 — 설계 문서

> Status: Draft | Issue: #426 | Branch: `feat/426-gcp-error-classification-preflight`

## 배경

Capability probe round_001/round_002의 정책시뮬레이션(5단계) 단계에서 반복적으로
겪은 문제 3가지를 다룬다.

1. round_001은 Vertex AI 임베딩 호출에서 429(쿼터 초과, 재시도로 해결 가능)를
   만났고, round_002는 같은 호출 지점에서 `invalid_grant`(ADC 세션 만료, 재시도로
   해결 불가능·사람의 재인증 필요)를 만났다. 지금 코드는 이 둘을 구분하지 않고
   똑같이 재시도한다.
2. round_002는 round_001 성공 7일 뒤 진행됐는데 같은 자격증명이 무효화돼 있었다.
   라운드(정책시뮬레이션 5단계) 시작 시점에 자격증명이 살아있는지 미리 확인하는
   절차가 없어, 5단계 중 3~4단계쯤(임베딩 호출 시점)에서야 문제를 발견한다.
3. `compute_user_topic_features`에는 `skip_embedding` 우회 옵션이 있지만
   `compute_interaction_columns`에는 없다 — round_002에서 이 결여 하나 때문에
   시뮬레이션 전체가 완주하지 못했다(재인증 후에야 완주).

## 범위 결정

**문제 2의 "서비스 계정 기반 비대화형 인증 경로"는 코드 없이 문서화만 한다.**
`google.auth.default()`(Vertex AI가 내부적으로 쓰는 함수)는 `GOOGLE_APPLICATION_
CREDENTIALS` 환경변수를 gcloud SDK ADC보다 **먼저** 확인하도록 이미 구현돼 있다
(google-auth 공식 동작 순서). 즉 서비스 계정 키를 만들어 이 환경변수만 설정하면
재인증 없이 계속 동작한다 — 코드 변경이 필요 없다. `docs/guides/feast-gcp-setup.md`
(이미 서비스 계정 생성 CLI 절차가 있는 문서)에 Vertex AI 권한 부여 절만 추가한다.

## 아키텍처

### 문제 1 — 재시도 대상을 예외 타입으로 좁힘

`src/features/embeddings.py`의 `_get_embeddings_chunk`에 걸린 `@retry` 데코레이터가
지금은 `retry=` 조건이 없어 **모든 예외를 재시도**한다(tenacity 기본값). 재시도할
가치가 있는 예외(전송/서버 쪽 일시적 오류)만 명시한다.

```python
from google.api_core.exceptions import (
    Aborted, DeadlineExceeded, InternalServerError, ResourceExhausted, ServiceUnavailable,
)
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

_RECOVERABLE_ERRORS = (
    ResourceExhausted,   # 429 쿼터 초과
    ServiceUnavailable,  # 503
    DeadlineExceeded,
    InternalServerError,
    Aborted,
)

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=1, max=20),
    retry=retry_if_exception_type(_RECOVERABLE_ERRORS),
)
def _get_embeddings_chunk(model, texts, task_type):
    ...  # 기존 본문 그대로
```

`google.auth.exceptions.RefreshError`(invalid_grant), `google.api_core.exceptions.
PermissionDenied`/`Unauthenticated` 등 목록에 없는 예외는 재시도 없이 즉시
호출자에게 올라간다 — 재시도 3회 + backoff(최대 약 20초×3)를 낭비하지 않는다.

### 문제 2 — 라운드 시작 사전점검

`src/features/embeddings.py`에 `verify_vertex_ai_credentials()`를 신설한다.
`gcloud auth application-default print-access-token`과 동일한 효과 — 실제 토큰
갱신을 한 번 시도해 세션 만료를 무거운 단계 전에 발견한다.

```python
def verify_vertex_ai_credentials() -> None:
    """GCP 자격증명이 유효한지 가볍게 확인한다(#426) — 라운드 시작 시 1회.

    실제 토큰 갱신을 시도해 세션 만료(invalid_grant)를 정책시뮬레이션 5단계
    전체를 실행하기 전에 빠르게 감지한다. 성공하면 아무것도 반환하지 않는다.
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

`src/pipeline/simulate_policy_round.py`의 `main()`에서 `assembly_source == "duckdb"`
일 때만 호출한다 — `_validate_replay_exposure_args` 직후, `load_reranker`(모델 로드)
전. `"feast"` 경로는 `build_pool_feature_frame_feast`가 임베딩을 호출하지 않고
BigQuery에서 사전계산된 topic_similarity를 읽으므로 이 점검이 필요 없다.

### 문제 3 — `compute_interaction_columns`에 `skip_embedding` 추가

`compute_user_topic_features`와 동일한 패턴을 그대로 적용한다(로직 복제가
아니라 같은 조건 분기를 미러링).

```python
def compute_interaction_columns(joined: pd.DataFrame, skip_embedding: bool = False) -> pd.DataFrame:
    ...
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
    ...  # historical_category_match/preferred_category_match는 skip_embedding과 무관하게 그대로
```

`src/pipeline/simulate_policy_round.py`의 `build_pool_feature_frame`도
`skip_embedding: bool = False` 인자를 받아 `compute_interaction_columns` 호출에
그대로 전달하도록 확장한다.

## 테스트 설계

**`tests/test_embeddings.py`** (기존 fake `TextEmbeddingModel` 컨벤션 재사용):
- 재시도 대상 예외(`ResourceExhausted`)는 여전히 재시도해서 결국 성공
- `RefreshError`는 재시도 없이 즉시 그대로 전파(호출 횟수 1회)
- `verify_vertex_ai_credentials()` 3건: `RefreshError` → `ValueError`,
  `DefaultCredentialsError` → `ValueError`, 정상 → 예외 없음

**`tests/test_features_assembly.py`** — 기존 `compute_user_topic_features` 테스트
2건을 `compute_interaction_columns`에 미러링:
- `skip_embedding=True`면 `embed_texts` 호출 시 즉시 실패하는 스텁으로 확인
- `skip_embedding` 값과 무관하게 `historical_category_match`/`preferred_category_match`는
  동일(로직 drift 방지, #214/#245/#246과 같은 원칙)

**`tests/test_simulate_policy_round.py`**: `assembly_source="duckdb"`일 때
`verify_vertex_ai_credentials()`가 호출되고 `"feast"`일 때는 호출되지 않는
배선 테스트 1건

## 문서

`docs/guides/feast-gcp-setup.md`(이미 서비스 계정 생성 CLI 절차가 있는 문서)에
"부록: 로컬 에이전트용 Vertex AI 서비스 계정(재인증 없이)" 절을 추가한다. 기존
"1. 서비스 계정 생성" 절차에 `roles/aiplatform.user` 권한 부여 한 줄과
`GOOGLE_APPLICATION_CREDENTIALS` 로컬 설정 방법만 얹는다(코드 변경 없음).

## 범위 밖

- 서비스 계정 기반 인증 코드 변경 — 이미 google-auth가 지원, 문서화만
- 재시도 백오프 파라미터(횟수 3회, 대기 1~20초) 자체의 조정 — 이번 이슈는 "무엇을
  재시도할지" 분류가 목적이며 기존 백오프 값은 그대로 유지
- `simulate_policy_round.py` 외 다른 Vertex AI 호출부(예: `category_reference.py`,
  `feature_builder.py`)에 대한 사전점검 추가 — 이번 이슈의 실측 근거(round_001/002)가
  전부 정책시뮬레이션 경로이므로 그 경로만 다룸. 다른 호출부도 같은 문제가 있다면
  별도 이슈로 판단

## 완료 조건

- [ ] `_get_embeddings_chunk`가 재시도 가능한 예외(429/503/DeadlineExceeded 등)만
      재시도하고, 나머지(`RefreshError` 등)는 즉시 전파한다
- [ ] `verify_vertex_ai_credentials()`가 신설되고, `simulate_policy_round.py`의
      `main()`이 `assembly_source == "duckdb"`일 때 라운드 시작 시 이를 호출한다
- [ ] `compute_interaction_columns`에 `skip_embedding` 옵션이 추가되고
      `build_pool_feature_frame`이 이를 전달한다
- [ ] 서비스 계정 기반 인증 절차가 `docs/guides/feast-gcp-setup.md`에 문서화된다

## 관련

- #396/#423 — 이 문제가 처음 관측된 capability probe round_001/round_002 기록
- `docs/guides/feast-gcp-setup.md` — 서비스 계정 생성 절차 추가 대상 문서
