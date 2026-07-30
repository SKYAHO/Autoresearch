# Agent Orchestration GKE 내부 배포 계약

## 목적

공용 ChatGPT OAuth로 인증한 Codex CLI를 사용하면서도, 외부 입력 프롬프트가
OAuth 자격 증명·PostgreSQL 연결 정보·GCP 자격 증명을 읽거나 저장하지 못하도록
Agent Orchestration을 GKE dev에 내부 전용으로 배포한다.

로컬에서 검증한 `/chat` → LLM 응답 → PostgreSQL 저장 경로를 서버에서
검증하되, FastAPI API와 Codex 실행기를 같은 Pod에 두지 않는다. 이 계약은
이슈 #432의 구현 정본이며, 실험 생명주기 API·외부 공개·사용자별 계정은 범위가
아니다.

## 범위와 비범위

### 범위

- API Deployment와 private Codex Runner Deployment를 별도 Pod·서비스 계정·파일
  시스템으로 배포한다.
- API는 `/chat` 요청 검증, Runner 호출, PostgreSQL `chat_interactions` 저장만
  담당한다.
- Runner는 승인된 Codex 모델로 한 요청을 실행하고 결과만 API에 반환한다.
- API만 Cloud SQL 전용 DB·사용자·DB 비밀번호 Secret Manager 접근 권한을 가진다.
- Runner는 `readOnlyRootFilesystem`, non-root, `automountServiceAccountToken: false`,
  scratch `emptyDir`, 최소 NetworkPolicy로 실행한다.
- GKE에서 `cli_auth_credentials_store = "keyring"` OAuth가 비대화형 실행·Pod
  재시작·침투 테스트를 통과하는지 먼저 검증한다.
- 성공한 경우에만 내부 `/healthcheck`와 `/chat` 및 Cloud SQL 저장을 검증하고
  runbook을 작성한다.

### 비범위

- Ingress, LoadBalancer, 공개 URL, 외부 사용자 인증·인가
- OAuth `auth.json`을 Secret Manager, Kubernetes Secret, PVC, 이미지, 환경 변수로
  주입하거나 보관하는 방식
- OpenAI API 키 기반 백엔드, 사용자별 OAuth, 사용자별 사용량 분리
- 고가용성, 다중 replica, 자동 수평 확장, 실험 생성·상태·이벤트 API

## 대상 아키텍처

```text
허용된 클러스터 워크로드 또는 kubectl port-forward
  → ClusterIP: agent-orchestration-api
      → API Pod
          - X-Orch-Token·요청 스키마 검증
          - private runner HTTP 호출
          - Cloud SQL Private IP 저장
          - DB Secret Manager 접근만 허용
  → ClusterIP: agent-orchestration-runner
      → Runner Pod
          - Codex CLI OAuth 실행만 담당
          - OS keyring 자격 증명 저장소
          - read-only root + scratch emptyDir
          - DB URL·앱 API 토큰·GCP 서비스 계정 토큰 없음
```

API와 Runner는 서로 다른 Kubernetes ServiceAccount를 사용한다. API만
Workload Identity로 DB 비밀번호 Secret Manager 버전을 읽는다. Runner에는
Workload Identity annotation과 Kubernetes 서비스 계정 토큰 자동 마운트를 두지
않는다.

Runner Service는 ClusterIP이며, NetworkPolicy는 API Pod label에서 오는 지정
포트 요청만 허용한다. API Service 역시 외부 Ingress를 만들지 않는다. 초기
검증은 승인된 같은 클러스터 워크로드 또는 `kubectl port-forward`로 한정한다.

## API·Runner 내부 계약

API가 Runner에 요청하는 내부 HTTP 계약은 외부 `/chat` 계약과 분리한다.

```json
POST /v1/generate
{
  "prompt": "CTR 개선안을 세 줄로 정리해 주세요."
}
```

```json
{
  "response": "...",
  "model": "codex-cli",
  "latency_ms": 1234,
  "token_count": null
}
```

- Runner는 API와 NetworkPolicy로만 연결되며 외부 노출하지 않는다.
- API는 Runner 오류를 기존 `/chat`의 안전한 `502` 응답으로 변환한다. Runner의
  stderr 원문, OAuth 상태, 내부 파일 경로는 API 로그·응답에 전달하지 않는다.
- API가 PostgreSQL 저장에 실패하면 응답을 성공으로 반환하지 않는다.
- Runner 요청 동시성은 하나로 시작하고, 한도를 넘는 요청은 API가 `503`으로
  거절한다. API의 PostgreSQL pool과 외부 요청 rate limit은 실험 API 단계에서
  별도 계약으로 확장한다.
- 초기 모델은 Runner deployment 설정의 허용된 `CODEX_MODEL` 하나로 고정한다.
  `/chat` 요청 본문에 모델명은 받지 않는다.

## OAuth 자격 증명 게이트

Codex CLI는 `cli_auth_credentials_store = "keyring"`을 지원하며, file·auto
저장소는 `CODEX_HOME/auth.json`을 사용할 수 있다. OAuth 파일은 access token을
포함하므로 Runner의 프롬프트 실행 범위에 제공하지 않는다.

