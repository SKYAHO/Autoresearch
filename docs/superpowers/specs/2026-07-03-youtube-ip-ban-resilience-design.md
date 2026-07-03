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

> **이중 목표 명시:** 본 설계는 두 목표를 동시에 추구한다 — (1) **실용적 복원력**: 현실적 실패(5xx/네트워크)에 대한 1차 재시도 방어는 하루 ~7 units 규모에서도 분명한 가치가 있다. (2) **학습/포트폴리오**: Cloud Run 프록시·Circuit Breaker 등 2차 계층은 이 규모의 엔지니어링 정답(tenacity + skip)을 넘어서는 **의도적 오버엔지니어링**이며, 멘토가 명시적으로 요구한 주제이자 GCP egress/NAT/Cloud Run 차이를 설명할 수 있다는 점에서 포트폴리오 가치가 크다. 이 자기 인식을 본문 전체에 일관되게 가져간다(§12 면접 관점 참조).

## 2. 실패 모드 분석

공식 API + API Key 조합에서 실제 발생 가능한 실패 모드 (YouTube 공식 에러 문서 `core_errors` 기준):

| # | 실패 모드 | YouTube 응답 (reason) | 빈도 (본 프로젝트 기준) | Key 롤링 유효? |
|---|---|---|---|---|
| 1 | 일일 쿼터 초과 | 403 `quotaExceeded` / 403 `dailyLimitExceeded` | **희귀** — 하루 ~7 units (프로젝트 기본 10,000 의 0.07%) | **무효** (프로젝트 단위 쿼터, 같은 프로젝트 Key 회전해도 공유) |
| 2 | 레이트리밋 | 403 `userRateLimitExceeded` / 403·429 `rateLimitExceeded` / 403 `servingLimitExceeded` / `concurrentLimitExceeded` | 희귀 — 호출량 적음 | **무효(보수적)** — 공식 문서는 `userRateLimitExceeded` 를 "per-user" 로 명시. 본 수집기는 API-key-only(OAuth 인증 사용자 없음) 컨텍스트라 이 reason 의 발생 가능성/스코프가 불명확하므로, per-credential 으로 가정하고 회전하는 것은 위험 → 같은 Key 로 backoff |
| 3 | **IP 단위 차단** | 403 (suspension/forbidden 계열) — 모든 Key 가 동일 403 으로 실패. 5xx/네트워크 오류는 IP 밴 시그니처에서 제외(전역 상류 장애 가능) | **저 (꼬리 케이스)** — 반복 패턴 감지 시 | 무관 (프록시 경로 전환으로 대응) |
| 4 | 일시적 서버/네트워크 오류 | 5xx (`internalError`/`backendError`/`notReady` 등), `TimeoutError`, `ConnectionError`, DNS(`gaierror`), `SSLError` | **중 (가장 현실적)** | **무효** (상류 장애 → 같은 Key backoff 재시도, 지속 시 skip) |
| 5 | Key 자체 무효화(삭제/만료) | 400 `keyInvalid` / 400 `keyExpired` / 401 `unauthorized`·`authError`·`required`·`expired` | 저 | **유효** — 해당 Key 만의 문제이므로 다른 정상 Key 로 회전 |
| 6 | API 활성화/설정 누락 | 403 `accessNotConfigured` | — | **무효** — 공식 문서가 명시적으로 "your **project** is not configured to access this API" 라 서술(프로젝트 스코프). 같은 프로젝트의 다른 Key 로 회전해도 똑같이 실패 → 즉시 터미널(skip+알림) |
| 7 | reason 파싱 불가 / 알 수 없음 | reason 필드 누락, malformed JSON 본문, 미정의 reason | — | **일시적 취급(기본 정책)** — 분류 불가 시 backoff 재시도, 소진 시 skip |

