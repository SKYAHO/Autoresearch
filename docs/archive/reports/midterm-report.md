# 중간발표: YouTube 수집 복원력 레이어

> 담당 도메인: Airflow Orchestration + YouTube 수집
> 작업 기간: 2026-07-03 ~ 2026-07-09
> 산출물: PR #56(머지), PR #48(리뷰 중), PR #77(리뷰 중), 이슈 #60(백로그)

---

## 1. 개요

Autoresearch 프로젝트는 YouTube 트렌딩 데이터를 수집해 CTR 모델링의
피처/라벨로 활용하는 파이프라인입니다. 매일 Airflow DAG이 YouTube Data API v3로
트렌딩 영상 메타데이터를 수집해 GCS 데이터 레이크에 적재합니다.

제가 담당한 영역은 이 수집 단계의 **신뢰성**입니다. YouTube API는 일일 쿼터, Key
무효화, IP 밴 등 다양한 실패 모드를 가지며, 기존 구현은 단일 Key로 동작해 한 번
실패하면 그날 수집이 전부 중단되는 취약점이 있었습니다. 이를 복원력 레이어로
보완하고, 설계 근거를 문서화하며, 실제 환경에서 동작을 실증한 작업을 정리합니다.

## 2. 문제 배경

YouTube Data API v3 수집 실패의 주요 원인:

| 실패 유형 | 특성 | 기존 대응 |
| --- | --- | --- |
| 일일 쿼터 초과 (`quotaExceeded`) | 프로젝트 단위, 24시간 후 갱신 | 전체 중단 |
| Key 무효화 (`keyInvalid`/`keyExpired`) | Key 단위, 회전으로 해결 | 전체 중단 |
| 프로젝트 설정 문제 (`accessNotConfigured`) | 프로젝트 스코프, 회전 무효 | 전체 중단 |
| IP 밴 (추정) | egress IP 단위, 회전으로 해결 가능 | 대응 없음 |

기존 구현의 문제:
- 단일 Key — 무효화 시 즉시 전체 수집 중단
- 부분 실패 허용 없음 — 일부 자원 실패가 전체를 맘
- IP 밴 대응 없음 — 같은 egress IP로 재시도해도 계속 실패
- 운영 알람 정책 부재 — 쿼터 소진(예상 가능)과 진짜 장애를 구분 못 함

## 3. 해결 설계 (PR #48)

### 3.1 4단계 복원력 방어 계층

수집 호출 한 번이 실패했을 때, 점진적 강도의 4단계로 복구를 시도합니다:

```
1단계 재시도 (BACKOFF)  ──실패──▶  2단계 Key 롤링 (ROTATE)
        │                                  │
        ▼ 실패                             ▼ 전 Key 동일 실패
4단계 Circuit Breaker  ◀──실패──  3단계 프록시 (IP 회전)
```

- **1단계 재시도**: tenacity BACKOFF로 일시적 5xx/타임아웃 흡수
- **2단계 Key 롤링**: `keyInvalid`/`keyExpired`/401 → 다음 Key로 교체
- **3단계 프록시**: 전 활성 Key가 동일 403(IP밴 시그니처) → Cloud Run 프록시로
  egress IP 전환
- **4단계 Circuit Breaker**: 반복 실패 시 해당 경로 차단, 폭주 가드

### 3.2 에러 분류

API 응답을 5종 `Verdict`로 분류해 각 단계의 동작을 결정합니다:

| Verdict | 의미 | 동작 |
| --- | --- | --- |
| `ROTATE` | Key 무효 (keyInvalid 등) | 다음 Key로 교체 |
| `BACKOFF` | 일시적 (5xx, rateLimit) | 지수 백오프 재시도 |
| `TERMINAL_QUOTA` | 일일 쿼터 소진 | 즉시 종료 (회전 무효) |
| `TERMINAL_CONFIG` | accessNotConfigured | 즉시 종료 (프로젝트 스코프) |
| `IP_BAN_CANDIDATE` | IP밴 시그니처 | 프록시 경로로 전환 |

핵심 설계 결정:
- `accessNotConfigured`는 프로젝트 스코프 → 회전 무효, 즉시 skip
- `userRateLimitExceeded`는 스코프 불명확 → 보수적으로 회전 무효
- IP밴 시그니처 = "전 활성 Key가 동일 403" (최소 Key ≥ 2일 때만 판정)

### 3.3 ADR 0001: 프록시 목적/범위

`docs/adr/0001-youtube-proxy-purpose.md`에 설계 결정을 기록했습니다:

- 프록시는 **학습 + 범용 egress seam** 목적 (IP밴은 꼬리 케이스)
- Cloud Run 배포/Terraform은 3차로 보류 (컨테이너 코드만 먼저)
- 운영 환경(7 units/day)에서 IP밴 발생 확률은 매우 낮음

