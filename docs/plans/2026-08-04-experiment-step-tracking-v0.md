# 실험 Step 추적 v0 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement
> this plan task-by-task. 각 Task 완료 후 사용자 확인을 받고 다음 Task로 진행한다.
> **Task 3은 하드 스톱이다 — 별도 승인 없이 Task 4로 넘어가지 않는다.**

**Goal:** `docs/specs/2026-08-04-experiment-step-tracking-v0.md`의 `ExperimentStep`
리소스를 기존 Experiment/Event/Log/Metadata 계약을 깨지 않고 구현한다.

**Architecture:** 기존 `agent_orchestration.app.experiments` feature package에 Step을
추가한다. 생성 경로는 `create_experiment_log`의 "row lock 없이 존재 확인 → INSERT →
IntegrityError 복구" 구조를 그대로 따르고, 갱신 경로는 터미널 확정 가드 + 전체 교체
시맨틱을 새로 도입한다. `experiments.status`는 어느 경로에서도 변경하지 않는다.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic,
PostgreSQL 15, pytest, uv.

## Global Constraints

- 계약 정본은 spec이다. 구현과 spec이 어긋나면 **먼저 spec을 갱신**하고 구현한다.
- Step 경로는 `experiments.status`와 `metric_summary`를 변경하지 않는다.
- `PATCH`는 전체 교체다. 생략된 선택적 필드는 `null`로 갱신한다.
- 응답 스키마에 relationship 필드를 넣지 않는다 (expunge-before-rollback 전제).
- `request_fingerprint`는 응답에 노출하지 않는다.
- 새 최상위 디렉토리·이미지·공개 CLI·필수 환경 변수를 도입하지 않는다.

### 동시성 검증 조건 (과거 보류 재발 방지)

과거 Task 4/5가 "SQLite에서 `FOR UPDATE`·unique constraint 복구가 신뢰성 있게 검증되지
않음"으로 보류됐다. 이번 구현은 같은 종류의 로직(IntegrityError 복구, expunge/rollback
순서, fingerprint 재계산 비교)을 다시 쓰므로 아래를 **Task 완료 기준에 포함**한다.

- **SQLite 테스트 통과만으로 어떤 Task도 완료 처리하지 않는다.** 동시성이 걸리는 Task 3과
  Task 4는 실제 PostgreSQL 검증을 통과해야 완료다.
- **Step 동시성 테스트는 반드시 `tests/test_experiment_postgres.py`에 추가한다.** CI
  `pytest-postgres` job이 실행하는 대상은 이 **파일 하나뿐**이다
  (`.github/workflows/ci.yml`: `pytest tests/test_experiment_postgres.py -v`). 새 파일로
  분리하면 CI가 조용히 실행하지 않는다. 굳이 분리하려면 같은 PR에서 `ci.yml`을 함께 고친다.
- **로컬 초록불은 검증이 아니다.** `postgres_engine` fixture는 `ORCH_TEST_POSTGRES_URL`이
  없으면 `pytest.skip()`한다. 즉 그냥 `pytest`를 돌리면 동시성 테스트는 **skip된 채로
  초록불**이 뜬다. `-rs`로 skip 목록을 확인해 대상 테스트가 skip이 아니라 pass임을 눈으로
  확인한 뒤 완료 보고한다.
- **기존 SQLite "concurrent" 테스트를 동시성 검증으로 오인하지 않는다.**
  `tests/test_experiment_service.py`에는 파일 기반 SQLite로 동시 요청을 흉내 내는 테스트가
  이미 있다. 이 초록불은 PostgreSQL 계약을 보장하지 않는다.

로컬 PostgreSQL 검증 절차:

```bash
docker compose -f agent_orchestration/docker-compose.yml up -d --wait
ORCH_DATABASE_URL=<url> uv run alembic -c agent_orchestration/alembic.ini upgrade head
ORCH_TEST_POSTGRES_URL=<url> uv run python -m pytest tests/test_experiment_postgres.py -v -rs
```

---

## File Structure

