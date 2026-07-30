# Agent Orchestration GKE 내부 배포 계약

## 목적

공용 ChatGPT 계정으로 OAuth 로그인한 Codex CLI를 사용하는 Agent Orchestration
FastAPI를 기존 GKE dev 클러스터와 Cloud SQL PostgreSQL에 내부 전용으로 배포한다.
로컬에서 검증한 `/chat` → Codex CLI → PostgreSQL 저장 경로를 서버에서도 같은
보안 경계로 운영하는 것이 목표다.

이 계약은 구현 코드 PR #431이 `main`에 병합된 뒤 적용한다. 사용자 인증과 외부
인터넷 공개는 이 배포의 범위가 아니다.

## 범위와 비범위

### 범위

- `agent_orchestration` 전용 컨테이너 이미지를 GAR에 발행한다.
- 기존 `autoresearch-dev-pg` Cloud SQL(PostgreSQL 15)에 전용 데이터베이스와
  전용 SQL 사용자를 만든다.
- GKE에 단일 replica Deployment와 ClusterIP Service를 만든다.
- Cloud SQL 접속 정보와 공용 Codex OAuth 초기 인증 파일을 Secret Manager에서
  주입한다.
- Codex 인증 상태를 단일 PVC에 보존해 토큰 갱신 후에도 Pod 재시작을 견딘다.
- 내부 `/healthcheck`, `/chat`, PostgreSQL 저장을 실제로 검증하고 운영
  runbook을 남긴다.

### 비범위

- Ingress, LoadBalancer, 공개 URL, 외부 사용자 인증/인가
- 사용자별 Google/Codex OAuth, 사용자별 모델 사용량 분리
- 고가용성, 다중 replica, 자동 수평 확장
- OpenAI API 키 기반 백엔드로의 전환

## 대상 아키텍처

```text
GKE 내부 호출 또는 kubectl port-forward
  → ClusterIP Service
  → agent-orchestration Deployment (replicas=1)
      → FastAPI /chat
      → codex exec (read-only, ephemeral)
      → Cloud SQL Private IP: agent_orchestration DB
      → PVC: CODEX_HOME (공용 OAuth 갱신 상태)
      ← Secret Manager: DB URL, OAuth 초기 auth.json
```

Pod는 non-root 사용자로 실행한다. `/healthcheck`는 DB 초기화 성공 여부만
확인하며 외부 LLM 호출을 수행하지 않는다. `/chat`은 기존 구현처럼 빈 임시
작업 디렉터리에서 `codex exec --sandbox read-only --ephemeral`을 호출한다.

## Cloud SQL 계약

- 인스턴스: 기존 `autoresearch-dev-pg` (PostgreSQL 15, Private IP)
- 신규 DB: `agent_orchestration`
- 신규 SQL 사용자: `agent_orchestration_app`
- 비밀번호는 Terraform이 생성하고 Secret Manager에 저장한다.
- 애플리케이션에는 완성된 `ORCH_DATABASE_URL`만 Secret Manager 기반 시크릿으로
  주입한다. 비밀번호를 ConfigMap, 이미지, Git, 로그에 두지 않는다.
- 스키마는 현재 앱의 `ensure_schema()`가 `chat_interactions` 테이블을 최초
  생성하는 방식으로 유지한다. 파괴적 마이그레이션은 이번 범위에 없다.

GKE 워크로드는 기존 VPC를 통해 Cloud SQL Private IP에만 접속한다. public IP와
공개 Cloud SQL Auth Proxy 엔드포인트는 추가하지 않는다.

## 공용 Codex OAuth 상태 계약

Codex CLI의 `CODEX_HOME`에는 인증 정보와 갱신 상태가 저장된다. 공용 OAuth를
지속해서 쓰려면 이 상태를 읽기 전용 Secret 볼륨에만 두면 안 된다.

1. 운영자가 신뢰된 로컬 환경에서 팀 공용 계정으로 `codex login`을 수행한다.
2. 파일 기반 자격 증명(`auth.json`)을 Secret Manager의 초기 인증 시크릿으로
   저장한다. 이 파일은 비밀번호와 동등한 민감도로 취급한다.
3. Pod init container가 PVC에 인증 파일이 없을 때만 초기 시크릿을
   `CODEX_HOME/auth.json`으로 복사하고 권한을 소유자 읽기/쓰기로 제한한다.
4. 애플리케이션 컨테이너는 같은 PVC를 읽기/쓰기로 마운트하고
   `CODEX_HOME`을 그 경로로 설정한다. Codex CLI가 실행 중 갱신한 인증 상태는
   PVC에 남는다.
5. Deployment는 `replicas: 1`, PVC는 `ReadWriteOnce`로 고정한다. 하나의 공용
   계정의 사용량과 인증 상태를 여러 Pod가 동시에 갱신하지 않게 한다.

