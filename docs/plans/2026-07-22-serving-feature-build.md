# 서빙 Feature Build 구현 계획 (#220)

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`(권장) 또는 `superpowers:executing-plans`로 이 계획을 Task 단위로 실행한다. 모든 Step은 체크박스로 추적하고, 각 Task는 `superpowers:test-driven-development`, 최종 통합은 `superpowers:verification-before-completion`을 적용한다.

**목표:** `/rerank`가 `{user_id, video_ids}`만 받아 Feast 온라인 스토어의 4개 FeatureView에서 피처를 조회하고, 확정된 15개 모델 피처를 조립해 `{video_id, ctr_score, model_id}`를 반환하도록 전환한다.

**구조:** HTTP 계층은 요청 검증과 오류 매핑만 담당한다. `ServingFeatureBuilder`가 Feast와 동일한 좁은 reader protocol을 소비해 두 번의 배치 조회(유저·비디오, 유저·카테고리)를 수행하고, 내부 `CandidateVideo`를 만든다. 실제 Feast SDK import는 어댑터 시작 경로로 격리해 기본 dev 테스트는 Redis·Feast 없이 실행한다. 모델은 #216의 `ResolvedModel`을 재사용해 응답에 불변 MLflow run ID를 싣는다.

**기술 스택:** Python 3.12, FastAPI/Pydantic v2, pandas, LightGBM 호환 `predict_proba`, Feast 0.64, Memorystore Redis Cluster, pytest, uv.

## Global Constraints

- 구현 브랜치는 GitHub Issue #220에서 생성한 `feat/220-serving-feature-build`를 사용한다.
- Python 3.12와 `pyproject.toml`/`uv.lock`을 단일 의존성 출처로 사용한다.
- 기본 dev 환경과 Feast 0.64 격리 환경을 합치지 않는다.
- 4개 FeatureView와 모델 입력 15개 피처의 이름·순서를 변경하지 않는다.
- Airflow DAG, BigQuery 피처 ETL, Redis 인프라는 이 저장소 작업 범위가 아니다.
- 새 공개 API 계약은 구 계약을 이중 지원하지 않는다.
- 각 Task는 자신의 소유 파일만 수정하며, 병렬 Task의 파일을 수정하지 않는다.

---

**관련 문서:**

- Issue: `#220 [FEAT] 서빙 Feature Build`
- 기존 serving spec: `docs/specs/2026-07-16-reranking-serving-api.md`
- FeatureView 계약: `feature_repo/feature_definitions.py`
- 15개 모델 피처 계약: `src/pipeline/config.yaml`
- 모델 계보·일일 추천 원장 선행 작업: `#216`, 블로커 수정 `#219`

## 0. 구현 전 전제와 범위 고정

### 브랜치 전제

현재 원격 `feat/220-serving-feature-build`는 `b80819e`에서 분기되어 #216/#219를 포함하지 않는다. 구현을 시작할 때 다음 순서를 지킨다.

1. #216과 #219가 `main`에 병합됐는지 확인한다.
2. 병합됐다면 #220 브랜치를 최신 `origin/main`에 rebase한다.
3. 병합 전 병행 개발이 필요하면 Task 2의 순수 피처 조립까지만 진행하고, `ResolvedModel`이나 `user_recommendations` 코드를 복제하지 않는다.
4. rebase 후 `src/serving/model_loader.py`의 `ResolvedModel`, `load_reranker_with_lineage()`를 그대로 소비한다.

### 이번 이슈의 포함 범위

- 외부 요청 계약을 `{user_id, video_ids}`로 교체한다.
- 요청당 영상은 1~200개, 빈 문자열과 중복 ID는 422로 거부한다.
- 온라인 피처는 호출 횟수가 아니라 **두 번의 배치 API 호출**로 읽는다.
  1. `(user_id, video_id)` 1~200행으로 UserStatic/UserDynamic/Video 피처 조회
  2. 첫 조회에서 얻은 고유 `(user_id, category_id)` 행으로 UserCategorySimilarity 조회
- 모델 입력에는 ID와 조립 보조 컬럼(`preferred_category`)을 넣지 않고, 아티팩트의 15개 컬럼 순서를 그대로 사용한다.
- 응답 `model_id`는 별칭이나 가변 버전명이 아닌 MLflow `run_id`다. 로컬 모델은 기존 #216 계약대로 `"local"`이다.
- fake reader로 모든 단위·HTTP 테스트를 수행하며 실제 Redis 연결은 #210/#218 및 Airflow materialize 완료 뒤 별도 smoke gate로 수행한다.