| File | Responsibility |
| --- | --- |
| `agent_orchestration/app/experiments/models.py` | `ExperimentStep` ORM, `StepKind`/`StepStatus` enum, CHECK·unique·index |
| `agent_orchestration/migrations/versions/0002_create_experiment_steps.py` | `experiment_steps` 테이블 생성·제거 |
| `agent_orchestration/app/experiments/schemas.py` | Step 생성·갱신·응답·page Pydantic 계약 |
| `agent_orchestration/app/experiments/repository.py` | Step 조회와 cursor query |
| `agent_orchestration/app/experiments/service.py` | 생성 멱등성, 터미널 가드, 정규화 비교 |
| `agent_orchestration/app/experiments/router.py` | `POST`/`GET`/`PATCH` HTTP 경계 |
| `tests/test_experiment_step_service.py` | 생성·갱신 service 단위 테스트 (SQLite) |
| `tests/test_experiment_step_router.py` | HTTP 계약·OpenAPI 응답 테스트 (SQLite) |
| `tests/test_experiment_postgres.py` | **Step 동시성 계약 (PostgreSQL 전용, CI 실행 대상)** |
| `agent_orchestration/ui/{models,client,state,views}.py` | Step polling과 렌더 |
| `agent_orchestration/README.md` | 신규 endpoint 3개 문서화 |

---

### Task 1: 계약 문서 확정 (spec + plan)

**Files:**
- Create: `docs/specs/2026-08-04-experiment-step-tracking-v0.md`
- Create: `docs/plans/2026-08-04-experiment-step-tracking-v0.md`

- [x] spec 작성 — 데이터 모델, `step_kind`/`step_type` 분리, `target` 계약, 생성 멱등성,
      PATCH 터미널 가드·전체 교체, 3컬럼 인덱스 근거, 알려진 한계
- [x] plan 작성 — Task 분해, 체크포인트, 동시성 검증 조건
- [ ] 이슈 발행 → 브랜치 생성 → **그 브랜치에서** spec + plan을 하나의 커밋으로 확정

**체크포인트:** 선례 40fdf42는 spec+plan 동시 커밋이지만 main에서 도달 불가한 커밋이다 —
이슈 브랜치에서 만들어져 PR로 squash됐다. main의 다른 커밋이 모두 `(#NNN)` squash 접미사를
가진 것과 일치한다. 따라서 docs 커밋도 main에 직접 올리지 않는다.

순서는 다음과 같다.

1. 이슈 발행 — 완료 조건은 spec "완료 조건" 섹션의 **텍스트를 직접 기입**한다. 이 시점에는
   spec 파일이 아직 어디에도 커밋되지 않았으므로 파일 링크를 넣으면 깨진 링크로 남는다
2. 이슈의 `Create a branch`로 브랜치 생성
3. 그 브랜치에서 spec + plan 커밋 (경로를 명시해 add — 작업 트리에 추적되지 않는
   `.tmp/`·`artifacts/`·`data/` 등이 섞여 있다)
4. PR을 열 때 spec/plan 파일 링크를 이슈·PR 본문에 추가
5. Task 2 착수

### Task 2: ORM 모델과 Alembic migration

**Files:**
- Modify: `agent_orchestration/app/experiments/models.py`
- Create: `agent_orchestration/migrations/versions/0002_create_experiment_steps.py`
- Create: `tests/test_experiment_step_service.py` (모델 수준 테스트만 우선)

**Consumes:** spec "데이터 모델", "인덱스는 3컬럼".

**Produces:** `ExperimentStep`, `StepKind`, `StepStatus`, `experiment_steps` 테이블.

- [ ] `StepKind`(`FEATURE_ASSEMBLY`/`FEATURE_DERIVE`/`TRAIN`/`EVALUATE`/`OTHER`)와
      `StepStatus`(`STARTED`/`PROGRESS`/`COMPLETED`/`FAILED`)를 `ExperimentStatus`와 같은
      방식으로 정의하고 CHECK SQL을 같은 패턴으로 생성한다
