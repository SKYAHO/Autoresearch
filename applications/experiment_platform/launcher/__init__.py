"""실험 executor Kubernetes Job launcher 패키지.

[파이프라인] `[AR]` 이슈와 기준 SHA가 DB에 봉인된 뒤 executor가 exp branch를 만들기
전 — CronJob 한 tick이 실행 대상을 선점하고 branch-bootstrap Job을 확정하는 구간을
담당한다.

[기능] launcher 설정 검증, PostgreSQL 전역 선점, 결정론적 Job manifest와 중단 복구를
모듈별로 제공한다.

[비책임] CronJob/RBAC/Secret/NetworkPolicy 배포(Autoresearch-infra), Job 완료 상태 회수와
실제 Git ref 생성(`agent_orchestration.executor`)은 담당하지 않는다.
"""
