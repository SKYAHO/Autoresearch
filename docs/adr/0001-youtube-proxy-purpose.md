# ADR 0001: YouTube 프록시 서비스의 목적과 범위

- **상태**: Accepted
- **날짜**: 2026-07-05
- **이슈**: #47

## 배경

YouTube Data API v3 일일 수집 볼륨은 하루 약 7 units(기본 할당량 10,000의 0.07%)로, 공식 API Key 기반 정상 사용자가 egress IP 단위 밴을 당하는 경우는 사실상 발생하지 않는다. Google의 제재 모델은 quota(프로젝트 단위)와 rate limit(per-user/per-key)이며, IP 밴은 비공식 스크래핑·남용 패턴에 대한 조치로 공식 Key 사용 환경에서는 꼬리 케이스다.

## 결정

`proxy/` Cloud Run dumb forwarder 와 `ResilientYouTubeClient` 의 IP밴 시그니처/프록시 전환 계층은 **운영상 관측된 IP밴에 대한 대응이 아니라** 아래 두 목적의 학습/포트폴리오 산출물이다:

1. **학습**: Cloud Run liveness/unhealthy, egress IP 경로 분리, SSRF 방어(host/path 화이트리스트), 예외 체이닝 마스킹, circuit breaker 패턴 등을 코드로 시연.
2. **범용 egress seam**: 향후 비-YouTube 엔드포인트나 별도 인증 경로가 필요할 때 교체 가능한 호출층.

## 3차(Cloud Run 배포/Terraform) 보류

Cloud Run 의 egress IP 회전 동작은 부분적으로 검증됐다:

- **수동 신규 revision 배포 → 새 IP 관측(검증됨)**: 임시 GCP 프로젝트 테스트(2026-07-05)에서 신규 revision 배포 시 새 인스턴스에 새 egress IP 가 할당됨을 관측(IPv6 대역 `...4604:400::d00` → `...4603:400::901`). 회전된 IP 로 YouTube Data API 호출 정상 성공(200, KR 트렌딩).
- **liveness 자동 재시작 → IP 회전(미검증)**: Cloud Run healthchecks 문서가 "limits instance restarts to prevent uncontrolled crash loops" 를 명시해, once-True-stays-True unhealthy 루프가 1~2회 후 재시작 자체가 스로틀될 수 있다. 본 경로는 프로덕션에서 실제 IP밴 발생 전까지 관측 불가.

따라서 3차 인프라 배포는 **liveness 자동 회전 경로의 경험적 검증 + 인프라 담당자 조율 전까지 보류**한다. 2차 코드(proxy/ + client 연동)는 Docker 통합테스트로 국소적 실행 증거를, 임시 Cloud Run 테스트로 수동 revision 경로의 IP 회전 증거를 남긴다.

## 결과

- 코드의 IP밴 시그니처 판정/프록시 전환은 이론적 방어로 그대로 유지(매몰비, 이미 built+tested).
- 단, **신규 유사 설계에 이 패턴(7 units/day 에 Cloud Run 프록시)을 반복하지 않는다** — 오버엔지니어링.
- code docstring/plan 은 IP밴을 동기 문제가 아닌 "이론적 시나리오/학습 목적"으로 프레이밍 한다(본 ADR 참조).

## 참고

- 설계 문서: `docs/archive/specs/2026-07-03-youtube-ip-ban-resilience-design.md` (PR #48 머지 후 main 에 반영)
- 구현 계획: `docs/archive/plans/2026-07-05-youtube-resilient-client.md`, `docs/archive/plans/2026-07-05-youtube-proxy-service.md`
- Cloud Run healthchecks 문서 재시작 throttle 명시: `cloud.google.com/run/docs/configuring/healthchecks`