- [ ] `ExperimentStep` ORM에 `idempotency_key(128)`, `request_fingerprint(64)`,
      `step_kind(32)`, `step_type(64)`, `status(16)`, `message(500)`, `target` JSONB,
      `created_at`, `updated_at`을 정의한다
- [ ] `updated_at`에 `onupdate=func.now()`를 두고, **ORM 경유 UPDATE에만 적용되는 애플리케이션
      레벨 동작**임을 모듈 docstring에 `Experiment.updated_at`과 같은 형식으로 명시한다
- [ ] unique `(experiment_id, idempotency_key)`, index `(experiment_id, created_at, id)`를
      정의한다. unique constraint가 만드는 인덱스를 중복 생성하지 않는다
- [ ] migration을 `revision="0002_experiment_steps"`,
      `down_revision="0001_experiment_tables"`로 작성하고 `downgrade()`에서 index → table
      순으로 제거한다
- [ ] ORM 정의와 migration DDL이 컬럼·제약·인덱스까지 일치하는지 대조한다
- [ ] 모듈 docstring을 갱신한다 (모듈 책임 규칙)

**완료 기준:** `uv run python -m pytest tests/test_experiment_step_service.py -v` 통과 +
로컬 PostgreSQL에 `alembic upgrade head` → `downgrade` → `upgrade` 왕복 성공.

**체크포인트:** 스키마가 굳으면 이후 Task가 전부 여기에 의존하므로, 승인 후 Task 3으로 넘어간다.

### Task 3: 생성 경로와 멱등성 (하드 스톱)

**Files:**
- Modify: `agent_orchestration/app/experiments/{schemas,service,router}.py`
- Modify: `tests/test_experiment_step_service.py`
- Modify: `tests/test_experiment_postgres.py`

**Consumes:** Task 2 모델, `_request_fingerprint()`.

**Produces:** `create_experiment_step()`, `POST /experiments/{id}/steps`.

- [ ] `ExperimentStepCreate`/`ExperimentStepResponse`를 `extra="forbid"`로 정의하고
      `request_fingerprint`를 응답에서 제외한다
- [ ] `target`을 직렬화 후 4096 byte로 제한하는 validator를 추가한다
- [ ] `create_experiment_log`의 구조를 그대로 따른다 — row lock 없이 실험 존재 확인 → 기존
      key 조회 → INSERT → `IntegrityError` 복구. `for_update`를 쓰지 않는다
- [ ] fingerprint payload는 `{step_kind, step_type, status, message, target}` 5개로 고정한다
- [ ] **`session.expunge(row)`를 `session.rollback()`보다 먼저 호출한다.** 이유를 원본과
      같은 수준으로 주석에 남긴다
- [ ] SQLite 테스트: 같은 key·같은 payload 재요청이 중복 row를 만들지 않음, 같은 key·다른
      payload가 `409`, `target` 초과가 `422`
- [ ] **PostgreSQL 테스트를 `tests/test_experiment_postgres.py`에 추가한다** — 같은
      `idempotency_key`로 동시 `POST`를 `ThreadPoolExecutor`로 보내 row가 정확히 1건이고
      두 응답이 같은 `id`를 반환하는지 검증한다 (기존 Log 동시성 테스트와 같은 형태)

**완료 기준:** SQLite 테스트 통과 + **`ORCH_TEST_POSTGRES_URL`을 설정한 상태에서**
`pytest tests/test_experiment_postgres.py -v -rs` 통과. 새로 추가한 테스트가 skip이 아니라
pass임을 skip 리포트로 확인한다.

**체크포인트 (하드 스톱):** 동시성이 실제로 걸리는 지점이다. 위 완료 기준의 PostgreSQL
결과를 제출하고 **별도 승인을 받은 뒤에만** Task 4로 진행한다.

### Task 4: PATCH 경로 — 터미널 가드와 전체 교체

**Files:**
- Modify: `agent_orchestration/app/experiments/{schemas,service,router}.py`
- Modify: `tests/test_experiment_step_service.py`
- Modify: `tests/test_experiment_postgres.py`

