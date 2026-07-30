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
- Runner는 non-root, 전용 `CODEX_HOME` PVC, scratch `emptyDir`, 최소
  NetworkPolicy로 실행한다.
- Runner만 Workload Identity로 공용 Codex OAuth 초기 인증 시크릿 하나를 읽고,
  API와 다른 어떤 워크로드도 이 시크릿에 접근하지 못한다.
- 내부 `/healthcheck`와 `/chat` 및 Cloud SQL 저장을 검증하고 runbook을 작성한다.

### 비범위

- Ingress, LoadBalancer, 공개 URL, 외부 사용자 인증·인가
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
          - OAuth 전용 Secret Manager 접근·CODEX_HOME PVC
          - DB URL·Cloud SQL 권한·앱 API 토큰 없음
```

API와 Runner는 서로 다른 Kubernetes ServiceAccount와 GCP ServiceAccount를
사용한다. API는 DB 비밀번호 Secret Manager 버전과 Cloud SQL에만 접근한다.
Runner는 Codex OAuth 초기 인증 시크릿 하나에만 접근하며 Cloud SQL·DB 비밀번호
시크릿·다른 GCP 리소스 권한을 받지 않는다.

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

## OAuth 자격 증명 계약

Codex CLI의 파일 기반 `CODEX_HOME/auth.json`은 access token을 포함하므로
비밀번호와 같은 민감도로 취급한다. 이 MVP는 기존 인프라의 Secret Manager+
Workload Identity 패턴을 따른다.

1. 운영자는 신뢰된 로컬 환경에서 팀 공용 계정으로 `codex login`을 수행한다.
2. `auth.json`은 전용 Secret Manager 시크릿의 초기값으로만 보관한다. Git,
   이미지, ConfigMap, 환경 변수, 일반 로그에는 넣지 않는다.
3. Runner init container가 Runner 전용 GSA로 초기 시크릿을 읽는다. 전용
   `CODEX_HOME` PVC에 인증 파일이 없을 때만 `auth.json`을 mode `0600`으로
   기록한다.
4. Runner 컨테이너는 동일 PVC를 읽기·쓰기로 마운트한다. Codex CLI가 갱신한
   인증 상태는 단일 replica의 `ReadWriteOnce` PVC에 남는다.
5. API Pod는 OAuth 시크릿과 PVC를 절대 읽거나 마운트하지 않는다. Runner GSA는
   OAuth 시크릿 외의 Secret Manager·Cloud SQL·데이터 접근 권한을 받지 않는다.

Runner는 OAuth 파일에 접근해야 하는 실행 환경이므로, 프롬프트가 이를 읽으려는
위험을 완전히 제거할 수 없다. 따라서 이 배포는 신뢰된 dev 내부 서비스와
비민감 프롬프트로만 시작하며, 외부 Ingress·사용자별 공개 API를 추가하지 않는다.
API/DB/GCP 권한을 Runner에서 분리해 유출 영향 범위를 OAuth 실행 계정으로
제한한다. OS keyring 지원은 이 MVP의 선행 조건이 아니라 후속 보안 개선 항목이다.

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
- API/Runner Deployment, 각 ClusterIP Service, Runner 전용 `ReadWriteOnce`
  OAuth PVC, scratch `emptyDir`, NetworkPolicy를 선언한다.
- Runner GSA에는 OAuth 초기 인증 시크릿 하나의 `secretAccessor`만 부여한다.
  Cloud SQL·DB 비밀번호 시크릿·다른 Secret Manager 리소스 권한은 추가하지
  않는다.
- 검증된 immutable GAR digest만 참조하며 ArgoCD는 수동 sync로 시작한다.

## 배포 순서와 성공 기준

1. API→Runner 내부 계약과 분리된 이미지를 구현·검증한다.
2. Runner 전용 Secret Manager 시크릿·PVC 초기화와 `codex login status`를
   검증하고 결과를 runbook에 기록한다.
3. API DB·Secret Manager·GKE NetworkPolicy Terraform을 적용한다.
4. 두 immutable image digest를 사용해 dev에 수동 동기화한다.
5. `kubectl port-forward`로 API `/healthcheck`와 짧은 `/chat`을 호출한다.
6. `agent_orchestration.chat_interactions`에는 ID·모델·지연 시간·생성 시각만
   조회해 저장을 확인한다. 프롬프트·응답·토큰·OAuth 내용은 출력하지 않는다.

성공은 다음을 모두 만족할 때다.

- API와 Runner가 서로 다른 Pod·서비스 계정·권한 집합으로 Ready 상태다.
- API Pod에는 OAuth 자격 증명이, Runner Pod에는 DB URL·DB 비밀번호·Cloud SQL
  권한이 없다. Runner GSA의 Secret Manager 권한은 OAuth 시크릿 하나로 제한된다.
- `/chat`이 `201`을 반환하고 Cloud SQL에 행을 저장한다.
- API·Runner의 로그, 이미지, manifest, Git에 OAuth·DB 비밀번호·완성 DB URL이
  없다.
- 이전 immutable digest로의 롤백과 OAuth 장애 대응 절차가 runbook으로 재현된다.
