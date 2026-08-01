# Agent Orchestration 실험 워크벤치 v0 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement
> this plan task-by-task. 각 Task 완료 후 사용자 확인을 받고 다음 Task로 진행한다.

**Goal:** Issue #448의 실험 관찰 API를 기존 `/chat` 계약을 깨지 않고 구현한다.

**Architecture:** 기존 `/chat`의 psycopg 경로는 유지하고, 신규
`agent_orchestration.app.experiments` feature package와 SQLAlchemy Session/Alembic
계보를 추가한다. 모든 상태 쓰기는 service transaction을 통과하며 수동 승격은 전용
endpoint로 격리한다.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x, psycopg 3,
Alembic, PostgreSQL, pytest.

## Global Constraints

- 정본 계약은 `docs/specs/2026-08-01-agent-orchestration-experiment-workbench-v0.md`다.
- 기존 `POST /chat` 요청·응답·인증·저장 동작을 변경하지 않는다.
- 일반 상태 API는 `PROMOTED`를 허용하지 않는다.
- production code는 대응하는 실패 테스트를 먼저 확인한 뒤 작성한다.
- 각 Task 완료 후 검증 결과를 보고하고 다음 Task 승인 전 멈춘다.

---

### Task 1: 계약과 초안 정합성 고정

**Files:**
- Create: `docs/specs/2026-08-01-agent-orchestration-experiment-workbench-v0.md`
- Create: `docs/plans/2026-08-01-agent-orchestration-experiment-workbench-v0.md`

- [x] 기존 FastAPI, psycopg, 오류, Pydantic, 테스트 패턴을 조사한다.
- [x] `EVALUATING -> ERROR`를 포함한 상태 전이와 터미널 상태를 확정한다.
- [x] `/promote` 전용 수동 승격과 일반 endpoint의 우회 차단을 확정한다.
- [x] UUID, timestamp, index와 migration 정본 정책을 확정한다.
- [x] Event/Log/Promote fingerprint 멱등성 계약을 확정한다.
- [x] `git diff --check`와 placeholder·계약 모순 검사를 통과한다.
- [x] 문서 변경을 `docs: 실험 워크벤치 v0 계약 확정`으로 커밋한다.

### Task 2: SQLAlchemy와 Alembic 기반

**Files:**
- Create: `agent_orchestration/app/database.py`
- Create: `agent_orchestration/alembic.ini`
- Create: `agent_orchestration/migrations/env.py`
- Create: `agent_orchestration/migrations/script.py.mako`
- Create: `agent_orchestration/migrations/versions/0001_create_experiment_tables.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Test: `tests/test_experiment_models.py`

- [x] migration/ORM 정합성 실패 테스트를 먼저 작성하고 실패 원인을 확인한다.
- [x] SQLAlchemy·Alembic 직접 의존성을 추가하고 lock을 갱신한다.
- [x] 동기 engine, session factory와 요청 단위 Session dependency를 구현한다.
- [x] 네 테이블, FK cascade, check와 unique constraint를 migration에 구현한다.
- [x] offline migration 및 dependency 검증을 통과하고 Task 커밋을 만든다.

### Task 3: ORM 모델과 상태 전이 검증

**Files:**
- Create: `agent_orchestration/app/experiments/__init__.py`
- Create: `agent_orchestration/app/experiments/models.py`
- Create: `agent_orchestration/app/experiments/transition_service.py`
- Test: `tests/test_experiment_models.py`
- Test: `tests/test_experiment_transition_service.py`

- [ ] 허용 전이 7개와 거부 전이의 실패 테스트를 작성한다.
- [ ] ORM 모델과 순수 `validate_transition()`을 최소 구현한다.
- [ ] ORM과 migration의 UUID/default/index 정합성을 검증한다.
- [ ] 관련 테스트와 lint를 통과하고 Task 커밋을 만든다.

### Task 4: 실험 생성과 조회

**Files:**
- Create: `agent_orchestration/app/experiments/schemas.py`
- Create: `agent_orchestration/app/experiments/repository.py`
- Create: `agent_orchestration/app/experiments/service.py`
- Create: `agent_orchestration/app/experiments/exceptions.py`
- Test: `tests/test_experiment_service.py`

- [ ] 생성·최초 event·metadata 원자성의 실패 테스트를 작성한다.
- [ ] 목록·상세·pagination·not-found 실패 테스트를 작성한다.
- [ ] repository와 service를 최소 구현한다.
- [ ] 관련 테스트와 lint를 통과하고 Task 커밋을 만든다.

### Task 5: 일반 상태 전이와 Event

**Files:**
- Modify: `agent_orchestration/app/experiments/service.py`
- Modify: `agent_orchestration/app/experiments/schemas.py`
- Test: `tests/test_experiment_service.py`

- [ ] row lock, 원자성, rollback과 `EVALUATING -> ERROR` 실패 테스트를 작성한다.
- [ ] 일반 endpoint의 `PROMOTED` 거부 테스트를 작성한다.
- [ ] 같은 key·같은 payload와 같은 key·다른 payload 테스트를 작성한다.
- [ ] 상태 전이와 Event 멱등 service를 최소 구현한다.
- [ ] 관련 테스트와 lint를 통과하고 Task 커밋을 만든다.

### Task 6: Log 저장과 polling 조회

**Files:**
- Modify: `agent_orchestration/app/experiments/service.py`
- Modify: `agent_orchestration/app/experiments/repository.py`
- Modify: `agent_orchestration/app/experiments/schemas.py`
- Test: `tests/test_experiment_service.py`

- [ ] append, cursor, 터미널 상태, 동시 중복과 충돌의 실패 테스트를 작성한다.
- [ ] Log 저장과 조회를 최소 구현한다.
- [ ] 관련 테스트와 lint를 통과하고 Task 커밋을 만든다.

### Task 7: 운영자 수동 승격

**Files:**
- Modify: `agent_orchestration/app/experiments/service.py`
- Modify: `agent_orchestration/app/experiments/schemas.py`
- Test: `tests/test_experiment_service.py`

- [ ] non-PASSED `409`, 필수 reason과 우회 승격 차단 테스트를 작성한다.
- [ ] 재요청 성공과 다른 payload 충돌 테스트를 작성한다.
- [ ] promotion transaction과 event를 최소 구현한다.
- [ ] 관련 테스트와 lint를 통과하고 Task 커밋을 만든다.

### Task 8: Router, 인증과 OpenAPI

**Files:**
- Create: `agent_orchestration/app/experiments/router.py`
- Modify: `agent_orchestration/app/main.py`
- Test: `tests/test_experiment_router.py`
- Test: `tests/test_agent_orchestration.py`

- [ ] 10개 endpoint의 성공·오류·인증·OpenAPI 실패 테스트를 작성한다.
- [ ] router, exception handler와 기존 토큰 dependency를 연결한다.
- [ ] 기존 `/chat` 회귀 테스트를 통과한다.
- [ ] 관련 테스트와 lint를 통과하고 Task 커밋을 만든다.

### Task 9: 전체 회귀 검증

**Files:**
- Modify: `agent_orchestration/README.md`
- Modify: `docs/README.md`

- [ ] API 사용법과 spec 링크를 문서화한다.
- [ ] `uv run python -m pytest`를 실행한다.
- [ ] `uv run --no-sync ruff check agent_orchestration autoresearch tests tools`를 실행한다.
- [ ] `git diff --check`를 실행한다.
- [ ] 최종 결과를 보고하고 branch 마무리 방식을 확인받는다.