**Consumes:** Task 3 생성 경로, `_request_fingerprint()`.

**Produces:** `update_experiment_step()`, `PATCH /experiments/{id}/steps/{step_id}`.

- [ ] `ExperimentStepUpdate`를 `status` 필수, `message`/`target` 선택으로 정의한다
- [ ] **`target` 4096 byte 제한을 Task 3과 동일한 validator로 재사용한다.** 생성 스키마에만
      붙이면 PATCH로 무제한 `target`이 들어오는 구멍이 생겨, spec이 든 제한 근거(저장된
      상태가 1초 polling으로 반복 조회됨)가 성립하지 않는다. 두 스키마가 공유하는 위치
      (공통 base 또는 공용 validator 함수)에 둔다
- [ ] **전체 교체**로 구현한다 — 생략된 선택적 필드는 이전 값 유지가 아니라 `null`로 갱신
- [ ] 터미널(`COMPLETED`/`FAILED`) 가드: 요청과 현재 저장 값의 정규화 비교가 동일하면 `200`과
      현재 row, 다르면 `409`
- [ ] **모든 갱신 UPDATE에 `WHERE status NOT IN ('COMPLETED','FAILED')`를 건다.**
      (구현 중 상향 조정 — 좁은 형태는 stale 세션 경로로 확정 결과가 덮인다. spec
      "터미널 전이는 원자적이어야 한다" 참조)
      매치 0건이면 현재 row를 다시 읽어 터미널 가드를 적용한다(동일 payload `200`, 다르면
      `409`). 검사-후-실행 사이의 창을 없애기 위한 조건이며, 비터미널 → 비터미널 갱신에는
      걸지 않는다
- [ ] **0건 매치 후의 재조회는 세션 캐시가 아니라 DB에서 새로 SELECT한 값이어야 한다.**
      `session.refresh()` 또는 expire 후 재조회를 사용한다. 세션이 이미 들고 있던 객체를
      그대로 비교하면 방금 커밋된 다른 트랜잭션의 값을 못 보고 낡은 값으로 판정한다
      (`expunge`/`rollback` 순서 함정과 같은 계열)
- [ ] 비터미널(`STARTED`/`PROGRESS`)은 어느 상태로든 갱신을 허용한다. 전이 그래프를 만들지 않는다
- [ ] 정규화 비교는 `_request_fingerprint()`에 `{status, message, target}`를 넘겨 계산한다.
      **저장된 `request_fingerprint` 컬럼과 비교하지 않는다** — 그 값은 생성 payload
      (`step_kind`/`step_type` 포함)의 digest라 key 집합이 다르다
- [ ] SQLite 테스트: 터미널 동일 payload 재시도 `200`, 터미널 다른 payload `409`, 비터미널
      왕복 허용, `target`의 key 순서만 다른 요청이 동일 판정, 생략 필드가 `null`로 갱신,
      **`target` 4096 byte 초과가 `422`**
- [ ] PostgreSQL 테스트: 터미널 Step에 동시 PATCH를 보내 한쪽이 `200`, 다른 쪽이 `200`
      (동일 payload) 또는 `409`(다른 payload)로 결정적으로 갈리는지 검증한다
- [ ] **PostgreSQL 테스트: `PROGRESS` Step에 `COMPLETED`와 `FAILED`를 동시에 보내** 정확히
      한쪽만 확정되고 다른 쪽은 `409`인지, 최종 저장 상태가 이긴 쪽과 일치하는지 검증한다

**완료 기준:** Task 3과 동일 — SQLite + PostgreSQL 양쪽, skip 아님 확인.

**체크포인트:** 승인 후 Task 5로 진행한다.

### Task 5: 조회 경로 — cursor pagination과 필터

**Files:**
- Modify: `agent_orchestration/app/experiments/{schemas,repository,router}.py`
- Modify: `tests/test_experiment_step_router.py`

**Consumes:** Task 2 인덱스, 기존 `find_experiment_logs` cursor 규칙.

