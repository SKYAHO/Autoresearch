# Agent Orchestration Digest 자동 승격 계약

## 목적

release에서 검증을 통과한 Agent Orchestration image digest를 수동 PR 없이
`Autoresearch-infra`의 GitOps desired state로 반영한다. infra `main` 갱신 후의
클러스터 배포는 ArgoCD automated sync와 infra PostSync verification Job이 담당한다.

## 승격 대상

| image | 반영 위치 | 참조 수 |
|---|---|---|
| api | `api-deployment`(2) · `api-migration-job`(2) · `launcher-cronjob`(1) · `runner-deployment`(1) · `deployment-verification-job`(1) | 7 |
| ui | `ui-deployment` | 1 |
| launcher | `launcher-cronjob` | 1 |
| runner | `runner-deployment` | 1 |
| executor | `launcher-cronjob`의 `ORCH_EXECUTOR_IMAGE` | 1 |

**executor를 포함하는 이유(#630):** launcher는 Job을 조립해 제출할 뿐이고 학습·Codex·
검증·채점·API 보고는 executor 이미지가 수행한다. executor만 수동으로 남기면 자동 승격
로그가 "배포됐다"로 읽히는데 실험은 옛 이미지로 도는 상태가 조용히 성립한다. 실제로
클러스터가 두 릴리스 동안 옛 executor digest에 머물러 있었다.

executor digest는 Deployment의 `image:`가 아니라 launcher CronJob의 **env 값**이므로,
승격 시점에 도는 실험은 자기 Pod spec을 유지하고 그 뒤 생성되는 Job부터 새 이미지로 뜬다.

## 계약

1. 승격 대상 publish job은 **모두 같은 full source SHA**를 사용하고, OCI revision,
   non-root, 각 runtime 검증을 통과해야 한다. 하나라도 다르면 승격하지 않는다.
2. promotion job은 GitHub App token으로 infra `main`을 checkout한다. App은 infra
   단일 저장소의 Contents read/write 권한만 가지며, 일반 token의 권한 확대는 없다.
3. infra 저장소 소유 script는 고정 GAR repository의
   `repository@sha256:<64자리-소문자-hex>`만 받고 위 표의 참조만 갱신한다. digest가
   기존부터 불일치하거나 허용 참조 수가 다르거나 허용 범위 밖 파일에 같은 repository
   참조가 있으면 실패한다.
4. workflow는 변경 파일이 고정된 manifest 여섯 개의 부분집합일 때만 commit한다.
   digest가 이미 최신이면 해당 manifest는 변경하지 않는다.
   checkout 뒤 infra `main`이 변경되면 push하지 않고 실패한다.
5. `main-protection` Ruleset bypass actor에는 이 GitHub App만 등록한다. 사람과
   일반 token의 직접 push 금지는 유지한다.

## 보안 및 운영

App ID와 private key는 GitHub repository secret으로만 사용하며 출력하거나
commit하지 않는다. concurrent release는 직렬화한다. 실패·rollback과 ArgoCD 확인은
infra의 `docs/AGENT_ORCHESTRATION_DIGEST_PROMOTION_RUNBOOK.md`를 기준으로 한다.

## 대상 추가 시 병합 순서

승격 script는 infra 저장소가 소유하고 release가 실행 시점에 infra `main`에서 가져와
실행한다. 그래서 **이 저장소를 먼저 병합한다.** infra를 먼저 병합하면 script가
아직 전달되지 않는 env를 `ENV.fetch`로 읽어 `KeyError`로 죽고, **모든 digest 승격이
함께 멈춘다.** 반대 순서는 script가 남는 env를 무시하므로 무해하고, 두 저장소가 모두
병합된 다음 릴리스부터 효과가 난다.
