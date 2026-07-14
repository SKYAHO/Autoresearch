# YouTube 복원력 실증 테스트 설계

> Version: 1.0.0 | Date: 2026-07-07 | Status: Draft

## 배경 및 목적

PR #56(feat/47-youtube-resilient-client)은 YouTube Data API v3 수집 실패/IP밴
대응 복원력 레이어를 구현했다(재시도 → Key 롤링 → 프록시 → Circuit Breaker).
팀원들의 요청으로, 이 복원력 메커니즘이 **실제로 작동하는지** 실증하기 위한
테스트를 별도 GCP 프로젝트에서 진행한다.

이 테스트는 운영 환경(`ar-infra-501607`)에 영향을 주지 않도록 격리된
프로젝트(`autoresearch-501004`)에서 수행된다.

### 핵심 질문

1. quota/rate limit 초과 시 client.py 처리(`TERMINAL_QUOTA`/`BACKOFF`)가
   실제 API로 작동하는가?
2. IP밴(또는 이에 준하는 응답 패턴) 발생 시 **egress IP 회전**이
   작동하는가?
3. client.py의 IP밴 시그니처 → Circuit Breaker → 프록시 전환 로직이
   의도대로 동작하는가?

### 접근 방식

**접근 B**: 정상 Key로 quota 한계까지 폭주 요청 + 비공식 패턴(인증 없는 직접
호출 등) 병행으로 IP밴 유도 시도. IP밴 감지 시 Cloud Run revision 재배포로
egress IP 변화를 관측하여 회전을 증명한다.

접근 B를 선택한 이유: 팀원들이 "실제로 IP밴이 발생하고 회전이 작동하는지"
직접 확인하기를 원했다. quota 실증만으로는 회전 메커니즘의 라이브 증명이
부족하다는 판단.

## 환경

