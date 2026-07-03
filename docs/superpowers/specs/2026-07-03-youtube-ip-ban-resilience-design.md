# 유튜브 수집 복원력 설계 (IP 밴 / 실패 대응)

- 상태: Draft (사용자 검토 대기)
- 작성일: 2026-07-03
- 관련 코드: `autoresearch/youtube_collection/`, `dags/youtube_trending_kr_daily.py`
- 관련 회의: 멘토 코칭(2026-07) — "크롤링 안정성 / Cloud Run 프록시 / Fallback Logic" 피드백

## 1. 배경과 목적

현재 일일 수집 DAG(`youtube_trending_kr_daily`)는 YouTube Data API v3 로 KR 트렌딩 영상(~200개/일)을 수집해 GCS Data Lake 에 Parquet 로 적재한다. 구조 자체는 잘 설계되어 있으나(`fetch.py` 가 `googleapiclient` 를 직접 import 하지 않는 callable 주입 패턴), **장애 대응이 전무**하다:

- API Key 1개 고정
- 재시도는 DAG 수준 `retries=2` (whole-task 단위, 정교하지 않음)
- 쿼터 초과 / 레이트리밋 / IP 차단 / Key 무효화에 대한 분기 없음
- 전체 실패 시의 폴백 정책 없음

멘토 피드백의 핵심: "API 를 신뢰하지 말고, 실패 시 재시도와 우회 전략을 가져라. 차단 상황을 고려한 구조를 설계해야 현업 수준의 프로젝트로 평가받는다."

본 설계의 목적은 **단일 API Key + 낙관적 호출 구조를 계층적 복원력 구조로 교체**하는 것이다.

## 2. 실패 모드 분석

공식 API + API Key 조합에서 실제 발생 가능한 5가지 실패 모드:

| # | 실패 모드 | YouTube 응답 | 빈도 (본 프로젝트 기준) |
|---|---|---|---|
| 1 | 일일 쿼터 초과 | 403 `quotaExceeded` | 중 (Key 1개 10,000 units, 하루 ~7 units 사용이라 사실상 희귀, 하지만 Key 공유/증설 시 증가) |
| 2 | 초당/사용자 레이트리밋 | 403 `userRateLimitExceeded` / `rateLimitExceeded`, 429 | 중 |
| 3 | **IP 단위 차단** | 403 (suspension/forbidden 계열) — 모든 Key 가 동일 403 으로 실패. 5xx/네트워크 오류는 IP 밴 시그니처에서 제외(전역 상류 장애 가능) | **저 (꼬리 케이스)** — 반복 패턴 감지 시 |
| 4 | 일시적 서버/네트워크 오류 | 5xx, `TimeoutError`, `ConnectionError` | 중 |
| 5 | Key 무효화/설정 오류 | 403 `keyInvalid` / `accessNotConfigured` | 저 |

