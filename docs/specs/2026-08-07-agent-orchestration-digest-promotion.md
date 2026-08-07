# Agent Orchestration Digest 자동 승격 계약

## 목적

release에서 검증을 통과한 Agent Orchestration API·UI image digest를 수동 PR 없이
`Autoresearch-infra`의 GitOps desired state로 반영한다. infra `main` 갱신 후의
클러스터 배포는 ArgoCD automated sync와 infra PostSync verification Job이 담당한다.

## 계약

1. API와 UI publish job은 같은 full source SHA를 사용하고, OCI revision, non-root,
   각 runtime 검증을 통과해야 한다.
2. promotion job은 GitHub App token으로 infra `main`을 checkout한다. App은 infra
   단일 저장소의 Contents read/write 권한만 가지며, 일반 token의 권한 확대는 없다.
3. infra 저장소 소유 script는 고정 GAR repository의
   `repository@sha256:<64자리-소문자-hex>`만 받고 API 일곱 참조와 UI 한 참조만
   갱신한다. API digest가 기존부터 불일치하거나 허용 참조 수가 다르면 실패한다.
4. workflow는 변경 파일이 고정된 manifest 여섯 개의 부분집합일 때만 commit한다.
   API 또는 UI digest가 이미 최신이면 해당 manifest는 변경하지 않는다.
   checkout 뒤 infra `main`이 변경되면 push하지 않고 실패한다.
5. `main-protection` Ruleset bypass actor에는 이 GitHub App만 등록한다. 사람과
   일반 token의 직접 push 금지는 유지한다.

## 보안 및 운영

App ID와 private key는 GitHub repository secret으로만 사용하며 출력하거나
commit하지 않는다. concurrent release는 직렬화한다. 실패·rollback과 ArgoCD 확인은
infra의 `docs/AGENT_ORCHESTRATION_DIGEST_PROMOTION_RUNBOOK.md`를 기준으로 한다.
