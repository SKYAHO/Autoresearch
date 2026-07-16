# YouTube 복원력 실증 테스트 실행 로그

> Date: 2026-07-07 | Project: autoresearch-501004 | 담당: (사용자)

## Phase 1: Cloud Run proxy 배포

- 서비스명: youtube-proxy
- 리전: asia-northeast3
- 서비스 URL: `https://youtube-proxy-990733524187.asia-northeast3.run.app`
- revision: youtube-proxy-00001-5fq
- /health 응답: `{"status":"ok"}` ✅
- egress IP₁: `34.96.43.251`

### 비고

- 첫 배포 시 `--memory 256MiB` 파싱 실패(Cloud Run은 Mi 단위 요구). `256Mi`로 수정 후 성공. plan 단위 오류.
- ADC quota project 경고 발생(운영 프로젝트 불일치) — 배포에는 영향 없음.

## Phase 2: 정상 작동 확인

- API Key 직접 요청: ✅ (items 반환)
- proxy 경유 직접 요청: ✅ (items 반환)
- client.py (proxy_url 구성): ✅ (정상 동작)

### 비고

- 3단계 전부 정상. Phase 3 진행 전제 충족.

## Phase 3: IP밴 유도 시도

- 시도 호출 수: 10000 (50 workers 병렬, 약 2.5분)
- 응답 분포(stats): `{200: 8117, 403: 1870, network_error: 13}`
- 응답 분포(reasons): `{quotaExceeded: 1870}`
- quota 소진 시점: 약 8117번째 호출 (이 프로젝트 일일 quota ~8117 units)
- IP밴 시그니처 감지: **아니오** (quotaExceeded 외 403 = 0건)

### quota 초과 시 복원력 레이어 동작 실증

quota 소진 상태에서 `ResilientYouTubeClient` 호출 결과:

- `verdict`: `terminal_quota`
- `route`: `normal` (proxy 경유 전 감지 — quota는 프로젝트 단위)
- 동작: 회전 없이 즉시 `CollectionExhausted` 승격
- 메시지: `프로젝트 일일 쿼터 소진 — 회전 무효 resource=videos reason=quotaExceeded`
- 로깅 마스킹: `key_index=0`만 노출, Key값/헤더/본문 미노출 ✅

## 최종 결론

- **회전 증명: 불가 (IP밴 미발생)** — IP밴이 발생하지 않았으므로 revision 재배포로 egress IP 회전을 증명할 수 없음. 회전 메커니즘 자체는 별도 단위테스트로 시행.
- **핵심 발견:**
  1. quota 폭주로는 IP밴이 유도되지 않는다 — YouTube는 quota 초과 시 IP밴 대신 `quotaExceeded`만 반환. 두 메커니즘은 별개.
  2. quota 초과 시 복원력 레이어는 정상 동작한다 — `TERMINAL_QUOTA` 판정 → 즉시 `CollectionExhausted` 승격, 불필요한 회전·프록시 전환 없음.
  3. IP밴은 정상 사용 환경(7 units/day 규모)에서 발생하지 않는 꼬리 케이스다 — ADR 0001 가설 실증.

### 산출 파일

- `/tmp/quota_burst.log` (폭주 요청 로그)
- `scripts/empirical_test/quota_burst.py` (폭주 스크립트, 보관)