### 이번 이슈에서 제외할 항목

- `user_recommendations`에 온라인 요청 결과를 추가 기록하지 않는다.
  - #216 테이블은 날짜 파티션 전체를 `WRITE_TRUNCATE`하는 일일 전체 순위 원장이다.
  - 온라인 append를 같은 테이블에 섞으면 배치 재실행 때 삭제되며, 현재 스키마에도 `request_id`, `source`, `served_at`이 없다.
  - #221은 온라인 호출 이력이 아니라 #216의 일일 전체 순위를 소비한다.
  - 온라인 감사 로그가 필요하면 append 전용 테이블과 비동기 sink를 별도 이슈로 설계한다. HTTP critical path에서 BigQuery를 동기 호출하지 않는다.
- FeatureView/15개 피처 자체를 재설계하지 않는다.
- BigQuery 피처 ETL, materialize DAG, Redis 인프라를 수정하지 않는다.
- 요청에 없는 후보 생성·Top-K 절단·LLM 클릭 생성은 수행하지 않는다.

## 1. 고정 계약

### 모델 입력 15개

순서는 `src/pipeline/config.yaml` 및 `feature_columns.pkl`과 완전히 같아야 한다.

```python
MODEL_FEATURE_COLUMNS = (
    "age_group",
    "occupation",
    "historical_category_affinity",
    "recent_click_count_7d",
    "recent_watch_time_7d",
    "recent_like_count_7d",
    "category_id",
    "duration_sec",
    "view_count",
    "like_ratio",
    "comment_ratio",
    "days_since_upload",
    "historical_category_match",
    "preferred_category_match",
    "topic_similarity",
)
```

### Feast 조회 매핑

| 모델/보조 값 | 소스 | 처리 |
| --- | --- | --- |
| `age_group`, `occupation` | `UserStaticView` | 직접 사용 |
| `preferred_category` | `UserStaticView` | 모델 입력이 아닌 `preferred_category_match` 계산 보조 값 |
| `historical_category_affinity`, `recent_click_count_7d`, `recent_watch_time_7d`, `recent_like_count_7d` | `UserDynamicView` | 직접 사용 |
| `category_id`, `duration_sec`, `view_count`, `like_ratio`, `comment_ratio`, `days_since_upload` | `VideoFeatureView` | 직접 사용 |
| `topic_similarity` | `UserCategorySimilarityView` | `(user_id, category_id)` 복합 entity로 조회 |
| `historical_category_match` | derived | 기존 `compute_historical_category_match()` 재사용 |
| `preferred_category_match` | derived | 기존 `compute_preferred_category_match()` 재사용 |

`preferred_topics`, `watch_time_band`, `recent_view_count_7d`, `total_event_count_7d`, 채널 통계, `topic_similarity_top_topic`은 이번 모델 입력에 필요하지 않으므로 조회하지 않는다.

### typed cold-start 기본값

모든 결측을 숫자 0으로 뭉개지 않는다. 학습 의미와 타입을 보존한다.

| 피처 | 기본값 |
| --- | --- |
| `age_group`, `occupation`, `historical_category_affinity`, `category_id` | `"unknown"` |
| `preferred_category` | `[]` |
| 최근 7일 count/watch-time, 영상 count/duration/age | `0` |
| `like_ratio`, `comment_ratio`, `topic_similarity` | `0.0` |
| 두 match 피처 | 위 기본값으로 기존 공용 함수를 계산한 결과 `0` |

학습 categorical artifact에 `"unknown"`이 없으면 기존 Reranker가 NaN으로 강등하고 `rerank_unseen_category_total`로 계측한다. 이것은 묵시적 숫자 0 대체보다 안전한 기존 계약이다.

## 구현 Wave와 Task DAG

### 실행 방식 결정

사용자가 지정한 `superpowers:subagent-driven-development`는 각 Task의 구현 직후 독립 리뷰를 완료해야 다음 Task로 진행할 수 있다. 따라서 이 계획은 같은 파일 충돌 여부와 무관하게 **완전 순차 실행**한다.

```text
Task 0 브랜치 동기화·baseline
  └─ Task 1 외부 API 계약·spec
      └─ Task 2 순수 Feature Build
          └─ Task 3 Feast reader·Redis CA bootstrap
              └─ Task 4 /rerank HTTP 경로·모델 계보
                  └─ Task 5 production startup·이미지·CI
                      └─ Task 6 전체 검증·실연동 smoke
```

