# Agent Orchestration Digest 자동 승격 계획

## 구현 순서

- [x] API·UI 검증 output을 소비하는 release promotion job을 추가한다.
- [x] GitHub App token, immutable digest 입력, 변경 파일 allowlist, 동시성·race
  검사를 fail-closed로 구성한다.
- [x] release pipeline 문서와 cross-repository 운영 계약을 갱신한다.

### 승격 대상 추가 (#630)

- [x] executor 검증 output을 promotion job이 소비한다 — `needs`·env·source SHA
  일치 검사. **executor만 수동으로 남아 있어 클러스터가 두 릴리스 동안 옛 이미지로
  돌았다.** 실험을 실행하는 코드가 이 이미지 안에 있다
- [x] `tests/test_release_workflow.py`에 promotion job 계약 테스트를 추가한다 —
  이 job을 검증하는 테스트가 그동안 없었다
- [ ] infra `scripts/promote-agent-orchestration-digests.rb`의 `TARGETS`에 executor를
  추가한다. **이 저장소를 먼저 병합한다** — 반대 순서는 `ENV.fetch` `KeyError`로
  모든 digest 승격이 함께 멈춘다
- [ ] 두 저장소 병합 뒤 첫 release에서 `ORCH_EXECUTOR_IMAGE`가 자동 갱신되는지 확인

## 검증

- [x] `actionlint .github/workflows/release.yml`
- [x] `git diff --check`
- [x] release workflow의 main ancestor fetch 계약 테스트를 promotion Job 추가 수에 맞게 갱신한다.
- [ ] infra PR #587 merge 뒤 release의 workflow_dispatch로 검증된 source SHA 한 건을
  실행하여 App 설치 범위·Ruleset bypass·ArgoCD/PostSync 경로를 확인한다.