| 항목 | 값 |
| --- | --- |
| GCP 프로젝트 | `autoresearch-501004` ("Autoresearch", #990733524187) |
| 운영 프로젝트 | `ar-infra-501607` (영향 받지 않음) |
| 인증 계정 | 활성 계정(접근 권한 확인됨) |
| YouTube Data API v3 | ENABLED |
| Cloud Run 서비스 | 현재 0개 (Phase 1에서 배포) |
| 일일 quota | 기본 10,000 units (videos.list = 1 unit/호출) |

## Phase 1: Cloud Run proxy 배포

### 목적

회전/프록시 전환 검증을 위해 이 프로젝트에 proxy 컨테이너를 배포한다.
현재 Cloud Run 서비스가 없으므로 신규 배포가 필요하다.

### 배포 사양

- **소스**: `proxy/Dockerfile` (FastAPI dumb forwarder, 포트 8080)
- **서비스명**: `youtube-proxy`
- **리전**: `asia-northeast3` (서울)
- **인증**: Cloud Run 기본(authenticated, IAM invoker)
- **빌드**: `gcloud run deploy youtube-proxy --source proxy/ --region asia-northeast3`
- **메모리/CPU**: 기본값 (256MiB / 1 vCPU 제안)
- **최소 인스턴스**: 0 (비용 절감)
- **최대 인스턴스**: 3

### 배포 후 확인

1. `/health` 엔드포인트 → 200 응답
2. **첫 번째 egress IP 기록(IP₁)**: Cloud Run 서비스의 egress IP 확인
3. proxy 경유 요청이 `www.googleapis.com`으로 정상 forward되는지 확인

### proxy 동작 요약

`proxy/app.py`(82줄)는 dumb forwarder:
- `UPSTREAM_HOST=https://www.googleapis.com`
- `/youtube/v3/{rest_path}` — `X-Goog-Api-Key` 헤더 필수, path escape 차단,
  `key=` query 거부
- `/health` — unhealthy(연속 3회 5xx) 시 503
- once-True-stays-True unhealthy (Cloud Run 재시작 유도)

## Phase 2: 정상 작동 확인

### 목적

극단 시나리오(IP밴 유도)에 앞서, 시스템이 정상 조건에서 의도대로
작동하는지 확인한다.

### 확인 항목

1. **Key 유효성**: 이전 API Key(이 프로젝트의 Key)로 직접 YouTube API
   요청 → 200 응답 확인
2. **client.py 정상 동작**: 정상 Key + `proxy_url`(Cloud Run URL)로
   client.py 구성 후 요청 → 200 확인
3. **proxy 경유 정상 동작**: client.py가 proxy를 통해 YouTube API 호출 →
   200 확인
4. **회전 로직**(선택): 여러 Key를 쓸 경우 `ROTATE` 처리 확인

### 필요 입력

- 이전 API Key 값 (사용자 제공)

## Phase 3: IP밴 유도 시도

### 목적

IP밴(또는 이에 준하는 응답 패턴)을 유도하여 회전 메커니즘이 작동하는지
관측한다.

### 제약 인식

- **quota 상한**: 일일 10,000 units가 IP밴 유도의 상한. quota 초과(403
  `quotaExceeded`) 후에는 정상 API 호출이 막히므로, IP밴 유도를 위한
  "요청 폭주"를 계속할 수 없다.
- **비결정성**: Google이 IP밴 기준을 공개하지 않으므로, 시도해도 IP밴이
  안 걸릴 수 있다.
- **정상 Key 환경**: 공식 API Key 기반 정상 사용자에게 IP밴은 ADR 0001의
  전제대로 꼬리 케이스다.

### 시도 순서

1. **정상 Key 폭주**: quota 한계까지 단기 대량 요청
   - IP밴 시그니처(전 활성 Key 동일 403) 감지
   - 응답 패턴 변화(403 비율, reason 분포) 관측
2. **비공식 패턴 병행**: 인증 없는 직접 HTTP 호출 등
   - quota 카운트에 영향을 주지 않는 경로
   - 단, 응답이 400/403 `keyInvalid`에 그칠 수 있어 IP밴 유발 보장 없음
3. **계속 시도**: 위 방법을 교차/반복
4. **IP밴 안 걸리면**: 사용자에게 보고 → 사용자가 다음 결정(추가 시도 /
   접근 A 폴백 / 데모 취소 등)

### IP밴 감지 기준

다음 중 하나 이상 관측 시 "IP밴 발생"으로 판정:
- client.py IP밴 시그니처: 전 활성 Key에서 동일 403 (최소 Key ≥ 2)
- 응답 본문/헤더에서 IP 단위 제재 시사 패턴
- quota/rate limit과 무관한 403 연속 발생

### IP밴 감지 시 증명 절차

1. Cloud Run revision 재배포 (트래픽 100% 신규 revision)
2. **egress IP 변화 관측(IP₁ → IP₂)**
3. IP₂에서 YouTube API 요청 → 200 회복 확인
4. client.py의 회전/프록시 전환 로직 동작 관측
5. 회전 증명 완료

### 실패 시 폴백

IP밴이 끝내 유도되지 않으면, 사용자 보고 후 다음 중 선택:
- 접근 A로 전환: quota 초과 실증 + 회전은 revision 배포로 별도 증명 +
  IP밴/프록시는 단위테스트 시연
- 데모 취소: "정상 사용 환경에서 IP밴은 발생하지 않는다"는 결과 자체를
  의미 있는 발견으로 문서화

## 산출물

문서 + 각 Phase 실행 로그:

- **Phase 1**: Cloud Run 배포 결과, `/health` 응답, egress IP₁
- **Phase 2**: 정상 요청 응답(200), client.py 결정 로그
- **Phase 3**: IP밴 유도 시도 기록(요청 수, 응답 분포), IP밴 감지 여부,
  egress IP₁→IP₂ 변화(성공 시)
- **최종 문서**: 팀원 공유용 요약 (성공/실패 불문 결과 기록)

## 제약 및 위험

| 위험 | 완화 |
| --- | --- |
| quota 소진이 IP밴 유도를 막음 | 비공식 패턴 병행; quota는 프로젝트 단위라 운영 무영향 |
| IP밴 유도 비결정적 | 계속 시도 후 안 걸리면 사용자 보고 → 접근 A 폴백 준비 |
| ToS 위반 소지(비공식 패턴) | 별도 프로젝트/Key 사용; 운영 리소스 미사용 |
| Cloud Run 비용 | min instances 0, 테스트 종료 후 서비스 삭제 |
| Key/프로젝트 제재 | autoresearch-501004는 테스트 전용, 운영과 분리 |

## 참조

- PR #56: feat/47-youtube-resilient-client (복원력 구현)
- ADR 0001: `docs/adr/0001-youtube-proxy-purpose.md` (프록시 목적/전제)
- 기존 회전 검증(2026-07-05): 임시 프로젝트 수동 revision 배포 시
  egress IP 변화 관측(IPv6 `...4604:400::d00` → `...4603:400::901`)
- client.py: `autoresearch/youtube_collection/client.py` (복원력 4단계)
- proxy: `proxy/app.py`, `proxy/Dockerfile`
- 이슈 #60: Minor 백로그 (후속 품질 항목)