| Task | 독립 산출물 | 소유 파일 | 선행 Task | 병렬 가능 |
| --- | --- | --- | --- | --- |
| 0 | 최신 선행 브랜치와 green baseline | Git 상태만 변경 | 없음 | 불가 |
| 1 | 새 HTTP schema와 serving spec | `schemas.py`, `test_serving_schemas.py`, serving spec | 0 | 불가 (Task별 리뷰 게이트) |
| 2 | Redis 없는 순수 2단계 조립기 | `online_features.py`, `test_serving_online_features.py` | 1 | 불가 (Task별 리뷰 게이트) |
| 3 | 실제 Feast SDK/CA 어댑터 | `feast_reader.py`, `feature_repo/bootstrap.py`, materialize 관련 파일 | 1, 2 | 불가 (Task별 리뷰 게이트) |
| 4 | fake builder가 주입된 `/rerank` | `app.py`, `test_serving_api.py` | 1, 2, #216 | 불가 (Task별 리뷰 게이트) |
| 5 | 기본 runtime wiring과 serving image | `app.py`, serving Dockerfile, CI, deployment test | 3, 4 | 불가 |
| 6 | 전체 검증 및 rollout 증거 | serving spec 검증 기록 | 5, #210, #218 | 불가 |

각 Task는 한 agent만 소유하고, 구현자 self-review와 독립 리뷰어의 spec/quality 승인을 모두 얻은 뒤 다음 Task로 넘어간다. Task 3은 `app.py`를, Task 4는 Feast/bootstrap 파일을 수정하지 않는다.

## Task 0: 선행 브랜치 동기화와 baseline 고정

**Files:** 없음

**Interfaces:**

- Consumes: `origin/main`, #216, #219의 병합 상태
- Produces: #216의 `ResolvedModel`, `load_reranker_with_lineage()`가 존재하는 깨끗한 #220 작업 기준점

- [x] **Step 1: 선행 작업 병합 상태 확인**

```bash
git fetch origin
git branch -r --contains origin/feat/216-daily-recommendations-batch
git branch -r --contains origin/fix/219-daily-recommendations-blockers
```