## 4. 구현 (PR #56)

> PR #56: `feat: 유튜브 수집 복원력 client.py + Cloud Run 프록시 (이슈 #47)`
> main 머지 완료 (커밋 `d9e7532`), +4,030/-44, 17파일, 151 테스트 통과

### 4.1 client.py 복원력 레이어 (591줄)

`autoresearch/youtube_collection/client.py`의 핵심 컴포넌트:

- `ResilientYouTubeClient` — 4단계 방어 총괄
- `_classify_error(status_code, reason) -> Verdict` — 순수 함수, 테스트 용이
- `_pick_proxy_key()` — 프록시 경로 Key 선택 (normal route와 분리)
- `_call_via_proxy()` — Cloud Run 프록시 경유 호출
- `_service_cache` — per-Key 서비스 객체 memoization
- Circuit Breaker — once-True-stays-True unhealthy (Cloud Run 재시작 유도)

**보안 불변량 (로깅 마스킹)**:
- 허용: `key_index` (0, 1, 2...)
- 금지: API Key 값, 헤더, 본문, traceback, URL 전체
- `from None` 체인 scrub — proxy URL/credentials 노출 방지

### 4.2 Cloud Run 프록시 (proxy/app.py, 82줄)

`dumb forwarder` 설계 — 비즈니스 로직 없이 egress IP만 전환:

- 업스트림 호스트 화이트리스트: `www.googleapis.com` 만
- path escape 차단 (`..` 방지)
- `key=` query parameter 거부 — `X-Goog-Api-Key` 헤더만 허용
- `/health` — unhealthy 임계(3회) 도달 시 503 반환
- once-True-stays-True unhealthy — Cloud Run liveness가 재시작 → 새 egress IP

### 4.3 DAG 통합

`dags/youtube_trending_kr_daily.py`:
- `_load_keys` / `_build_client` 헬퍼로 복원력 클라이언트 주입
- `CollectionExhausted` → `AirflowFailException` 승격

