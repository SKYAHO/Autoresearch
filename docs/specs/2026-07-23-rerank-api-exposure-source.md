# rerank API 노출 소스 — 실서버를 거치는 폐루프 앞반부

> 작성: 2026-07-23 | 상태: 설계(리뷰 대기) | 이슈: #277
> 선행: #221·#222(모델 노출 조립·노출 태그), #267(per-slate 클릭·draft 계보),
> 병렬: #278(action log → BQ → 피처 → materialize, Codex 트랙)

## 목표

action log 배치의 노출 결정을 **실제 Inference Server(FastAPI `/rerank`)** 를
거치게 만든다. 지금까지 노출 순위는 두 곳에서 왔다:

- `--exposure-source heuristic` — 키워드 휴리스틱 (LLM·모델 무관)
- `--exposure-source model` — BigQuery `user_recommendations` 파티션
  (일일 배치가 미리 계산한 순위, #216/#222)

여기에 세 번째 소스를 추가한다:

- `--exposure-source rerank-api` — 유저별로 `POST /rerank`를 호출해 **그 자리에서**
  순위를 받는다. 서버는 Feast online store(Redis)에서 피처를 읽고 champion/loop-test
  모델로 추론하므로, 이 경로가 그림의 `Request → Inference Server → Response`
  구간을 실제로 통과한다.

## 전제 (2026-07-23 검증 완료)

- `autoresearch-serving-looptest` deployment가 GKE에서 기동 중 (`ctr-model` v4,
  별칭 `loop-test`, 21피처). `/healthcheck` 200, `/rerank` 응답 정상.
- Feast online store materialize 4개 뷰 전부 최신(7/22), 실조회 값이 BigQuery와 일치.
- 로컬 → 서버 접근은 `kubectl port-forward` (GKE는 DNS 엔드포인트로 접근).

## 설계

### 핵심 결정 — 조립은 재사용, 순위 출처만 교체

`src/pipeline/model_exposure_provider.py`의 `build_model_exposures()`는 순위
목록(`RankedVideo`)을 받아 24개 노출(모델 17 · 트렌딩 5 · 랜덤 2, 부족분 규칙,
`ExposureMetadata` 태그)을 조립한다. 이 로직과 태그 계약은 **한 글자도 바꾸지
않는다.** 새 코드가 하는 일은 오직 "유저의 `RankedVideo` 목록을 HTTP로
만들어내는 것"이다.

```
BQ 소스   : user_recommendations dt 파티션 ──┐
                                             ├─→ build_model_exposures() → 노출+태그
HTTP 소스 : POST /rerank 응답 (신규)  ───────┘        (기존 코드, 무변경)
```

### 신규 모듈 — `src/pipeline/rerank_api.py`

담당 구간: 후보 pool → `/rerank` 요청 → 응답을 순위로 변환. 노출 조립·클릭
판정·저장은 담당하지 않는다(각각 `model_exposure_provider`, `action_logs` 소유).

```python
@dataclass(frozen=True, slots=True)
class RerankApiSettings:
    base_url: str            # 예: http://127.0.0.1:8088
    timeout_sec: float = 30.0
    max_attempts: int = 3    # 연결 오류·5xx만 재시도, 4xx는 즉시 실패


class RerankApiError(RuntimeError):
    """rerank API 호출 실패(재시도 소진·4xx·응답 계약 위반)."""


def select_candidate_video_ids(videos: Sequence[dict], limit: int = 200) -> list[str]:
    """RerankRequest 상한(200)에 맞춰 결정적으로 후보를 고른다.

    view_count 내림차순, 동률은 video_id 오름차순 — build_model_exposures의
    popular_pool 정렬과 동일 기준.
    """


def rank_user(
    settings: RerankApiSettings,
    user_id: str,
    video_ids: Sequence[str],
    *,
    session: requests.Session,
) -> tuple[list[RankedVideo], str]:
    """유저 1명을 /rerank로 순위화한다. (순위 목록, model_id) 반환.

    응답 items를 ctr_score 내림차순(동률 video_id 오름차순)으로 정렬해
    rank 1..N을 부여한다. 응답의 model_id가 곧 계보(policy_version 재료)다.
    """


def make_rerank_api_exposure_provider(
    settings: RerankApiSettings,
    videos: Sequence[dict],
    *,
    candidates_per_user: int = 24,
    personalized_ratio: float = 0.7,
    popular_ratio: float = 0.2,
    exploration_ratio: float = 0.1,
    session: requests.Session | None = None,
) -> ModelExposureRound:
    """CandidateProvider seam에 주입할 lazy HTTP 노출 provider를 만든다.

    provider가 유저 단위로 호출될 때마다 /rerank를 1회 부른다(pool의 유저 수
    = API 호출 수). 응답 순위를 build_model_exposures()에 넘겨 기존과 동일한
    조립·태그를 얻는다. 후보 pool(video_ids)은 provider 생성 시 1회 고정한다.
    """
```

### 오류 의미론 — fail-fast, 조용한 대체 금지

- 재시도 소진·4xx·응답 계약 위반(빈 items, 무점수)은 `RerankApiError`로
  **라운드 전체를 중단**한다. #222의 "휴리스틱 대체 금지" 규칙과 동일 철학 —
  노출 출처가 조용히 바뀐 action log는 재학습 데이터를 오염시킨다.
- **model_id 혼합 금지**: 한 라운드 안에서 응답 `model_id`가 달라지면(서버가
  라운드 도중 재배포됨) 즉시 실패한다. BQ 소스의 "파티션당 단일 run" 계약
  (`load_user_rankings`의 경고·필터)과 같은 목적이지만, HTTP에서는 필터가
  불가능하므로 fail-fast가 유일하게 안전하다.
- 서버가 미존재 엔티티를 기본값으로 관용하는 것(`online_features.py`의
  `*_or_default`)은 서버의 계약이다. 클라이언트는 이를 재검증하지 않는다 —
  다만 후보 pool은 online store에 실재할 가능성이 높은 소스(트렌딩 lake
  파티션)에서 뽑는 것을 운영 전제로 runbook에 명시한다.

### CLI 배선 — `autoresearch/jobs/action_log.py`

- `--exposure-source` choices에 `rerank-api` 추가.
- `--rerank-url` 신설: `rerank-api`일 때 필수, 그 외 소스에서 지정 시
  `BatchArgumentError` (기존 `--recommendations-table`의 검증 패턴과 동일).
- `--rerank-timeout-sec` 신설(기본 30.0).
- `_build_candidate_provider_factory`에 분기 추가 — `src.pipeline.rerank_api`를
  지연 import(heuristic 모드가 src·requests에 의존하지 않는 기존 구조 유지).
- shard/merge 모드와의 관계: `rerank-api`는 **single 모드 전용**으로 시작한다.
  shard는 GCS 체크포인트·재시도 구조라 HTTP 호출의 재현성(재실행 시 다른
  점수) 문제를 따로 설계해야 한다 — 이번 범위 밖, 지정 시 거부.

### 산출·업로드 — 기존 경로 그대로

`--output-base-path gs://…`를 주면 `daily.py`가 dt 파티션
(`data_lake/action_log/dt=YYYY-MM-DD/`)에 쓴다. 신규 코드 없음. 노출 태그는
#222의 draft 운반 경로로 event log까지 전달되며, `policy_version`에는 응답
`model_id`(MLflow run id)가 실린다 — 산출물만 보고 어느 모델이 노출을 만들었는지
추적된다.

## 호출 규모

라운드당 API 호출 = 유저 수(유저당 1회, 후보 ≤200개 동봉). LLM 호출과 달리
비용이 없고 서버는 클러스터 내부다. 100유저 ≈ 100회 · 수 분 내 완료 전망.
Vertex 임베딩은 **호출하지 않는다** — topic_similarity는 서버가 Redis에서 읽는다
(#273의 문제가 이 경로에는 처음부터 없음).

## 테스트

`tests/test_rerank_api.py` (신규, 가짜 session 주입 — 실 HTTP 없음):

1. 응답 → 순위 변환: ctr_score 내림차순·동률 video_id 오름차순, rank 1..N,
   요청 순서와 무관.
2. `select_candidate_video_ids`: 200 상한, view_count·video_id 결정적 선택.
3. 재시도: 연결 오류·5xx는 `max_attempts`까지 재시도 후 `RerankApiError`,
   4xx는 즉시 실패(재시도 없음).
4. model_id 혼합 시 실패.
5. provider 조립: 노출 24개, `exposure_source` 태그(model/trending/random),
   `policy_version == model_id`, metadata 맵이 lazy하게 채워짐.

`tests/test_action_log_job.py` (기존 파일에 추가):

6. `--exposure-source rerank-api`는 `--rerank-url` 필수.
7. `--rerank-url`을 model/heuristic 소스와 함께 주면 거부.
8. shard 모드 + `rerank-api` 거부.

## 실행 runbook (spec 승인 후 실측으로 갱신)

```bash
# 0) 서버 연결
kubectl -n autoresearch port-forward deploy/autoresearch-serving-looptest 8088:8000 &

# 1) LLM 0콜 스모크 — rule_based 클릭 판정으로 배선만 검증
uv run --env-file .env --no-sync python -m autoresearch.jobs.action_log \
  --mode single --partition-date <dt> \
  --youtube-base-path <트렌딩 lake 경로> --virtual-users-path <vu parquet> \
  --output-base-path data/generated/serving_loop_smoke \
  --exposure-source rerank-api --rerank-url http://127.0.0.1:8088 \
  --generator-name rule_based --click-threshold 0.7 --max-users 5

# 2) 실 LLM 라운드 — openrouter + GCS 업로드 (#278이 소비할 dt로)
#    dt 선택은 #278 트랙과 조율 (기존 파티션과 미충돌 날짜)
```

## 범위 밖

- shard/merge 모드의 rerank-api 지원 (HTTP 재현성 설계 필요)
- 후보 pool을 online store 실재 목록과 대조하는 사전 검증
- champion 별칭 교체·모델 품질 (#271/#269, 팀원 트랙)
- 적재 이후의 피처 갱신·materialize (#278, Codex 트랙)