**핵심 통찰 (리서치 + 공식 문서 팩트체크 정정):** 가장 현실적인 실패는 일시적 5xx/네트워크(#4)이며, **재시도(tenacity)가 1차 방어**다. Key 롤링의 유효 범위는 좁다:

- YouTube Data API 의 쿼터(#1)와 API 활성화(#6)는 **GCP 프로젝트 단위**. 공식 문서가 `accessNotConfigured` 를 "your project" 로, 쿼터를 "projects that enable the API" 로 명시. 같은 프로젝트의 다수 Key 는 쿼터를 공유하고, 같은 활성화 상태를 공유 → 회전해도 해소 안 됨.
- 따라서 **Key 롤링이 실질적으로 기여하는 시나리오는 #5(Key 자체 무효화/만료) 뿐**이다. `accessNotConfigured`(#6)는 기존(리서치 전)에 #5 와 묶여 "회전 유효"로 오분류되어 있었으나, 공식 문서 팩트체크로 프로젝트 단위가 확인되어 **회전 무효·즉시 skip** 로 재분류했다(§5.2).

> **⚠️ 쿼터 정책 제약 (공식 문서 기준):** 쿼터를 늘리기 위해 별도 GCP 프로젝트를 여러 개 만드는 행위는 YouTube 개발자 정책이 **"sharding"** 이라 부르며 **명시적 약관 위반**(적발 시 쿼터 삭감·Key 취소·계정 정지)이다. 정석은 **Quota Extension(API Compliance Audit)** 양식 신청이다. 단, 진짜 use case 가 다른 경우(iOS/Android 분리, prod/dev 분리 등)는 별도 프로젝트+Key 가 허용된다. 본 프로젝트는 하루 ~7 units 로 쿼터 소진 자체가 비현실적이므로 Extension 없이도 충분하다. 본 설계의 Key 풀은 #5 대응(예비 Key 보유)이 목적이지 쿼터 회피가 아니다.

IP 밴(#3)은 여전히 꼬리 케이스이며, Cloud Run 프록시가 벨트-서스펜더(2차)로 대응한다. 프록시를 설계하는 이유는 멘토가 명시적으로 요구한 주제이며, Cloud Run + NAT + K8s 차이를 설명할 수 있다는 점에서 포트폴리오/면접 가치가 크기 때문이다.

## 3. 설계 결정 요약

| 결정 | 선택 | 근거 |
|---|---|---|
| 복원력 범위 | 계층 방어: **재시도(1차)** → Key 롤링(#5 Key 무효화 대응으로 축소) → Cloud Run 프록시(IP 밴) → Circuit Breaker → skip+알림 | 멘토 "현업 수준 고민" 부합, 단계별 점진 도입 가능. 쿼터/레이트리밋은 프로젝트 단위라 회전 무효(§2 참조) |
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
   ├─③ Key 회전: keyInvalid/keyExpired/401 인증계열(Key 자체 무효, §2 #5)만 → 다음 키. accessNotConfigured(#6)·quotaExceeded(#1)·userRateLimitExceeded(#2)는 프로젝트 단위라 회전 무효(§2)
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

class YouTubeCallables(NamedTuple):
    """make_callables 반환 — 순서 실수를 타입으로 방지."""
    list_videos: Callable[..., dict]
    list_channels: Callable[..., dict]
    list_categories: Callable[..., dict]

class ResilientYouTubeClient:
    def __init__(
        self,
        keys: list[str],
        *,
        proxy_url: str | None = None,
        max_retries: int = 3,          # tenacity: 현재 Key+경로에 대한 backoff
        max_proxy_attempts: int = 2,   # 프록시 경로 재시도 상한
        max_total_calls: int = 60,     # 한 collection run 폭주 가드(videos+channels+categories 호출 합산 상한)
    ): ...

    def make_callables(self) -> YouTubeCallables:
        """fetch.collect_trending 이 기대하는 (list_videos, list_channels,
        list_categories) 반환. 각 callable 은 (**kw) -> dict."""
```

**호출 트리 (재시도 ↔ 회전 ↔ 전환 중첩 순서 — 핵심 명세):**

```
for resource in (videos, channels, categories):       # collect_trending 이 순차 호출
  call_with_resilience(resource, params):
    # ── 외곽: Key 회전 + 경로 전환 (상태는 per-run 공유) ──
    while not exhausted:
      key = pick_active_key()          # 무효화된 Key 제외
      route = pick_route()             # 정상(googleapiclient) or 프록시(proxy_url)
      try:
        # ── 내부: tenacity 는 "현재 Key + 현재 경로" 에만 backoff ──
        return tenacity_retry(lambda: invoke(key, route, params),
                              retry=retry_if_classified_backoff,
                              stop=stop_after_attempt(max_retries))
      except HttpError as e:
        verdict = classify(e)          # §5.2 분류표
        log(key_index, route, verdict, attempt)   # 관측성 (아래)
        apply_verdict(verdict, key, route)        # 상태 갱신/회전/전환/터미널
```

핵심 규칙:
- **tenacity 는 가장 안쪽** — "현재 Key + 현재 경로" 조합에 대해서만 backoff. 이 조합이 소진되면 예외를 밖으로 던져 회전/전환 루프가 처리. (tenacity 를 외곽에 두면 무효 Key 를 `max_retries` 회 반복 호출하는 낭비가 생긴다.)
- **IP 밴 시그니처·Circuit Breaker 상태는 per-run(클라이언트 인스턴스) 공유** — 한 run 내에서 한 번 전환/OPEN 이면 이후 모든 자원 callable 은 즉시 그 상태를 이어받는다. `collect_trending` 중간 배치(channels 2번째)에서 시그니처가 감지되면, 이미 수집한 items 는 유지한 채 이후 호출만 프록시 경로로 전환된다(재시작 아님).
- **`max_total_calls` 폭주 가드** — 어떤 경로든 총 호출 수가 상한을 넘기면 `CollectionExhausted` 로 단절(무효 Key 반복·시그니처 오판 루프 방지).

**proxy_url=None 일 때의 동작 (1차 PR):** 프록시 경로가 비활성이므로, IP 밴 시그니처가 감지돼도 **전환은 no-op** 이다. 즉 시그니처 감지 → Circuit Breaker 즉시 OPEN(프록시 시도 0회 숏트서킷) → `CollectionExhausted` → skip+알림. 1차 PR 은 "IP 밴 감지 능력은 갖되, 우회 수단(프록시)은 2차에 추가" 상태다. 로그 메시지는 "IP 밴 시그니처 감지, 프록시 미구성으로 skip" 로 명확히 안내.

**Key 상태 추적:** 무효화된 Key 만 in-memory 로 per DAG-run 유지(`quotaExceeded` 등은 프로젝트 단위라 Key별 소진 추적 불필요). 매일 새 프로세스이므로 영속 상태 불필요. **태스크 재시도(Airflow `retries`) 시 새 프로세스이므로 Key 무효화 상태도 초기화**된다 — 의도적(영구 무효 Key 는 재시도마다 1회 재검증됨; 무해하지만 비용임을 인지).

**관측성 (구조화 로깅 — 운영 필수):** 모든 복원력 의사결정 지점에서 구조화 로그를 남긴다: `key_index`(값 아님, 식별자만), `route`(normal/proxy), `verdict`(분류 결과), `attempt`(n/N), `action`(backoff/rotate/switch/skip). skip 시 그 원인(쿼터/IP밴/일시장애)을 로그·알림 메시지에 명시해 "정상 결측"과 "이상 실패"를 구분. **Key 값·Authorization/X-Goog-Api-Key 헤더·응답 본문 전체·raw 예외 traceback 은 절대 로그에 넣지 않는다**(§5.3 마스킹 정책).

### 5.2 에러 분류 규칙

| YouTube reason / 상황 | 분류 | 액션 |
|---|---|---|
| 403 `quotaExceeded` / 403 `dailyLimitExceeded` | **프로젝트** 일일 쿼터 소진 | 회전 **무효**(같은 프로젝트 Key 들은 쿼터 공유). 그날 그대로 **skip + 알림**. (본 프로젝트는 ~7 units/일이라 사실상 발생 불가) |
| 403 `userRateLimitExceeded` | **per-user** 레이트리밋(공식 문서 명시). API-key-only 컨텍스트에선 스코프 불명확 | **보수적으로 회전 무효** — 같은 Key 로 backoff. backoff 소진 후에도 지속 시 skip + 알림. (per-credential 으로 가정하고 회전하는 건 OAuth per-user 정의와 충돌해 위험) |
| 403·429 `rateLimitExceeded` / 403 `servingLimitExceeded`·`concurrentLimitExceeded`·`limitExceeded` | API/글로벌 레이트리밋 | backoff 후 **같은 Key** 재시도. 회전 무효. backoff 소진 후 skip + 알림 |
| 400 `keyInvalid` / 400 `keyExpired` | **Key 자체 무효/만료**(해당 Key 만의 문제) | 풀에서 영구 제외 → **다음 Key 로 회전(유일한 유효 회전 시나리오)** |
| 401 `unauthorized`·`authError`·`required`·`expired` | 인증/Key 헤더 문제 | Key 무효화 취급 → 회전 |
| 403 `accessNotConfigured` | **프로젝트 스코프** — 공식 문서 "your project is not configured to access this API". 같은 프로젝트 다른 Key 도 똑같이 실패 | 회전 **무효** → 즉시 터미널 **skip + 알림** (프로젝트 설정 문제) |
| 5xx(`internalError`/`backendError`/`notReady`), `TimeoutError`, `ConnectionError`, DNS `gaierror`, `SSLError` | 일시적(전역 상류 장애 후보) | backoff 재시도 (같은 Key). 모든 Key·backoff 소진 후에도 지속되면 **"일시 장애"로 skip (프록시 전환 없이)** — 상류 장애는 프록시로도 해결 불가 |
| reason 파싱 불가(malformed JSON / 미정의 reason) | 알 수 없음 | **기본 정책 = 일시적 취급** → backoff 재시도, 소진 시 skip |
| 한 collection run 내 **모든 활성 Key 가 403(suspension/forbidden 계열)으로 동일 실패** | **IP 밴 시그니처** | Cloud Run 프록시 경로로 전환. **시그니처 엣지 규칙(아래)** 참조. 403 계열만 시그니처로 인정 (5xx/네트워크는 IP 밴 아님) |
| 프록시 경로도 `max_proxy_attempts` 회 실패 (또는 `proxy_url=None`)| Circuit Breaker OPEN | `CollectionExhausted` raise |

**판정 방식:** `googleapiclient.errors.HttpError` 의 `resp.status` 와 본문 `error.errors[].reason` 파싱. 프록시 경로(`requests`)는 응답 JSON 에서 동일 `error.errors[].reason` 파싱 로직을 재사용(스키마 일관성).

**IP 밴 시그니처 엣지 규칙 (모호함 제거):**
- **최소 Key 수 ≥ 2**: 활성 Key 가 1개뿐이면 "전 키 동일 403" 이 1회 403 으로 성립해 버리므로, 단일 403 의 IP밴 오판을 막기 위해 활성 Key 가 2개 이상일 때만 시그니처로 인정. Key 1개일 때 403 `keyInvalid` 면 회전 후보가 없어 그냥 `CollectionExhausted` (IP밴 아님).
- **"동일(identical)"의 정의**: reason 문자열이 정확히 같아야(또는 status 403 + suspension/forbidden 계열 reason). 일부 Key 만 403이고 다른 Key 는 성공(또는 다른 결과)이면 부분 성공 → **시그니처 불성립**.
- **reason 파싱 실패 시**: 403 이되 reason 을 못 읽으면 #7(일시적) 기본 정책으로, 시그니처에 카운트하지 않는다(오판 방지).
- **per-run 집계**: 시그니처 판정은 한 collection run(클라이언트 인스턴스) 내에서만.

### 5.3 Cloud Run 프록시 계약 (dumb forwarder)

프록시는 상태를 가지지 않는 단순 전달자다. Key 는 collector 가 **HTTP 헤더 `X-Goog-Api-Key`** 로 붙여 보내며, 프록시는 헤더를 googleapis 로 그대로 전달만 한다 (Key 를 다루지 않음). 헤더 방식을 쓰는 이유: query string `key=` 는 Cloud Run access/request logs 에 평문으로 남기 때문이다.

> **⚠️ 헤더 방식의 두 가지 미해결 리스크 (2차 PR 전 해결 필수):**
> 1. **YouTube Data API v3 의 `X-Goog-Api-Key` 헤더 공식 지원이 미입증**. 공식 credentials 문서는 `key` query param 만 설명한다(일부 Google API — Gemini/Maps — 는 헤더를 지원하지만 YouTube Data API v3 에 대한 문서화를 찾지 못함). **2차 PR 배포 전 `curl -H "X-Goog-Api-Key: ..." https://www.googleapis.com/youtube/v3/...` 로 실동작을 반드시 검증**. 작동하지 않으면 아래 대안 (a) 로 전환.
> 2. **"헤더는 기본 로깅에서 노출 안 된다"는 단정은 과소평가**. APM/트레이싱(Datadog/Sentry/OpenTelemetry), FastAPI/uvicorn 예외 핸들러의 에러 리포트, `tenacity` 재시도 중 `HttpError` 의 `repr()`/traceback, `requests` 예외 메시지(URL 포함) 등에 헤더가 캡처될 수 있다. 따라서 §5.1 의 로깅 마스킹 정책(Key/헤더/본문 전체·raw traceback 금지, `key_index` 식별자만)이 헤더 방식의 **필수 짝**이다.

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
- `proxy_url` 이 `None` 이면 프록시 계층 비활성 → 재시도 + 롤링 + skip 만 동작(§5.1 의 proxy_url=None 숏트서킷). **점진적 도입 가능** (1차: client.py만, 2차: 프록시 배포 후 환경변수 주입).

**Key 전달 트레이드오프 (PR 리뷰 + 팩트체크 반영):** dumb forwarder 원칙(Key 안 다룸)을 유지하되 Key 노출을 줄이기 위해 헤더(`X-Goog-Api-Key`) 방식을 1차 선택했다. 검토한 대안:
- **(a) 프록시가 Secret Manager 에서 Key 를 직접 읽기** — egress 경로에 Key 가 아예 안 나가 모든 헤더 노출 경로를 근본 소거. 단 dumb forwarder 원칙을 포기하고 프록시에 상태·권한·Key 회전 책임이 추가된다. **위 헤더 미입증 리스크가 실동작 테스트에서 확인되면 (a) 를 1순위로 전환**한다(보안 관점에서도 (a) 가 가장 견고).
- (b) Cloud Run access log 의 query string 마스킹 — 로깅 정책에 의존해 덜 근본적.
- 본 설계는 헤더 방식을 1차 기본으로 하되, 2차 구현 시 (a) 의 복잡도-보안 트레이드오프를 재검토한다.

Cloud Run IP 회피의 정직한 한계:
- "재시작 = 무조건 다른 IP" 가 아님. Google 관리 풀에서 **확률적 회전**이다.
- Cloud Run egress 는 Google 데이터센터 IP 대역이라 레지덴셜 IP가 아님.
- 진짜 핵심 효과는 "GKE NAT IP 가 밴 먹었을 때 Cloud Run 은 **다른 egress 경로**라 우회가 통한다"는 **경로 분리**다. (K8s 파드 재시작은 노드/NAT IP 가 안 바뀌어 효과 없음 — NAT/내부IP/외부IP/공유기 구조와 연관.)

### 5.4 Circuit Breaker / 폴백 (skip + 알림)

**Circuit Breaker 상태머신 (1차 단순 이항 — 복구 메커니즘은 매일 새 run 으로 대체):**

| 상태 | 진입 조건 | 동작 |
|---|---|---|
| **CLOSED** | (초기) | 정상 호출 |
| **OPEN** | (a) 프록시 경로 `max_proxy_attempts` 회 실패, 또는 (b) `proxy_url=None` 인데 IP 밴 시그니처 감지(숏트서킷: 프록시 시도 0회), 또는 (c) `max_total_calls` 초과 | 이후 모든 callable 즉시 `CollectionExhausted` raise |

1차는 HALF-OPEN(시도적 복구)이 의미 없다 — 매일 새 DAG run 이 새 클라이언트 인스턴스(=자동 CLOSED 리셋)이므로. 복구는 "다음 날"이 곧 reset 이다.

**`CollectionExhausted` 가 raise 되면 DAG 태스크는 실패.** 핵심은 이 터미널 예외를 Airflow 재시도 루프에서 빼내는 것이다:

**DAG 수정 (의사코드):**
```python
# dags/youtube_trending_kr_daily.py — snapshot 태스크 본문
from airflow.exceptions import AirflowFailException
from autoresearch.youtube_collection.client import ResilientYouTubeClient, CollectionExhausted

@task
def snapshot(ds):
    client = ResilientYouTubeClient(keys=_load_keys(), proxy_url=_get_proxy_url())
    callables = client.make_callables()
    try:
        videos = collect_trending(*callables, snapshot_date=ds)   # fetch.py (변경 없음)
    except CollectionExhausted as e:
        # 터미널: 모든 Key·프록시 소진. 재시도 무의미 → 즉시 failed 로 승격.
        raise AirflowFailException(f"유튜브 수집 폭주 — skip: {e}") from e
    write_partition(videos, dt=ds)
```

- **재시도 금지(터미널만)**: 현재 DAG `default_args={"retries": 2}` 로 일시적 실패(5xx 등)는 재시도하지만, `CollectionExhausted` → `AirflowFailException` 승격으로 `retries` 와 무관하게 즉시 `failed`. (`fetch.py` 는 예외를 잡지 않고 그대로 전파한다 — 확인 완료. 태스크 `state` 직접 조작 안티패턴·`retries=0` 오버라이드는 쓰지 않는다: 후자는 일시적 5xx 재시도까지 같이 제거되기 때문.)
- Airflow 내장 알림(email/on-call)이 발생 → 그날 partition 은 미생성.
- 멱등 설계(`write_partition` 은 dt 단위 덮어쓰기)이므로 다음 날 수집에 영향 없음.

**데이터/ML 결측 비용 (자원 배분 근거):** 트렌딩은 **시점 신호**라 결측은 backfill 불가(영구). 또한 `view_count` 증분(velocity)·"트렌딩 체류일수" 같은 연속성 피처는 하루 결측이 **전후 2일**의 차분을 깨먹는다. 그래서 "결측을 줄이는" 방어가 가장 가치 있고, 그건 **1차의 tenacity(5xx 회복)** 이다. 반대로 프록시(IP밴 꼬리 대응)는 빈도가 훨씬 낮아 2차의 가치는 상대적으로 낮다. 즉 데이터/ML 비용 구조 관점에서도 **1차(tenacity) 고가치·2차(프록시) 저가치** 로 자원 배분이 정렬된다. 다만 하루 결측 자체는 수백 일 학습 데이터에서 무시 가능한 잡음이므로, skip+알림 폴백이 ML 품질을 무너뜨리지 않는다.

## 6. 설정 / Key 관리

| 항목 | 운영(prod) | 로컬 개발 |
|---|---|---|
| API Key 풀 | Google Secret Manager (복수 secret 또는 1개 JSON) | 환경변수 `YOUTUBE_API_KEYS=key1,key2,...` |
| 프록시 URL | 환경변수 / Airflow Variable `YOUTUBE_PROXY_URL` | 미설정(프록시 비활성) |
| GCS 버킷 | 기존 `YOUTUBE_LAKE_BUCKET` 유지 | 동일 |

Key 는 Secret Manager, collector(`client.py`)가 런타임에 로드. **프록시 경로에서는 query string 이 아닌 `X-Goog-Api-Key` 헤더로 전달**을 지향하되(자세한 트레이드오프·미해결 리스크는 §5.3), 헤더 노출 경로가 넓으므로 §5.1 의 로깅 마스킹 정책이 필수 짝이다.

**설정 위생 (1차 PR 에 동기화):**
- `.env.example`: 현재 `YOUTUBE_API_KEY`(단수)만 있음 → `YOUTUBE_API_KEYS=`(복수, 쉼표 구분)·`YOUTUBE_PROXY_URL=` 추가, 단수 키는 deprecated 표시.
- `.gitignore`: 현재 `.env` 한 줄만 → `.env*`(또는 `.env.local`/`.env.*` 변형) 패턴 추가, `!.env.example` 예외. (gitleaks pre-commit 훅으로 이중 방어 권장.)
- 환경변수 Key 풀 노출 경로(`ps`/envdump/자식 상속/core dump)는 prod 에서 Secret Manager 사용으로 회피; 로컬도 `.env` 파일 헬퍼로 읽고 자식 spawn 시 env 필터링을 권장.

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

- **`client.py` 단위테스트**: 가짜 `service` callable 이 상황별로 예외를 던지도록 주입. Key 회전·프록시 전환·Circuit Breaker 동작을 모두 가짜 데이터로 검증. (기존 `fetch.py` 테스트 패턴과 동일 — googleapiclient Mock 불필요.)
- **`fetch.py` 회귀**: 변경 없음을 확인.
- **프록시 통합테스트**: 로컬 Docker 로 프록시를 띄우고, query forward / health 응답 전환 / restart 유도를 검증. **2차 미배포에 대비해 1차 PR 에도 로컬 Docker 통합테스트를 포함**해 "프록시가 동작한다"는 실행 가능한 증거를 남긴다.
- **CI**: 기존 `.github/workflows/ci.yml`(pytest 3.11/3.12 + Docker 빌드)이 통과해야 함.

**단위테스트 시나리오 체크리스트 (각 분기를 가짜 callable 로 주입해 검증):**

| 시나리오 | 검증 포인트 |
|---|---|
| 403 `quotaExceeded` | 회전 없이 즉시 skip + 알림 (Circuit Breaker OPEN 경유) |
| 403 `userRateLimitExceeded` | 회전 없이 같은 Key backoff |
| 403 `keyInvalid` → 다음 Key 200 | Key 무효화 마킹 + 회전 성공 |
| 403 `accessNotConfigured` | 회전하지 않고 즉시 skip (프로젝트 단위 정정 확인) |
| 400 `keyExpired` | keyInvalid 와 동일 취급(회전) |
| 5xx backoff 후 성공 | tenacity 재시도 후 정상 복귀 |
| 5xx backoff 전부 소진 | "일시 장애" skip (프록시 전환 X) |
| DNS `gaierror` / `SSLError` | 일시적 취급, backoff |
| reason 파싱 실패(malformed JSON) | 일시적 기본 정책 → backoff, 시그니처 미카운트 |
| **IP 밴 시그니처(전 Key 동일 403) + `proxy_url` 있음** | 프록시 경로 전환 |
| **IP 밴 시그니처 + `proxy_url=None`** | 숏트서킷 → 즉시 CollectionExhausted (전환 시도 X) |
| **활성 Key 1개 + 403** | 시그니처 미성립 (최소 Key≥2 규칙) |
| **부분 성공(Key1=403, Key2=200)** | 시그니처 불성립 |
| **중간 배치 전환**(channels 2번째 호출에서 시그니처) | 이미 수집한 items 유지, 이후만 프록시 경로 |
| `CollectionExhausted` → `AirflowFailException` 승격 | DAG 레벨: 터미널이 재시도 루프에 안 걸림 |
| `max_total_calls` 폭주 가드 | 상한 도달 시 CollectionExhausted |

## 9. 인프라 조율 사항 (인프라 담당 / Terraform)

- Cloud Run 프록시 서비스 Terraform 추가 (별도 issue 권장 — 본 PR 범위와 분리).
- Secret Manager: YouTube API Key secret 생성 + collector GKE 서비스계정에 Secret Accessor 권한.
- 프록시 URL 을 collector(Airflow)에 환경변수/Variable 로 주입.
- GKE NAT IP 와 Cloud Run IP 가 서로 다른 egress 경로임을 인프라 관점에서 확인.

## 10. 점진적 도입 순서 (권장)

1. **1차 PR**: `client.py` (재시도 + Key 롤링 + Circuit Breaker) + DAG 교체 + 단위테스트(§8 체크리스트) + 로컬 Docker 프록시 통합테스트. `proxy_url=None`. 현실적 실패(쿼터/레이트리밋/5xx)의 대부분 즉시 해소. 함께:
   - 설정 위생: `.env.example` 동기화(`YOUTUBE_API_KEYS`·`YOUTUBE_PROXY_URL`), `.gitignore` `.env*` 패턴(§6).
   - CI 보강(권장): `.github/workflows/ci.yml` 에 의존성 감사(`pip-audit`/`safety`) + 시크릿 스캔(`gitleaks`) 스텝 추가, 의존성 상한 핀(`pydantic>=2.6,<3` 식)/lockfile 도입 검토.
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
- "Circuit Breaker 패턴이 왜 필요한가? (이 규모에선 본질적으로 '전부 소진 시 터미널 예외 승격'인데 왜 패턴 이름을 붙였나?)"
- "Key 롤링은 어느 실패에만 기여하는가? 왜 `quotaExceeded`/`userRateLimitExceeded`/`accessNotConfigured` 에는 회전이 무효한가?(쿼터의 프로젝트 단위 할당 · sharding 금지 · `accessNotConfigured` 의 프로젝트 스코프)"
- "왜 복원력 로직을 DAG 가 아니라 별도 모듈(`client.py`)로 뽑았는가?(테스트용이성/책임분리)"
- **"하루 7 units 규모에 Cloud Run 프록시·Circuit Breaker까지 한 이유는?"** — 가장 강한 답: "이 규모의 엔지니어링 정답은 tenacity + keyInvalid 롤링 + skip + 알림(+구조화 로그) 입니다. 프록시 계층은 학습/포트폴리오 목적과 멘토 요구로 추가한 의도적 오버엔지니어링이며, 이 방어가 정당화되는 임계점(볼륨 증가·반복 IP밴)도 설명할 수 있습니다." 이 **자기 인식**이 과잉 시스템 자체보다 훨씬 인상적인 답이다.
- "Cloud Run liveness 재시작 루프의 함정은? (재시작 throttle, liveness를 응답코드에 직결하면 안 되는 이유)"
