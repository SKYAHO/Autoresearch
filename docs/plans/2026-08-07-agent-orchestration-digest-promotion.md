# Agent Orchestration Digest 자동 승격 계획

## 구현 순서

- [x] API·UI 검증 output을 소비하는 release promotion job을 추가한다.
- [x] GitHub App token, immutable digest 입력, 변경 파일 allowlist, 동시성·race
  검사를 fail-closed로 구성한다.
- [x] release pipeline 문서와 cross-repository 운영 계약을 갱신한다.

## 검증

- [x] `actionlint .github/workflows/release.yml`
- [x] `git diff --check`
- [x] release workflow의 main ancestor fetch 계약 테스트를 promotion Job 추가 수에 맞게 갱신한다.
- [ ] infra PR #587 merge 뒤 release의 workflow_dispatch로 검증된 source SHA 한 건을
  실행하여 App 설치 범위·Ruleset bypass·ArgoCD/PostSync 경로를 확인한다.
