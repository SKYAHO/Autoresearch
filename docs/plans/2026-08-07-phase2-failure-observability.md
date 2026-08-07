# Phase 2 실패 관측과 smoke Job 보존 구현 계획

> 관련 이슈: #582
> 정본: `docs/specs/2026-08-06-experiment-executor-phase2.md`

**목표:** 무로그로 종료되는 Phase 2 container의 실패 stage와 안전한 사유를 Cloud
Logging에서 확인하고, 긴급 smoke 동안 완료 Job을 3600초 보존할 수 있게 한다.

**범위:** 애플리케이션은 실패 로그와 선택 TTL 환경 변수만 제공한다. Infra의 실제 환경
변수 주입·digest 승격과 운영 smoke는 companion 변경에서 수행한다. Kubernetes RBAC,
admission, NetworkPolicy는 로그가 구체적인 차단을 증명할 때만 별도 변경한다.

## Task 1: Phase 2 안전 실패 로그

- [x] `tests/test_experiment_executor_integration.py`에 stage 시작·종료와 예외 실패 로그를
  추가한다.
- [x] raw `OSError`의 filesystem 경로와 임의 `RuntimeError` 원문이 로그에 나오지 않는
  실패 테스트를 확인한다.
- [x] `agent_orchestration/executor/phase2.py`에 INFO logging 초기화와 정제된 실패 로그를
  최소 구현한다.
- [x] 좁은 executor 통합 테스트를 통과시킨다.

## Task 2: 완료 Job TTL 선택 설정

- [x] `ORCH_TTL_AFTER_FINISHED_SEC=3600` 주입과 미설정 기본값 30초를 테스트한다.
- [x] 0·음수·비정수 값을 fail-closed하는 테스트를 추가한다.
- [x] launcher 설정과 `.env.example`, README, 프로젝트 참조 문서를 갱신한다.
- [x] launcher와 executor 관련 테스트를 통과시킨다.

## Task 3: 검증과 운영 인계

- [x] `git diff --check`와 관련 pytest, 전체 Ruff를 실행한다.
- [ ] PR과 immutable executor/launcher image digest를 발행한다.
- [ ] Infra에서 TTL 3600과 새 digest를 반영하고 launcher suspend를 해제한다.
- [ ] 새 Experiment 하나가 평가 중 (`EVALUATING`)까지 가는 smoke 증거를 수집한다.
- [ ] 성공 후 TTL을 30초로 되돌리고 불필요한 권한 확대가 없음을 확인한다.