> **후속 작업 (이슈 #60)**: 현재는 모든 `CollectionExhausted`를 hard fail로
> 처리합니다. 쿼터 소진/BACKOFF 소진 등 예상 가능 종료는 `AirflowSkipException`으로
> 분기해 알람 피로를 줄이는 정책 개선을 설계 중입니다.

## 5. 실증 테스트 (PR #77)

> PR #77: `IP밴 실증 테스트 결과 (#76)`, 리뷰 중
> 별도 GCP 프로젝트 `autoresearch-501004`에서 진행 (운영 무영향)

### 5.1 목적

- IP밴 회전이 실제로 작동하는지 팀원 궁금증 해소
- 복원력 레이어의 종단 동작 검증
- ADR 0001 가설(정상 사용 환경에서 IP밴 발생 안 함) 실증

### 5.2 방법

3단계로 진행:

1. **Phase 1**: Cloud Run에 프록시 배포, egress IP₁ 관측
2. **Phase 2**: 정상 API Key로 수집 정상 동작 확인
3. **Phase 3**: quota 폭주(`quota_burst.py`, 50 workers 병렬 10,000호출)로
   IP밴 유도 시도 → 감지 시 revision 재배포로 IP₁→IP₂ 회전 증명

### 5.3 결과: IP밴 미발생

```
stats={200: 8117, 403: 1870, network_error: 13}
reasons={quotaExceeded: 1870}
IP밴 시그니처: 0건
```

### 5.4 3가지 핵심 발견

1. **quota 폭주로는 IP밴 유도 불가** — YouTube는 quota 초과 시 IP밴 대신
   `quotaExceeded`만 반환. 두 메커니즘은 별개.
2. **quota 초과 시 복원력 레이어 정상 동작** — `TERMINAL_QUOTA` verdict 감지 →
   즉시 `CollectionExhausted` 승격, 회전 없이 종료 (의도된 최적화)
3. **정상 사용 환경(7 units/day)에서 IP밴 발생 안 함** — ADR 0001 가설 실증.
   일일 약 8,117 units quota 대비 7 units/day 사용량으로는 IP밴 도달 불가.

회전 증명은 IP밴 미발생으로 생략했습니다. 환경 정리(Cloud Run 삭제, 임시 코드
revert) 완료했습니다.

## 6. 후속 작업

### 이슈 #60: Minor 백로그 (6항목)

PR #56 구현/리뷰 중 식별했지만 핵심 동작과 무관해 후속으로 미뤄둔 품질 항목:

1. `_proxy_attempts` dead state 제거/활성화 (`client.py:267`)
2. `_breaker_open` 상태 persist 정책 결정 (`client.py:263,346`)
3. proxy 네트워크 예외 즉시 포기 개선 (`client.py:542-549`)
4. multi-value query 검증 강화 (`proxy/app.py`)
5. 3xx 리다이렉트 처리 정책 (`proxy/app.py`)
6. 테스트 커버리지 gap 보강 (`tests/`)

### skip/fail 정책 개선

리뷰어 질문으로 제기된 `CollectionExhausted`의 skip/fail 분기 정책:
- `ExhaustionKind` enum (SOFT=skip / HARD=fail) 추가 설계 완료
- SOFT: BACKOFF 소진, QUOTA 소진, 프록시 재시도 소진
- HARD: Circuit Breaker OPEN, 활성 Key 없음, IP밴+proxy None, 알 수 없는 verdict
- DAG에서 `AirflowSkipException` / `AirflowFailException` 분기

### 3차: Cloud Run 배포 / Terraform

ADR 0001로 보류 명시. 인프라 담당 팀원(hyeongyu-data)에게 Terraform 추가 요청
완료(요청서 작성). 컨테이너 코드(`proxy/`)는 PR #56에 머지됐으나, 빌드/배포
파이프라인은 인프라 담당자 영역.

---

## 부록

### A. 파일 목록

| 파일 | 역할 | 상태 |
| --- | --- | --- |
| `autoresearch/youtube_collection/client.py` (591줄) | 복원력 클라이언트 | main 머지 |
| `proxy/app.py` (82줄) | Cloud Run dumb forwarder | main 머지 |
| `proxy/Dockerfile` (21줄) | 컨테이너 이미지 | main 머지 |
| `dags/youtube_trending_kr_daily.py` | DAG (복원력 클라이언트 통합) | main 머지 |
| `tests/test_youtube_client.py` (753줄) | client.py 테스트 | main 머지 |
| `tests/test_proxy_app.py` (194줄) | proxy/app.py 테스트 | main 머지 |
| `tests/test_proxy_docker.py` (79줄) | 컨테이너 통합 테스트 | main 머지 |
| `docs/adr/0001-youtube-proxy-purpose.md` | 프록시 목적/범위 ADR | main 머지 |
| `docs/archive/specs/2026-07-03-youtube-ip-ban-resilience-design.md` | 설계 문서 | PR #48 리뷰 중 |
| `docs/archive/specs/2026-07-07-youtube-resilience-empirical-test.md` | 실증 테스트 설계 | PR #77 리뷰 중 |
| `docs/archive/plans/2026-07-07-youtube-resilience-empirical-test.md` | 실증 테스트 실행 계획 | PR #77 리뷰 중 |
| `docs/archive/reports/2026-07-07-empirical-test-run-log.md` | 실증 테스트 실행 로그 | PR #77 리뷰 중 |
| `scripts/empirical_test/quota_burst.py` | IP밴 유도 스크립트 | PR #77 리뷰 중 |

### B. ADR 0001 요약

- **결정**: Cloud Run 프록시는 학습 + 범용 egress seam 목적. IP밴 대응은 꼬리 케이스.
- **이유**: 운영 환경(7 units/day)에서 IP밴 발생 확률 매우 낮음. 복원력 1-2단계
  (재시도 + Key 롤링)가 90%+ 실패 모드 커버.
- **검증**: 2026-07-05 임시 GCP 프로젝트에서 수동 revision 배포 시 새 egress IP
  관측, YouTube API 200 성공. 자동 IP 회전은 throttle 위험으로 미검증.
- **보류**: 3차 배포(Cloud Run/Terraform)는 인프라 담당자 영역으로 이관.

### C. 실증 테스트 데이터

**폭주 실행** (50 workers 병렬, 약 2.5분):
- 총 호출: 10,000
- 성공(200): 8,117
- quota 초과(403 quotaExceeded): 1,870
- 네트워크 오류: 13
- IP밴 시그니처(403 비-quota): 0
- quota 소진 시점: 약 8,117번째 호출

**quota 소진 상태에서 client.py 동작**:
- verdict: `terminal_quota`
- route: `normal` (proxy_url 설정했어도 normal route에서 먼저 감지)
- 동작: 회전 없이 즉시 `CollectionExhausted` 승격
- 마스킹: `key_index`만 노출, Key값/헤더/본문/URL 미노출 ✅

### D. 제약 사항

- `fetch.py` 변경 금지 (팀 컨벤션)
- 한국어 docstring / 영어 식별자
- 커밋 메시지: `<type>: <한국어 설명> (#이슈)`
- 로깅 마스킹 불변량: key_index만 허용, 나머지 전부 금지
- CI는 팀원 영역 (CI 감사 잡은 revert됨)