Result (2026-07-22): `origin/main`의 `b9ca06f`(#216)와 `ce4c67b`(#219)에 두 변경이 포함됐다. 머지 후 원격 작업 브랜치는 정리되어 직접 ancestry 확인은 불가능하므로, `origin/main`의 squash 커밋으로 확인했다.

- [x] **Step 2: 선행 작업이 병합된 경우 #220 rebase**

```bash
git rebase origin/main
```

Result (2026-07-22): 충돌 없이 `origin/main`의 `ce4c67b` 위로 rebase했으며, 기준 계획 커밋은 `4b96255`다.

- [x] **Step 3: baseline 검증**

```bash
uv sync --frozen
uv run --no-sync python -m pytest tests/test_serving_api.py tests/test_feast_materialize.py -v
uv lock --check
git status --short
```

Result (2026-07-22): `tests/test_serving_api.py`, `tests/test_serving_model_registry.py`, `tests/test_feast_materialize.py` 37개가 통과했고 `uv lock --check`도 통과했다. MLflow/HTTP 422 관련 기존 경고 3개는 남아 있다.

- [x] **Step 4: 기준 SHA 기록**

```bash
git rev-parse HEAD
```

Result (2026-07-22): Task 1 구현 전 기준 SHA는 이 Task 결과를 기록한 커밋으로 고정한다.

## Task 1: serving spec과 외부 스키마를 먼저 고정

**Files:**

- Modify: `docs/specs/2026-07-16-reranking-serving-api.md`
- Modify: `src/serving/schemas.py`
- Create: `tests/test_serving_schemas.py`

**Interfaces:**

- Consumes: 기존 내부 `CandidateVideo`, `RerankedVideo`
- Produces: `RerankRequest(user_id: str, video_ids: list[str])`, `RerankResponseItem(video_id: str, ctr_score: float, model_id: str)`, `RerankResponse(items: list[RerankResponseItem])`

- [ ] **Step 1: 새 요청/응답 스키마 실패 테스트 작성**

다음을 테스트한다.

```python
def test_rerank_request_accepts_user_and_video_ids_only() -> None:
    request = RerankRequest(user_id="user-1", video_ids=["video-1", "video-2"])
    assert request.model_dump() == {
        "user_id": "user-1",
        "video_ids": ["video-1", "video-2"],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"user_id": "user-1", "video_ids": ["video-1"] * 201},
        {"user_id": "user-1", "video_ids": ["video-1", "video-1"]},
        {
            "user_id": "user-1",
            "video_ids": ["video-1"],
            "candidates": [{"video_id": "video-1", "features": {"x": 1}}],
        },
    ],
)
def test_rerank_request_rejects_invalid_or_legacy_payload(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RerankRequest.model_validate(payload)


def test_response_item_contains_model_id_and_no_user_id() -> None:
    response = RerankResponse(
        items=[RerankResponseItem(video_id="video-1", ctr_score=0.7, model_id="run-1")]
    )
    assert response.model_dump() == {
        "items": [{"video_id": "video-1", "ctr_score": 0.7, "model_id": "run-1"}]
    }
```

Run:

```bash
uv run --no-sync python -m pytest tests/test_serving_schemas.py -v
```

Expected: 새 계약이 아직 없으므로 FAIL.

- [ ] **Step 2: 최소 스키마 구현**

`RerankRequest`를 다음 의미로 변경한다.

```python
class RerankRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    user_id: Annotated[str, Field(min_length=1)]
    video_ids: Annotated[list[Annotated[str, Field(min_length=1)]], Field(min_length=1, max_length=200)]

    @model_validator(mode="after")
    def reject_duplicate_video_ids(self) -> "RerankRequest":
        if len(set(self.video_ids)) != len(self.video_ids):
            raise ValueError("video_ids must not contain duplicates")
        return self
```

`CandidateVideo`는 HTTP 요청 타입에서 제거하되 배치·시뮬레이션이 쓰는 내부 Reranker 입력으로 유지한다. HTTP 응답 전용 `RerankResponseItem`에 `model_id: str`을 추가하고, 내부 `RerankedVideo`에는 모델 계보를 섞지 않는다.

- [ ] **Step 3: spec 갱신**

기존 문서의 “사전 조립된 scalar feature” 및 “MVP에서는 Feature Store 조회를 수행하지 않는다” 문장을 제거하고 다음을 기록한다.

- 요청/응답 JSON 예시
- 200개 제한 및 중복 거부
- 두 번의 배치 Feast 조회
- 15개 순서·보조 컬럼·typed cold-start 표
- `model_id = MLflow run_id`
- BQ 온라인 기록 제외 사유와 별도 이슈 경계

- [ ] **Step 4: 테스트 통과 및 커밋**

Run: 위 targeted test. Expected: PASS.

```bash
git add docs/specs/2026-07-16-reranking-serving-api.md src/serving/schemas.py tests/test_serving_schemas.py
git commit -m "docs: #220 서빙 피처 계약 고정"
```

## Task 2: Redis 없이 검증 가능한 온라인 피처 조립기 구현

**Files:**

- Create: `src/serving/online_features.py`
- Create: `tests/test_serving_online_features.py`
- Reuse unchanged: `src/features/feature_builder.py`

**Interfaces:**

- Consumes: `src.serving.schemas.CandidateVideo`, `compute_historical_category_match()`, `compute_preferred_category_match()`
- Produces: `MODEL_FEATURE_COLUMNS`, `OnlineFeatureReader.read`, `FeatureRetrievalError`, `FeatureContractError`, `ServingFeatureBuilder.build -> list[CandidateVideo]`

- [ ] **Step 1: reader protocol과 fake를 사용하는 실패 테스트 작성**

`tests/test_serving_online_features.py`에 최소 fake reader를 두고 다음을 고정한다.

1. 첫 호출 entity rows가 입력 순서의 `{user_id, video_id}` 1~200행인지
2. 첫 호출이 세 View에서 필요한 12개 직접 피처와 `preferred_category`만 요청하는지
3. 둘째 호출이 중복 제거된 `{user_id, category_id}` 행인지
4. 같은 카테고리의 여러 영상에 similarity가 다시 broadcast되는지
5. 결과 `CandidateVideo.features`가 정확히 15개이며 ID/보조 컬럼이 없는지
6. 입력 영상 순서를 보존하는지
7. 타입별 cold-start 기본값과 두 match 값이 0인지
8. Feast 결과의 길이·entity key가 요청과 다르면 조용히 misjoin하지 않고 `FeatureRetrievalError`인지
9. 모델 artifact 컬럼이 고정 15개와 다르면 `FeatureContractError`인지

Run:

```bash
uv run --no-sync python -m pytest tests/test_serving_online_features.py -v
```

Expected: 모듈이 없어 FAIL.

- [ ] **Step 2: 최소 protocol과 도메인 오류 구현**

실제 Feast 응답 객체를 외부로 새지 않게 reader 계약을 좁힌다.

```python
FeatureRows = Mapping[str, Sequence[object]]

class OnlineFeatureReader(Protocol):
    def read(
        self,
        *,
        feature_refs: Sequence[str],
        entity_rows: Sequence[Mapping[str, str]],
    ) -> FeatureRows:
        raise NotImplementedError

@dataclass(frozen=True, slots=True)
class FeatureRetrievalError(Exception):
    reason: str

@dataclass(frozen=True, slots=True)
class FeatureContractError(Exception):
    reason: str
```

- [ ] **Step 3: 두 단계 조립 구현**

`ServingFeatureBuilder.build()`의 공개 계약을 다음으로 둔다.

```python
@dataclass(frozen=True, slots=True)
class ServingFeatureBuilder:
    reader: OnlineFeatureReader

    def build(
        self,
        *,
        user_id: str,
        video_ids: Sequence[str],
        feature_columns: Sequence[str],
    ) -> list[CandidateVideo]:
        raise NotImplementedError
```

구현 순서:

1. `feature_columns`가 고정 15개와 순서까지 같은지 검증한다.
2. 1차 batch read 후 entity key로 행 정합을 검증한다.
3. `category_id`를 typed default까지 적용한 뒤 고유 category 순서 목록을 만든다.
4. 2차 batch read 후 `(user_id, category_id)`로 similarity map을 만든다.
5. 기존 `compute_historical_category_match`, `compute_preferred_category_match`를 호출한다.
6. 모델 컬럼 순서로 dict를 만들고 `CandidateVideo`로 반환한다.

`.to_dict()` 배열의 우연한 위치만 믿지 말고 반환된 `video_id`/`category_id` key로 검증·join한다. 중복 입력은 API 스키마에서 막지만 builder도 직접 호출될 수 있으므로 명시적으로 거부한다.

- [ ] **Step 4: targeted test 통과 및 커밋**

```bash
uv run --no-sync python -m pytest tests/test_serving_online_features.py -v
git add src/serving/online_features.py tests/test_serving_online_features.py
git commit -m "feat: #220 온라인 피처 배치 조립 추가"
```

## Task 3: 실제 Feast 어댑터와 Redis CA 시작 계약 연결

**Files:**

- Create: `feature_repo/bootstrap.py`
- Modify: `autoresearch/jobs/feast_materialize.py`
- Modify: `tests/test_feast_materialize.py`
- Create: `src/serving/feast_reader.py`
- Create: `tests/test_serving_feast_reader.py`
- Modify: `.env.example`

**Interfaces:**

- Consumes: `OnlineFeatureReader`, `FeatureRows`, `FeatureRetrievalError` from Task 2
- Produces: `FeastOnlineFeatureReader.read`, `load_feast_online_feature_reader(repo_path: str | Path) -> FeastOnlineFeatureReader`, `ensure_redis_ca_bundle`

- [ ] **Step 1: 공용 bootstrap 이동 회귀 테스트 작성**

materialize CLI에만 있는 private `_ensure_ca_bundle()`을 복제하지 않는다. `feature_repo/bootstrap.py`의 공개 함수로 이동하고 기존 materialize 테스트가 같은 동작을 보장하게 한다.

공개 시그니처는 `ensure_redis_ca_bundle(environment: MutableMapping[str, str] | None = None) -> str | None`와 `load_feature_store(repo_path: str | Path) -> object`로 고정한다. Feast import는 `load_feature_store()` 함수 내부에서만 수행한다.

dev 그룹에는 Feast가 없으므로 module top-level Feast import를 두지 않는다.

Run:

```bash
uv sync --frozen
uv run --no-sync python -m pytest tests/test_feast_materialize.py -v
```

Expected: 함수 이동 전 새 import 테스트 FAIL.

- [ ] **Step 2: Feast reader 구현**

`FeastOnlineFeatureReader`는 생성자에서 주입받은 store만 보관하며 `read()`에서 아래 한 줄의 의미만 감싼다.

```python
store.get_online_features(
    features=list(feature_refs),
    entity_rows=[dict(row) for row in entity_rows],
).to_dict()
```

Feast 예외는 secret/entity 값을 로그에 담지 않고 `FeatureRetrievalError`로 연결한다. store factory는 먼저 CA bundle을 준비한 뒤 `FeatureStore(repo_path=str(repo_path))`를 생성한다.

- [ ] **Step 3: 두 의존성 환경에서 테스트**

dev 환경에서는 fake store로 어댑터 변환만 테스트하고, Feast 전용 테스트는 module 수준 `pytest.importorskip("feast")`를 사용한다.

```bash
uv sync --frozen
uv run --no-sync python -m pytest tests/test_serving_online_features.py tests/test_feast_materialize.py -v

uv sync --frozen --no-dev --group feast
uv run --no-sync python -m pytest tests/test_serving_feast_reader.py tests/test_redis_iam.py -v
```

Expected: 양쪽 PASS. 실제 Redis에는 접속하지 않는다.

- [ ] **Step 4: 커밋**

```bash
git add feature_repo/bootstrap.py autoresearch/jobs/feast_materialize.py src/serving/feast_reader.py tests/test_feast_materialize.py tests/test_serving_feast_reader.py .env.example
git commit -m "feat: #220 Feast 온라인 조회 어댑터 연결"
```

## Task 4: 모델 계보와 Feature Build를 `/rerank`에 배선

**Files:**

- Modify: `src/serving/app.py`
- Modify: `tests/test_serving_api.py`

**Interfaces:**

- Consumes: Task 1의 `RerankRequest`/`RerankResponseItem`, Task 2의 `ServingFeatureBuilder`, #216의 `ResolvedModel`
- Produces: `create_app(resolved_model: ResolvedModel | None = None, feature_builder: ServingFeatureBuilder | None = None) -> FastAPI`, fake builder가 주입된 `/rerank` 경로
- Constraint: Task 3 소유 파일과 production Feast factory를 수정하지 않는다. 기본 production wiring은 Task 5가 소유한다.

- [ ] **Step 1: HTTP 통합 실패 테스트 작성**

`create_app()`에 `ResolvedModel`과 `ServingFeatureBuilder` fake를 주입하고 다음을 검증한다.

- `test_rerank_builds_features_from_user_id_and_video_ids`: fake builder가 받은 `user_id`, `video_ids`, `feature_columns`를 정확히 단정한다.
- `test_rerank_returns_scores_with_model_run_id`: 응답 순서와 모든 item의 `model_id == "run-123"`을 단정한다.
- `test_rerank_does_not_accept_caller_supplied_features`: legacy payload가 422인지 단정한다.
- `test_rerank_maps_feature_store_failure_to_503`: `FeatureRetrievalError`를 내는 fake builder로 503을 단정한다.
- `test_healthcheck_requires_both_model_and_feature_store`: 두 의존성 중 하나가 없을 때 503인지 단정한다.
- `test_metrics_observe_video_id_count`: 영상 2개 요청 뒤 histogram count 증가를 단정한다.

spy model이 받은 DataFrame의 컬럼을 확인해 다음을 HTTP 경로에서도 고정한다.

```python
assert tuple(model.received.columns) == MODEL_FEATURE_COLUMNS
assert "user_id" not in model.received
assert "video_id" not in model.received
assert "preferred_category" not in model.received
```

Run:

```bash
uv run --no-sync python -m pytest tests/test_serving_api.py -v
```

Expected: 기존 app이 `request.candidates`를 읽으므로 FAIL.

- [ ] **Step 2: 주입된 의존성의 lifecycle과 ready 상태 구현**

이 Task에서는 테스트로 주입한 `ResolvedModel`과 `ServingFeatureBuilder`만 조립한다. 둘 다 있을 때만 health ready이며 외부 시스템은 열지 않는다. 환경 기반 모델/Feast 기본 로드는 Task 5에서 한 번에 연결한다.

공개 시그니처는 `create_app(resolved_model: ResolvedModel | None = None, feature_builder: ServingFeatureBuilder | None = None) -> FastAPI`로 고정한다.

- [ ] **Step 3: route 변경**

```python
candidates = active_feature_builder.build(
    user_id=request.user_id,
    video_ids=request.video_ids,
    feature_columns=active_model.reranker.feature_columns,
)
outcome = active_model.reranker.rerank_with_diagnostics(candidates)
```

- `RERANK_CANDIDATES.observe(len(request.video_ids))`
- feature store/configuration 실패는 503
- 고정 15개 artifact 계약 위반은 startup/health 503으로 노출
- 예측 오류는 기존 500 유지
- unseen categorical 계측은 그대로 유지
- 각 응답 항목을 `model_id=active_model.run_id`로 변환

- [ ] **Step 4: 테스트 통과 및 커밋**

```bash
uv run --no-sync python -m pytest tests/test_serving_api.py tests/test_serving_online_features.py -v
git add src/serving/app.py tests/test_serving_api.py
git commit -m "feat: #220 rerank 요청을 온라인 피처 조립에 연결"
```

## Task 5: production startup, Feast serving 이미지와 CI gate 구성

**Files:**

- Modify: `src/serving/app.py`
- Modify: `tests/test_serving_api.py`
- Modify: `deploy/serving/Dockerfile`
- Modify: `.github/workflows/ci.yml`
- Create: `tests/test_serving_deployment.py`

**Interfaces:**

- Consumes: Task 3의 `load_feast_online_feature_reader()`, Task 4의 `create_app()`, #216의 `load_reranker_with_lineage()`
- Produces: 환경 기반 production lifespan, Feast 호환 OCI image, CI build/import gate

- [ ] **Step 1: production startup 실패 테스트 작성**

`tests/test_serving_api.py`에서 환경 로더를 monkeypatch해 주입 없이 생성한 앱이 lifespan 시작 시 모델과 Feast reader를 각각 한 번 로드하고 ready가 되는지 검증한다. 어느 하나라도 실패하면 `/healthcheck`는 503이어야 한다.

- `test_lifespan_loads_model_and_feast_builder_from_environment`: 두 loader 호출 횟수와 `/healthcheck` 200을 단정한다.
- `test_healthcheck_is_503_when_feast_initialization_fails`: Feast loader 예외와 `/healthcheck` 503을 단정한다.

Run: `uv run --no-sync python -m pytest tests/test_serving_api.py -k "lifespan or initialization" -v`

Expected: production Feast factory가 아직 app에 연결되지 않아 FAIL.

- [ ] **Step 2: production lifespan 연결**

기본 시작 경로를 다음 순서로 구성한다.

1. `load_reranker_with_lineage(load_model_settings_from_environment())`
2. `RERANK_FEATURE_REPO_PATH`(기본 `feature_repo`)를 읽어 `load_feast_online_feature_reader()` 호출
3. `ServingFeatureBuilder(reader)` 구성
4. 두 의존성이 모두 준비됐을 때만 ready gauge를 1로 설정

테스트에서 두 의존성을 주입하면 환경 로더를 호출하지 않는다.

- [ ] **Step 3: 배포 계약 실패 테스트 작성**

현재 `deploy/serving/Dockerfile`은 `serving` 그룹만 설치하고 `src/`만 복사하므로 Feast와 `feature_repo`가 없다. 정적 계약 테스트로 다음을 고정한다.

- `test_serving_image_installs_feast_compatible_group`: Dockerfile의 export가 `--group feast`를 포함하는지 단정한다.
- `test_serving_image_copies_src_feature_repo_and_bootstrap_package`: `autoresearch`, `feature_repo`, `src` COPY를 단정한다.
- `test_ci_builds_serving_image`: CI가 `deploy/serving/Dockerfile` build와 import smoke를 실행하는지 단정한다.

Run:

```bash
uv sync --frozen
uv run --no-sync python -m pytest tests/test_serving_deployment.py -v
```

Expected: FAIL.

- [ ] **Step 4: Dockerfile 변경**

Feast 0.64는 FastAPI 0.139/Starlette 1.3 계열을 요구하고 현재 `serving` 그룹은 FastAPI `<0.129`다. 두 그룹을 억지로 함께 설치하지 않는다. 이미 lock에 검증된 Feast 실행 조합을 사용한다.

```dockerfile
RUN ["/uv", "export", "--frozen", "--no-dev", "--group", "feast", "--no-hashes", "--output-file", "/requirements.lock"]

COPY autoresearch ./autoresearch
COPY feature_repo ./feature_repo
COPY src ./src
```

현재 기준으로 Feast 그룹에서 기존 serving/Redis 테스트가 함께 통과함을 확인했다. `serving` 그룹은 dev 테스트용 FastAPI 표면으로 유지하고, production serving image만 Feast 호환 그룹을 사용한다.

- [ ] **Step 5: CI에 serving image build/smoke 추가**

기존 Docker build job에 다음을 추가한다.

```bash
docker build -f deploy/serving/Dockerfile -t autoresearch-serving:ci .
docker run --rm autoresearch-serving:ci \
  python -c "import feast, fastapi, feature_repo.redis_iam, src.serving.app"
```

Feast pytest job에는 `tests/test_serving_feast_reader.py`와 `tests/test_serving_api.py`를 추가한다. 실 모델/Redis를 요구하는 uvicorn startup은 CI smoke에서 실행하지 않는다.

- [ ] **Step 6: 로컬 검증 및 커밋**

```bash
uv run --no-sync python -m pytest tests/test_serving_api.py tests/test_serving_deployment.py -v
docker build -f deploy/serving/Dockerfile -t autoresearch-serving:issue-220 .
docker run --rm autoresearch-serving:issue-220 \
  python -c "import feast, fastapi, feature_repo.redis_iam, src.serving.app"
git add src/serving/app.py tests/test_serving_api.py deploy/serving/Dockerfile .github/workflows/ci.yml tests/test_serving_deployment.py
git commit -m "build: #220 Feast 포함 serving 이미지 검증"
```

## Task 6: 전체 회귀 검증과 의존 작업 후 실연동 smoke

**Files:**

- Modify: `docs/specs/2026-07-16-reranking-serving-api.md` (실연동 검증 결과 기록)
- No production code changes; 발견한 결함은 해당 소유 Task로 돌아가 실패 테스트부터 추가한다.

**Interfaces:**

- Consumes: Task 1~5의 통합 결과와 #210/#218/materialize 운영 준비 상태
- Produces: dev/Feast/container 검증 증거와 rollout 전 실제 Redis smoke 결과

- [ ] **Step 1: dev 전체 테스트**

```bash
uv sync --frozen
uv run --no-sync python -m pytest -v
```

Expected: 전체 PASS. 기존 배치·시뮬레이션은 내부 `CandidateVideo`/`Reranker`를 계속 사용하므로 동작 불변.

- [ ] **Step 2: Feast 격리 테스트**

```bash
uv sync --frozen --no-dev --group feast
uv run --no-sync python -m pytest \
  tests/test_redis_iam.py \
  tests/test_feast_materialize.py \
  tests/test_serving_feast_reader.py \
  tests/test_serving_api.py -v
```

Expected: 전체 PASS.

- [ ] **Step 3: 정적·컨테이너 검증**

```bash
uv lock --check
git diff --check
docker build -f deploy/serving/Dockerfile -t autoresearch-serving:issue-220 .
docker run --rm autoresearch-serving:issue-220 \
  python -c "import feast, fastapi, feature_repo.redis_iam, src.serving.app"
```

Expected: exit 0.

- [ ] **Step 4: #210/#218/materialize 완료 뒤 GKE smoke**

이 단계는 코드 PR의 fake 테스트 완료를 막지 않지만 실제 rollout 전에는 필수다.

1. 동일 KSA/Workload Identity와 Redis CA 환경으로 serving pod를 기동한다.
2. 존재하는 user 1명과 video 2개로 `/rerank`를 호출한다.
3. 응답이 2개이며 CTR 내림차순, `model_id`가 현재 champion run ID인지 확인한다.
4. 로그/metric으로 Feast 오류 0, unseen categorical/default 사용량을 확인한다.
5. 없는 user와 없는 video를 각각 호출해 typed cold-start가 200 응답을 내는지 확인한다.
6. 201개, 중복 video ID, legacy candidates 요청이 각각 422인지 확인한다.

- [ ] **Step 5: 최종 문서·커밋**

실연동 결과와 아직 남은 인프라 의존성을 spec/PR에 기록한다.

```bash
git add docs/specs/2026-07-16-reranking-serving-api.md
git commit -m "docs: #220 서빙 Feature Build 검증 결과 기록"
```

## 완료 기준

- `/rerank` 외부 계약은 `{user_id, video_ids[1..200]}`뿐이며 legacy `features` 입력은 거부된다.
- Feast 온라인 조회는 영상 수와 무관하게 두 번의 batch API 호출이다.
- 4개 View에서 필요한 값만 읽고, 15개 모델 컬럼은 artifact 순서와 정확히 일치한다.
- `user_id`, `video_id`, `preferred_category`는 모델 DataFrame에 없다.
- cold-start 기본값은 타입·학습 의미별로 테스트에 고정된다.
- 응답 각 항목에 MLflow `run_id` 기반 `model_id`가 있고 `user_id`는 없다.
- 기본 dev 테스트와 Feast 격리 테스트가 모두 Redis 없이 통과한다.
- serving image가 Feast/feature repo/IAM Redis adapter를 실제로 포함하고 CI에서 import smoke를 통과한다.
- #216/#219 코드를 복제하지 않고 rebase 후 재사용한다.
- 온라인 요청 결과를 #216의 `WRITE_TRUNCATE` 테이블에 섞지 않는다.

## 롤백

API 계약이 breaking change이므로 배포는 이미지 단위로 롤백한다. FeatureView·Redis key·BQ 테이블은 변경하지 않으므로 이전 serving image로 되돌리면 데이터 롤백은 없다. 호출자는 새 이미지 전환과 동시에 `{user_id, video_ids}` 계약으로 전환하고, 구 계약과 신 계약을 한 endpoint에서 이중 지원하지 않는다.