핵심 통찰: 공식 API + Key 환경에서 **IP 밴(#3)은 현실적으로 꼬리 케이스**다. 흔한 실패는 쿼터/레이트리밋(#1,#2)이며, 이는 **Key 롤링만으로 Key 없이도 해결**된다. 따라서 Key 롤링이 1차 방어, 프록시는 IP 밴 전용의 벨트-서스펜더(2차) 역할이다. 그럼에도 프록시를 설계하는 이유는 멘토가 명시적으로 요구한 주제이며, Cloud Run + NAT + K8s 차이를 설명할 수 있다는 점에서 포트폴리오/면접 가치가 크기 때문이다.

## 3. 설계 결정 요약

| 결정 | 선택 | 근거 |
|---|---|---|
| 복원력 범위 | 4단계 계층 방어 (재시도 → Key 롤링 → Cloud Run 프록시 → Circuit Breaker) | 멘토 "현업 수준 고민" 부합, 단계별 점진 도입 가능 |
| IP 밴 회피 아키텍처 | Cloud Run 전달 프록시 (dumb forwarder) | 멘토 묘사와 일치, Airflow/GKE 단일 오케스트레이터 구조 유지(Git Sync DAG 합의와 일관) |
| 전체 실패 폴백 | 건너뛰기 + 알림 | 트렌딩은 시점 데이터라 복구 불가; ML 관점에서 수백 일 중 하루 결측은 무시 가능 |
| 복원력 로직 위치 | 신규 모듈 `autoresearch/youtube_collection/client.py` | 기존 callable 주입 seam 활용, DAG 얇게 유지, `fetch.py` 변경 없이 단위테스트 보존 |
| Key 롤링 주체 | collector-side `client.py` | 프록시는 멍청한 전달자로 한정; 책임 분리 |
| Key 풀 저장소 | Google Secret Manager (로컬 개발은 환경변수 폴백) | GCP 현업 표준, 회전/권한/감사 우수 |
| 알림 채널 | Airflow 태스크 실패 (내장 알림) | 별도 구현 0, 운영 노이즈 최소 |
| 재시도 라이브러리 | `tenacity` (exponential backoff + jitter) | 파이썬 표준 |

## 4. 아키텍처

### 4.1 전체 흐름

```
collect_trending (fetch.py, 순수 로직 — 변경 없음)
   │ list_videos(part=..., maxResults=50, pageToken=...)
   ▼
ResilientYouTubeClient.make_callables()   ← client.py (신규)
   │
   ├─① 에러 분류: 재시도? 키 회전? 프록시? 폭주?
   ├─② 재시도: tenacity backoff+jitter (5xx/429/네트워크만)
   ├─③ Key 풀 롤링: quotaExceeded/userRateLimitExceeded → 다음 키
   ├─④ IP 밴 시그니처(전 키 동일 403) → Cloud Run 프록시 경로로 전환 (5xx/네트워크는 일시장애 → skip, 전환 X)
   └─⑤ Circuit Breaker OPEN(전부 실패) → CollectionExhausted raise
                │                                  → DAG 실패 → Airflow 알림 → 그날 skip
                ▼
   [정상 경로] googleapiclient 직접 호출 (GKE NAT IP)
   [④ 프록시 경로] GET {proxy_url}/youtube/v3/... → Cloud Run → YouTube (Cloud Run IP)
```

### 4.2 복원력 로직의 위치 (기존 seam 활용)

`fetch.py` 는 이미 `service.videos().list(**kw).execute()` 모양을 `list_videos(**kw)` callable 로 추상화해 둔다. 따라서 복원력 로직은 **callable 을 생산하는 어댑터 층**에 끼워 넣는다.

```
[현재]                                    [제안]
dags/youtube_trending_kr_daily.py         dags/youtube_trending_kr_daily.py
  └─ _build_service + _make_callables       └─ ResilientYouTubeClient(...).make_callables()
                                              (client.py 로 이동)

fetch.py — 변경 없음 (순수 로직 + 단위테스트 보존)
```

## 5. 상세 설계

### 5.1 `client.py` — ResilientYouTubeClient

```python
class CollectionExhausted(Exception):
    """모든 Key · 프록시 경로마저 실패한 최종 폭주 상태. DAG 가 이걸 잡아 skip+알림."""

class ResilientYouTubeClient:
    def __init__(
        self,
        keys: list[str],
        *,
        proxy_url: str | None = None,
        quota_budget_per_key: int = 9000,   # 10,000 중 안전 마진
        max_retries: int = 3,
        max_proxy_attempts: int = 2,
    ): ...

    def make_callables(self) -> tuple[Callable, Callable, Callable]:
        """fetch.collect_trending 이 기대하는 (list_videos, list_channels,
        list_categories) 반환. 모두 (**kw) -> dict."""
```

동작:
- 각 자원(videos/channels/videoCategories)별 callable 은 내부적으로:
  1. 현재 활성 Key 로 googleapiclient 호출
  2. 예외 분류(5.2) → 액션 결정
  3. tenacity 로 재시도 가능류는 backoff 재시도
  4. Key 회전 대상이면 다음 Key 로 전환 후 재시도
  5. IP 밴 시그니처(전 키 동일 403) 감지 시 proxy_url 경로(`requests.get`)로 전환. 5xx/네트워크는 일시장애로 분류해 프록시 전환 없이 skip
  6. Circuit Breaker 조건 충족 시 `CollectionExhausted` raise
- Key 상태(소진/무효)와 일일 unit 사용량은 in-memory 로 per DAG-run 유지. 매일 새 프로세스이므로 영속 상태 불필요.

### 5.2 에러 분류 규칙

| YouTube reason / 상황 | 분류 | 액션 |
|---|---|---|
| 403 `quotaExceeded` | 키 일일쿼터 소진 | 해당 Key "오늘 소진" 마크 → 회전 (그날 다시 안 씀) |
| 403 `userRateLimitExceeded` / `rateLimitExceeded`, 429 | 키 단위 레이트리밋 | backoff 후 회전 |
| 403 `keyInvalid` / `accessNotConfigured` | Key 자체 무효 | 풀에서 영구 제외 → 회전 |
| 5xx, `TimeoutError`, `ConnectionError` | 일시적(전역 상류 장애 후보) | backoff 재시도 (같은 Key). 모든 Key·backoff 소진 후에도 지속되면 **"일시 장애"로 skip (프록시 전환 없이)** — 상류 장애는 프록시로도 해결 불가 |
| 한 collection run 내 **모든 Key 가 403(suspension/forbidden 계열)으로 동일 실패** | **IP 밴 시그니처** | Cloud Run 프록시 경로로 전환. 403 계열만 시그니처로 인정 (5xx/네트워크는 IP 밴 아님) |
| 프록시 경로도 `max_proxy_attempts` 회 실패 | Circuit Breaker OPEN | `CollectionExhausted` raise |

판정 방식: `googleapiclient.errors.HttpError` 의 `resp.status` 와 본문 `error.errors[].reason` 파싱.

### 5.3 Cloud Run 프록시 계약 (dumb forwarder)

프록시는 상태를 가지지 않는 단순 전달자다. Key 는 collector 가 **HTTP 헤더 `X-Goog-Api-Key`** 로 붙여 보내며, 프록시는 헤더를 googleapis 로 그대로 전달만 한다 (Key 를 다루지 않음). 헤더 방식을 쓰는 이유: query string `key=` 는 Cloud Run access/request logs 에 평문으로 남지만, `X-Goog-Api-Key` 헤더는 기본 로깅에서 노출되지 않아 6장 "Key 평문 노출 주의" 원칙과 충돌하지 않는다.

```
collector(프록시 모드):
  GET {proxy_url}/youtube/v3/videos?part=...&chart=mostPopular
  Header: X-Goog-Api-Key: <현재키>
프록시(FastAPI):
  동일 path+query 로 https://www.googleapis.com/youtube/v3/videos?... 전달 (X-Goog-Api-Key 헤더 포함)
  응답 JSON + status 그대로 반환
  응답이 429/5xx (또는 IP 밴 시그니처)면 내부 unhealthy 플래그 set
  → 이후 GET /health 가 503 반환 → Cloud Run liveness probe 실패 → 컨테이너 재시작
  → 새 인스턴스 → (확률적) 새 egress IP
```

- collector 의 프록시 경로 callable 은 `googleapiclient` 가 아닌 `requests.get(proxy_url/...)` 로 직접 호출. 동일 `(**kw) -> dict` 시그니처 유지(`fetch.py` 변경 없음).
- `proxy_url` 이 `None` 이면 프록시 계층 비활성 → 재시도 + 롤링 + skip 만 동작. **점진적 도입 가능** (1차: client.py만, 2차: 프록시 배포 후 환경변수 주입).

**Key 전달 트레이드오프 (PR 리뷰 반영)**: dumb forwarder 원칙(Key 안 다룸)을 유지하되 Key 노출을 막기 위해 헤더(`X-Goog-Api-Key`) 방식을 선택했다. 검토한 대안: (a) 프록시가 Secret Manager 에서 Key 를 직접 읽으면 egress 경로에 Key 가 아예 안 나가지만, dumb forwarder 원칙을 포기하고 프록시에 상태·권한·Key 회전 책임이 추가된다. (b) Cloud Run access log 의 query string 마스킹도 가능하지만, 헤더 방식이 로깅 정책에 의존하지 않아 더 근본적이다. 본 설계는 헤더 방식 우선; 2차 구현 시 (a)의 복잡도 가치를 재검토.

Cloud Run IP 회피의 정직한 한계:
- "재시작 = 무조건 다른 IP" 가 아님. Google 관리 풀에서 **확률적 회전**이다.
- Cloud Run egress 는 Google 데이터센터 IP 대역이라 레지덴셜 IP가 아님.
- 진짜 핵심 효과는 "GKE NAT IP 가 밴 먹었을 때 Cloud Run 은 **다른 egress 경로**라 우회가 통한다"는 **경로 분리**다. (K8s 파드 재시작은 노드/NAT IP 가 안 바뀌어 효과 없음 — NAT/내부IP/외부IP/공유기 구조와 연관.)

### 5.4 Circuit Breaker / 폴백 (skip + 알림)

- `CollectionExhausted` 가 raise 되면 DAG 태스크는 실패.
- **재시도 금지**: 현재 DAG `default_args={"retries": 2}` 로 인해 일시적 실패는 재시도하지만, `CollectionExhausted` 는 터미널(모든 Key·프록시 소진) 상태라 재시도가 무의미하다. 따라서 태스크 본문에서 `CollectionExhausted` 를 잡아 **`AirflowFailException` 으로 승격**한다 — 이 예외는 `retries` 값과 무관하게 태스크를 재시도 없이 곧바로 `failed` 로 만든다. (태스크 `state` 를 직접 세팅하는 안티패턴은 쓰지 않는다: 실행 중 TI 상태 직접 조작은 스케줄러/executor 와 경합할 수 있다. `retries=0` 오버라이드도 쓰지 않는다 — 일시적 5xx 재시도까지 같이 제거되기 때문.) 일시적(5xx 등) 실패는 기존대로 `retries` 로 재시도.
- Airflow 내장 알림(email/on-call)이 발생 → 그날 partition 은 미생성.
- 멱등 설계(`write_partition` 은 dt 단위 덮어쓰기)이므로 다음 날 수집에 영향 없음. 하루 결측은 ML 학습 데이터(수백 일)에서 무시 가능.

## 6. 설정 / Key 관리

| 항목 | 운영(prod) | 로컬 개발 |
|---|---|---|
| API Key 풀 | Google Secret Manager (복수 secret 또는 1개 JSON) | 환경변수 `YOUTUBE_API_KEYS=key1,key2,...` |
| 프록시 URL | 환경변수 / Airflow Variable `YOUTUBE_PROXY_URL` | 미설정(프록시 비활성) |
| GCS 버킷 | 기존 `YOUTUBE_LAKE_BUCKET` 유지 | 동일 |

Key 는 Secret Manager, collector(`client.py`)가 런타임에 로드. **프록시 경로에서는 query string 이 아닌 `X-Goog-Api-Key` 헤더로 전달**하여 Cloud Run access/request logs 평문 노출을 피한다(자세한 트레이드오프는 §5.3).

## 7. 프로젝트 구조

```
autoresearch/youtube_collection/
  client.py        ← 신규: ResilientYouTubeClient, CollectionExhausted, 에러 분류
  fetch.py         (변경 없음)
  transform.py / load.py / schema.py / backfill.py (변경 없음)

proxy/             ← 신규 Cloud Run 서비스 (별도 이미지)
  main.py          (FastAPI dumb forwarder + /health)
  Dockerfile

dags/youtube_trending_kr_daily.py
  └ _build_service/_make_callables 제거 → ResilientYouTubeClient.make_callables() 로 교체

tests/
  test_youtube_client.py        ← 신규: callable 주입으로 가짜 에러/키 시나리오 단위테스트
  test_proxy.py(또는 별도)       ← 신규: 로컬 Docker 로 프록시 forward/health 통합테스트
```

## 8. 테스트 전략

- **`client.py` 단위테스트**: 가짜 `service` callable 이 상황별로(quotaExceeded/429/5xx/IP밴 시그니처) 예외를 던지도록 주입. Key 회전·프록시 전환·Circuit Breaker 동작을 모두 가짜 데이터로 검증. (기존 `fetch.py` 테스트 패턴과 동일 — googleapiclient Mock 불필요.)
- **`fetch.py` 회귀**: 변경 없음을 확인.
- **프록시 통합테스트**: 로컬 Docker 로 프록시 띄우고, query forward / health 응답 전환 / restart 유도를 검증.
- **CI**: 기존 `.github/workflows/ci.yml`(pytest 3.11/3.12, Docker 빌드) 그대로 통과해야 함.

## 9. 인프라 조율 사항 (인프라 담당 / Terraform)

- Cloud Run 프록시 서비스 Terraform 추가 (별도 issue 권장 — 본 PR 범위와 분리).
- Secret Manager: YouTube API Key secret 생성 + collector GKE 서비스계정에 Secret Accessor 권한.
- 프록시 URL 을 collector(Airflow)에 환경변수/Variable 로 주입.
- GKE NAT IP 와 Cloud Run IP 가 서로 다른 egress 경로임을 인프라 관점에서 확인.

## 10. 점진적 도입 순서 (권장)

1. **1차 PR**: `client.py` (재시도 + Key 롤링 + Circuit Breaker) + DAG 교체 + 단위테스트. `proxy_url=None`. 현실적 실패(쿼터/레이트리밋)의 대부분 즉시 해소.
2. **2차 PR**: `proxy/` Cloud Run 프록시 서비스 + `proxy_url` 주입 + 통합테스트. IP 밴 꼬리 케이스 대응.
3. **3차 (인프라 담당 별도 issue)**: 인프라 담당자가 Cloud Run/Secret Manager 를 Terraform 으로 인프라화. 본 설계는 §9 의 요구사항만 명시하며, 컬렉션 담당자는 Terraform 코드를 작성하지 않는다(역할 분리: 인프라 = 인프라 담당, 컬렉션 = 데이터 수집 담당).

## 11. Non-goals (명시적 비대상)

- 유상 rotating proxy(Bright Data 등) 도입 — 비용/의존성, 하루 200건엔 오버킬.
- 영속 쿼터/Key 상태 저장(DB/Redis) — 매일 새 프로세스라 불필요. Key 풀 전체가 장기 밴이면 설계상 skip+알림으로 처리.
- 수집 자체를 Cloud Run 으로 이관 — Airflow/GKE 단일 오케스트레이터 구조(회의 합의)와 충돌.
- 비-공식 스크래핑 경로 — 본 설계는 공식 API + Key 전용.

## 12. 면접/포트폴리오 관점 (참고)

본 설계는 다음 깊이 있는 질문에 답할 수 있어야 한다:
- "공식 API 를 써도 IP 밴이 나는 이유는?"
- "Cloud Run 재시작이 IP 를 바꾸는 메커니즘과, K8s 파드 재시작이 안 바꾸는 이유(NAT)?"
- "Circuit Breaker 패턴이 왜 필요한가?"
- "Key 롤링과 프록시 중 어느 쪽이 먼저여야 하는가, 그 이유는?"
- "왜 복원력 로직을 DAG 가 아니라 별도 모듈(`client.py`)로 뽑았는가?(테스트용이성/책임분리)"