OAuth 초기 인증 파일은 Kubernetes Secret, ConfigMap, 환경 변수로 직접 넣지
않는다. Secret Manager CSI 또는 동등한 읽기 전용 파일 주입 경로를 사용한다.
PVC에는 `CODEX_HOME` 외의 애플리케이션 데이터나 사용자 프롬프트를 저장하지
않는다.

## OAuth 갱신·복구 운영 절차

- 정상 실행 중 토큰 갱신은 PVC에 보존된다.
- `codex login status` 실패, 공용 계정 로그아웃, PVC 손실이 발생하면 운영자는
  신뢰된 로컬 환경에서 다시 로그인한 뒤 초기 인증 시크릿을 갱신한다.
- 기존 PVC를 새 인증 상태로 교체할 때는 Deployment를 0으로 축소하고, 승인된
  일회성 관리 Pod에서 새 파일을 복사한 후 권한을 재설정한다. 완료 후에만
  Deployment를 1로 복구한다.
- 인증 파일의 값, 복사 명령 출력, Secret Manager 버전 내용은 티켓·PR·로그에
  기록하지 않는다.

이 방식은 공용 OAuth를 요구하는 신뢰된 내부 단일 워크로드용이다. 자동화의
일반적인 기본 인증 수단으로 확장하지 않는다.

## 이미지·배포 책임

### `SKYAHO/Autoresearch`

- `agent_orchestration` 전용 Dockerfile을 추가해 FastAPI와 호환되는 Codex CLI를
  포함한 non-root 이미지를 만든다.
- 이미지에는 OAuth 인증 파일, DB URL, `.env`를 넣지 않는다.
- 기존 release 이미지 발행 흐름에 `agent-orchestration` GAR 이미지와 immutable
  digest 검증을 추가한다.
- Docker build, 앱 import, 단위 테스트를 CI에서 검증한다.

### `SKYAHO/Autoresearch-infra`

- Cloud SQL DB·사용자·Secret Manager 시크릿을 Terraform으로 선언한다.
- GKE namespace, Kubernetes ServiceAccount, Workload Identity, Secret Manager
  접근 권한, PVC, Deployment, ClusterIP Service를 선언한다.
- Deployment는 검증된 immutable GAR digest만 참조한다.
- Secret Manager CSI 마운트, init container, `CODEX_HOME` 권한을 구성한다.

## 네트워크·보안 계약

- Service type은 `ClusterIP`이며 Ingress와 LoadBalancer를 만들지 않는다.
- 초기 검증은 같은 클러스터의 허용된 워크로드 또는 `kubectl port-forward`로만
  수행한다.
- Workload Identity는 DB URL과 OAuth 초기 인증 시크릿을 읽는 최소 권한만 가진다.
- 앱 로그에는 프롬프트, LLM 응답 전문, OAuth 인증 정보, DB URL/비밀번호를
  기록하지 않는다.
- Pod와 PVC의 파일 권한은 앱 non-root UID와 필요한 init container 외에는
  읽을 수 없도록 제한한다.

## 배포 순서

1. PR #431을 병합하고 Agent Orchestration 이미지 생성 코드를 별도 PR로 병합한다.
2. 검증된 이미지를 GAR에 push하고 digest를 확정한다.
3. 인프라 저장소에서 Cloud SQL·Secret Manager·GKE 리소스 PR을 작성해 적용한다.
4. 초기 OAuth 시크릿을 운영자가 안전한 경로로 등록한다.
5. GKE Deployment를 digest로 배포하고 readiness를 확인한다.
6. 내부 `/healthcheck`와 짧은 `/chat` 요청을 실행한다.
7. `agent_orchestration.chat_interactions`에 저장된 모델명·지연시간·생성 시각을
   확인한다. 토큰·프롬프트·응답 전문은 운영 검증 출력에 노출하지 않는다.

## 롤백·성공 기준

롤백은 이전 검증 이미지 digest로 Deployment를 되돌린다. DB 변경은 신규
데이터베이스·테이블 생성뿐이므로 이미지 롤백 시에도 호환된다. OAuth PVC는
롤백 대상 이미지와 분리해 보존한다.

성공 기준은 다음과 같다.

- 단일 non-root Pod가 Ready 상태이고 Cloud SQL에 연결된다.
- Pod의 `codex login status`가 공용 ChatGPT 로그인 상태를 확인한다.
- 내부 `/chat` 호출이 201을 반환하고 전용 DB의 `chat_interactions`에 행을
  저장한다.
- OAuth 인증 파일과 DB 비밀번호가 Git, 이미지, 환경 변수, ConfigMap, 일반
  로그에 존재하지 않는다.
- 이미지 digest 롤백과 OAuth 재로그인/PVC 복구 절차가 runbook으로 재현 가능하다.
