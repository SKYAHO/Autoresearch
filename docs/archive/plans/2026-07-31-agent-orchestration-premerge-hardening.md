# Agent Orchestration PR 사전 병합 강화 계획

## 목표

PR #435/#454의 독립 보안·런타임·배포 감사에서 확인된 P1 항목을 병합 전에
해소하고, 앱·이미지·인프라·운영 문서의 계약을 테스트 가능한 형태로 고정한다.

## 범위와 결정

- `ORCH_API_TOKEN`과 `ORCH_RUNNER_TOKEN`은 같은 값이면 API 기동을 거부한다.
- `bootstrap_secrets`는 API DB와 Runner OAuth bootstrap을 명시적 CLI 역할로 제공한다.
  인프라 init container는 Python 내부 함수 import가 아닌 이 CLI만 호출한다.
- API 이미지 CI·release smoke는 bootstrap 모듈과 Secret Manager 의존성을 직접
  import해 init container 실행 표면을 검증한다.
- Runner는 기동 시 설정을 검증하고, Runner Codex 제한이 API Runner HTTP 제한보다
  짧다는 배포 계약을 manifest·검증으로 고정한다. 클라이언트 연결 종료 시 진행 중인
  Codex 작업이 취소되고 용량 토큰이 회수되는 경로를 테스트한다.
- OAuth 회전은 기존 `auth.json`을 무의식적으로 덮어쓰지 않는다. 명시적 일회성
  교체 경로와 GitOps target revision 갱신 절차를 infra runbook에 기록한다.

## 작업

1. 앱 설정·bootstrap CLI·이미지 smoke 계약을 TDD로 구현한다.
2. Runner startup/timeout/cancellation의 실패 모드를 TDD로 보완하고 infra manifest와
   계약을 맞춘다.
3. OAuth 회전과 ArgoCD target revision의 운영 절차를 infra manifest/runbook에 반영한다.
4. 독립 task review와 전체 PR 보안·런타임·배포 재감사를 수행하고, 테스트·린트·Docker·
   YAML/Terraform 검증 후에만 커밋한다.

## 검증 기준

- 앱 단위/통합 테스트는 API 토큰 분리, bootstrap 역할 dispatch, Runner 포화·취소·
  timeout, 502/503 경로를 검증한다.
- API·Runner 이미지 build/smoke가 각각의 실제 런타임 import 표면을 통과한다.
- infra YAML은 KSA/GSA/PVC/timeout·토큰·회전 절차 계약과 일치한다.
- 검토자는 보안, 런타임, 배포·운영 관점을 독립적으로 재검토한다.