배포 전에 아래를 모두 만족해야 한다.

1. GKE Runner에서 keyring 설정으로 비대화형 `codex login status`와 짧은
   `codex exec`가 성공한다.
2. Pod 재시작 후에도 인증 상태가 안전하게 유지되거나, 승인된 운영 절차로
   재인증할 수 있다. 인증 상태를 파일·PVC·Kubernetes Secret으로 복사하지
   않는다.
3. 프롬프트 기반 침투 테스트가 `auth.json`, `/proc/*/environ`, GCP ADC,
   Kubernetes 서비스 계정 토큰, API 소스, DB 연결 정보에 접근하지 못한다.
4. Runner root filesystem은 read-only이고, `HOME`, `TMPDIR`, `XDG_CACHE_HOME`,
   `XDG_STATE_HOME`은 요청 scratch `emptyDir`에만 쓴다.
5. Runner가 필요한 Codex 외부 목적지 외로 통신하지 않고, API 이외의 Pod가
   Runner Service를 호출하지 못한다.

위 조건 하나라도 실패하면 공용 OAuth 기반 `/chat` 배포는 중단한다. 이 경우
API·Cloud SQL·Runner 네트워크 배선만 검증할 수 있으며, 실제 LLM smoke는
OpenAI API 전환 또는 별도 승인된 인증 경계가 마련될 때까지 완료로 주장하지
않는다.

## Cloud SQL·시크릿 계약

- 인스턴스: 기존 `autoresearch-dev-pg` (PostgreSQL 15, Private IP)
- 신규 DB: `agent_orchestration`
- 신규 SQL 사용자: `agent_orchestration_app`
- DB 비밀번호는 Terraform이 생성하고 Secret Manager에 보관한다.
- API init container만 Workload Identity로 DB 비밀번호를 읽어 API 전용 권한
  제한 runtime 파일을 준비한다. 완성 DB URL·비밀번호를 Git, 이미지, ConfigMap,
  Kubernetes Secret, Pod manifest, 일반 로그에 두지 않는다.
- 현재 `ensure_schema()`의 `chat_interactions` 최초 생성은 이 배포 검증에서만
  유지한다. 실험 Control Plane 단계에서 Alembic으로 전환한다.

## 이미지·저장소 책임

### `SKYAHO/Autoresearch`

- API 이미지에는 FastAPI·PostgreSQL 의존성만 포함하고 Codex CLI·OAuth 상태를
  포함하지 않는다.
- Runner 이미지는 고정된 Codex CLI와 internal `/v1/generate` 서비스만 포함한다.
  애플리케이션 저장소 전체, DB URL, `.env`를 포함하지 않는다.
- 두 이미지의 non-root 실행, OAuth 파일 부재, API→Runner 오류 변환, Runner
  sandbox 인자 계약을 테스트·CI에서 검증한다.
- GAR에 두 immutable digest를 발행하고 runbook에 digest 기반 배포·롤백 절차를
  기록한다.

### `SKYAHO/Autoresearch-infra`

- Cloud SQL DB·사용자·Secret Manager·API GSA/KSA와 최소 IAM을 Terraform으로
  선언한다.
- API/Runner Deployment, 각 ClusterIP Service, `ReadWriteOnce` OAuth PVC가 아닌
  scratch `emptyDir`, NetworkPolicy를 선언한다.
- Runner에는 Secret Manager 권한, Cloud SQL 권한, Workload Identity annotation,
  서비스 계정 토큰 자동 마운트를 추가하지 않는다.
- 검증된 immutable GAR digest만 참조하며 ArgoCD는 수동 sync로 시작한다.

## 배포 순서와 성공 기준

1. API→Runner 내부 계약과 분리된 이미지를 구현·검증한다.
2. GKE에서 OAuth keyring 사전 검증을 수행하고 결과를 runbook에 기록한다.
3. API DB·Secret Manager·GKE NetworkPolicy Terraform을 적용한다.
4. 두 immutable image digest를 사용해 dev에 수동 동기화한다.
5. `kubectl port-forward`로 API `/healthcheck`와 짧은 `/chat`을 호출한다.
6. `agent_orchestration.chat_interactions`에는 ID·모델·지연 시간·생성 시각만
   조회해 저장을 확인한다. 프롬프트·응답·토큰·OAuth 내용은 출력하지 않는다.

성공은 다음을 모두 만족할 때다.

- API와 Runner가 서로 다른 Pod·서비스 계정·권한 집합으로 Ready 상태다.
- API Pod에는 OAuth 자격 증명이, Runner Pod에는 DB URL·DB 비밀번호·GCP 서비스
  계정 토큰이 없다.
- keyring·침투 검증 게이트가 통과한 경우에만 `/chat`이 `201`을 반환하고 Cloud
  SQL에 행을 저장한다.
- API·Runner의 로그, 이미지, manifest, Git에 OAuth·DB 비밀번호·완성 DB URL이
  없다.
- 이전 immutable digest로의 롤백과 OAuth 장애 대응 절차가 runbook으로 재현된다.