**Produces:** `find_experiment_steps()`, `GET /experiments/{id}/steps`.

- [ ] `find_experiment_logs`와 동일한 keyset 규칙으로 구현한다 —
      `created_at > c OR (created_at = c AND id > c.id)`, `ORDER BY created_at ASC, id ASC`
- [ ] `limit`(1~100), 선택적 `after_id`, 선택적 `step_kind` 필터를 받는다
- [ ] `after_id`가 해당 실험에 없으면 `InvalidCursorError` → `404`
- [ ] `next_cursor`를 마지막 item의 id로 반환한다
- [ ] OpenAPI `responses`가 실제 handler 응답과 일치하는지 테스트한다

**완료 기준:** `uv run python -m pytest tests/test_experiment_step_router.py -v` 통과.

### Task 6: Streamlit 소비와 문서 갱신

**Files:**
- Modify: `agent_orchestration/ui/{models,client,state,views}.py`
- Modify: `tests/test_agent_orchestration_ui_{client,state,views}.py`
- Modify: `agent_orchestration/README.md`

**Consumes:** Task 5 조회 계약.

**Produces:** Step polling과 진행 표시.

- [ ] `client.get_steps(experiment_id, after_id)`를 기존 Event/Log와 같은 형태로 추가한다
- [ ] 1초 polling에 Step page 조회를 추가하고 `next_cursor`를 cursor로 전진시킨다
- [ ] `step_kind`로 렌더 경로를 결정하고 `step_type`은 라벨로만 표시한다
- [ ] **`message`가 `null`이면 `step_kind`+`step_type` 라벨을 대신 표시한다** — 한 줄 표시가
      비는 경우가 없어야 한다
- [ ] 터미널 상태 실험에서 Step page를 한 번 더 가져온 뒤 polling을 중단한다
- [ ] `README.md`의 "실험 워크벤치 v0" 절에 신규 endpoint 3개와 PATCH 전체 교체 시맨틱을
      추가한다

**완료 기준:** UI 테스트 통과 + `message` 없는 Step이 폴백 라벨로 렌더되는 테스트 존재.

---

## 최종 검증

CI job과 동일한 명령으로 확인한다.

```bash
uv run python -m pytest -v
uv run --no-sync ruff check agent_orchestration autoresearch tests tools
ORCH_TEST_POSTGRES_URL=<url> uv run python -m pytest tests/test_experiment_postgres.py -v -rs
docker build -f Dockerfile.app -t autoresearch:ci .
```

- 위 세 번째 명령의 skip 리포트에 Step 테스트가 없어야 한다.
- 구현이 끝나면 spec과 plan을 `docs/archive/`로 옮긴다 (`docs/README.md` 규칙).

## Plan Self-Review

- **`step_kind` 목록이 잠정이다.** 에이전트 실행기가 없어 실제 스텝 구분을 코드로 대조할 수
  없다. Task 2에서 enum을 굳히므로, 실행기 구현 시 값 추가가 필요하면 migration이 한 번 더
  필요하다. `OTHER` 폴백으로 그 시점까지 버틴다.
- **cursor 경합은 이 계획이 해결하지 않는다.** spec "알려진 한계"에 기록된 기존 문제를 Step도
  상속한다. 3컬럼 인덱스는 커버리지 개선이지 해결이 아니다.
- **비터미널 구간의 진행 역행·lost update는 Task 4가 막지 않는다.** 비터미널 자유 전이의
  의도된 결과이며, 완화가 필요해지면 `updated_at` 조건부 갱신으로 별도 처리한다. 반면
  **터미널 확정 경합은 조건부 UPDATE로 막는다** — 확정된 결과가 조용히 뒤집히는 것은
  가드의 목적 자체를 무력화하므로 감수 대상에 넣지 않았다.
- **Task 6은 실행기 없이는 실데이터로 확인할 수 없다.** Step을 쓰는 주체가 아직 없으므로 UI
  검증은 테스트와 수동 `POST`로만 가능하다. 실행기 연동은 후속 이슈다.
