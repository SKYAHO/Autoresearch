# Agent Orchestration 실험 워크벤치 v0 계약

## 목적

Issue #448의 FastAPI 실험 API가 가설, 상태 전이, 판단 지표, 구조화 이벤트와 원본
로그를 PostgreSQL에 저장하고 Streamlit 워크벤치의 1초 폴링을 지원하도록 공개 계약을
정의한다. 기존 `POST /chat`의 요청·응답·인증·저장 계약은 변경하지 않는다.

## 범위

- 실험 생성·목록·상세·상태·이벤트·로그·메타데이터 API
- 운영자가 배포 완료를 확인한 뒤 수행하는 수동 승격 API
- SQLAlchemy 2.x ORM, 요청 단위 동기 Session, Alembic migration
- Event와 Log 쓰기의 멱등성

Streamlit UI 구현, GitHub webhook 자동 승격, 사람과 에이전트의 인증 분리, 기존
`/chat`의 SQLAlchemy 이관은 범위 밖이다.

## 기존 `/chat` 비변경 계약

`agent_orchestration/app/main.py`, `schemas.py`, `db.py`의 현재 동작을 보존한다.

- `POST /chat`, 성공 `201 Created`
- 필수 `X-Orch-Token`
- 요청은 `prompt` 한 필드이며 1~8192자, 추가 필드 금지
- 응답 필드는 `id`, `prompt`, `response`, `model`, `latency_ms`,
  `token_count`, `created_at`
- 기존 `psycopg` 기반 `chat_interactions` 저장 유지
- 오류 `401`, `422`, `500`, `502`, `503` 유지
- 멱등 키가 없으므로 동일 프롬프트 자동 재시도 금지

신규 실험 API도 v0에서는 같은 `X-Orch-Token`을 사용한다. 이는 운영 편의를 위한
임시 결정이며 사용자 인증이나 역할 기반 권한을 뜻하지 않는다.

## 상태 계약

상태는 `CREATED`, `RUNNING`, `EVALUATING`, `PASSED`, `FAILED`, `ERROR`,
`PROMOTED` 중 하나다. 허용 전이는 다음과 같다.

```text
CREATED    -> RUNNING
RUNNING    -> EVALUATING | ERROR
EVALUATING -> PASSED | FAILED | ERROR
PASSED     -> PROMOTED
```

`FAILED`, `ERROR`, `PROMOTED`는 터미널 상태다. 동일 상태 재요청, 역행, 단계
건너뛰기, 터미널 상태의 재전이는 `409 Conflict`다. `FAILED`는 지표·통계 판정
미달이고, `ERROR`는 실행 또는 평가 과정의 코드·인프라 오류다.

`PASSED -> PROMOTED`는 도메인에서는 유효하지만 `PATCH /status`와
`POST /events`에서는 허용하지 않는다. 운영자가 PR merge와 prod 배포 완료를 확인한
뒤 `POST /promote`로만 수행한다.

## 데이터 모델

### Experiment

- `id: UUID`
- `hypothesis: text`
- `status: ExperimentStatus`, 기본값 `CREATED`
- `metric_summary: JSON object | null`
- `agent_session_id: string(64) | null`; `/chat` 스키마를 확장하지 않는 연계 식별자
- `created_at`, `updated_at`: timezone-aware server timestamp

### ExperimentEvent

- `id`, `experiment_id`: UUID
- `idempotency_key: string(128)`
- `request_fingerprint: string(64)`; canonical payload의 SHA-256 hex digest
- `from_status: ExperimentStatus | null`
- `to_status: ExperimentStatus`
- `reason: text | null`
- `metric_snapshot: JSON object | null`
- `created_at`: timezone-aware server timestamp
- unique `(experiment_id, idempotency_key)`

생성 시 서버가 `from_status=null`, `to_status=CREATED`인 최초 event를 기록한다.
이 서버 생성 event의 idempotency key는 `experiment-created:<experiment_id>`다.

### ExperimentLog

- `id`, `experiment_id`: UUID
- `idempotency_key: string(128)`
- `request_fingerprint: string(64)`
- `log_type: string(32)`
- `content: text`
- `created_at`: timezone-aware server timestamp
- unique `(experiment_id, idempotency_key)`

### ExperimentMetadata

- `id`, `experiment_id`: UUID
- `key: string(64)`
- `value: text`
- unique `(experiment_id, key)`

## 멱등성 계약

Event, Log, Promote 요청은 클라이언트가 1~128자의 `idempotency_key`를 보낸다.
서버는 키를 제외한 의미 있는 요청 payload를 키 정렬·공백 없는 canonical JSON으로
직렬화한 뒤 SHA-256 fingerprint를 저장한다.

- 새 key: 요청을 한 번 처리하고 결과를 저장한다.
- 같은 key와 같은 fingerprint: 저장된 기존 결과를 반환한다.
- 같은 key와 다른 fingerprint: `409 Conflict`를 반환한다.
- 동시 요청: unique constraint를 최종 방어선으로 사용한다.

상태 변경, event 생성과 멱등성 기록은 하나의 transaction에서 수행한다. Event와 Log
응답 및 GET 목록에는 `idempotency_key`를 포함하되 fingerprint는 내부 필드로 숨긴다.

## API 계약

모든 요청 모델은 추가 필드를 금지한다. UUID 또는 body 검증 실패는 `422`, 인증 실패는
`401`, 없는 실험은 `404`다.

### `POST /experiments`

요청은 `hypothesis: str`, 선택적 `agent_session_id: str | null`, 기본 빈 객체인
`metadata: dict[str, str]`다. 서버가 상태를 `CREATED`로 정하고 최초 event와 metadata를
같은 transaction에 저장한다. 성공은 `201`과 Experiment 응답이다.

### `GET /experiments`

`limit=50`(1~100), `offset=0`(0 이상), 선택적 `status`를 받는다. 응답은 `items`,
`total`, `limit`, `offset`이며 `created_at DESC, id DESC`로 정렬한다.

### `GET /experiments/{id}`

Experiment를 반환한다. Event와 Log는 중첩하지 않는다.

### `PATCH /experiments/{id}/status`

요청은 `status`, 선택적 `reason`, 선택적 `metric_snapshot`이다. `status` 타입은
`RUNNING | EVALUATING | PASSED | FAILED | ERROR`만 허용한다. row lock 뒤 상태를
검증하고 상태 갱신과 event 생성을 같은 transaction에서 수행한다. 이 endpoint는
서버가 생성하는 operation key를 사용하며 클라이언트 멱등 API로 제공하지 않는다.

### `POST /experiments/{id}/events`

요청은 `idempotency_key`, `to_status`, 선택적 `reason`, 선택적 `metric_snapshot`이다.
`to_status`는 `PROMOTED`를 허용하지 않는다. PATCH와 같은 상태 전이 service를 호출하며
성공은 `201`과 Event 응답이다.

### `GET /experiments/{id}/events`

`limit=100`(1~200), 선택적 `after_id`를 받는다. `created_at ASC, id ASC` 순으로
`items`, `next_cursor`를 반환한다.

### `POST /experiments/{id}/logs`

요청은 `idempotency_key`, `log_type`, `content`다. 터미널 상태에서도 로그를 추가할 수
있다. 성공은 `201`과 Log 응답이다.

### `GET /experiments/{id}/logs`

`limit=100`(1~100), 선택적 `after_id`, 선택적 `log_type`을 받는다. `created_at ASC,
id ASC` 순으로 정렬하며 동일 timestamp에서는 UUID `id`를 tie-breaker로 사용한다.
`after_id`가 가리키는 `(created_at, id)`보다 뒤의 `items`와 마지막 item의 ID인
`next_cursor`를 반환한다.

### `GET /experiments/{id}/metadata`

`experiment_id`와 key-value row를 합친 `entries: dict[str, str]`를 반환한다.

### `POST /experiments/{id}/promote`

요청은 필수 `idempotency_key`, 필수 `reason`, 선택적 `deployment_metadata`다.
`reason`은 공백 제거 후 한 글자 이상이어야 하며 운영자가 확인한 merge·배포 근거를
반드시 남긴다. 현재 상태가 `PASSED`가 아니면 `409`다. 성공 시 `PROMOTED` 상태와
promotion event를 한 transaction에 저장하고 `200` Experiment 응답을 반환한다.

## DB와 migration

기존 `db.py`의 `/chat`용 psycopg 코드는 유지한다. 신규 `database.py`가 SQLAlchemy
engine, session factory와 요청 단위 dependency를 소유한다. 동기 Session을 사용하므로
실험 endpoint는 동기 함수로 실행한다. 실험 테이블은 Alembic만 생성하며 startup
`create_all()`을 사용하지 않는다.

UUID는 PostgreSQL `gen_random_uuid()` server default를 migration과 ORM의 정본으로
사용한다. 배포 전 대상 PostgreSQL에서 함수 사용 가능 여부를 migration 검증으로
확인한다. unique constraint가 만드는 `(experiment_id, idempotency_key)` 인덱스를
별도 중복 생성하지 않는다.

## 오류와 OpenAPI

- `ExperimentNotFoundError` -> `404`
- `InvalidTransitionError` -> `409`
- `IdempotencyConflictError` -> `409`
- Pydantic/FastAPI validation -> `422`

도메인 오류 응답은 `{"detail": "..."}`이며 SQL, 자격 증명, 내부 traceback을 노출하지
않는다. 각 operation의 `responses`가 실제 handler 및 FastAPI 기본 validation 응답과
일치하는지 OpenAPI 테스트로 검증한다.

## Streamlit polling

워크벤치는 1초마다 상세, 새 Event, 새 Log를 조회한다.

```text
GET /experiments/{id}
GET /experiments/{id}/events?after_id=<last_event_id>
GET /experiments/{id}/logs?after_id=<last_log_id>
```

metadata는 실험 선택 시 한 번 조회한다. 터미널 상태에서는 마지막 Event와 Log를 한 번
더 가져온 뒤 polling을 중단한다. `PASSED`는 수동 승격 대기 상태이므로 polling을
유지한다.

## 완료 조건

- 허용·거부 상태 전이가 서버에서 검증되고 위반은 `409`다.
- 일반 상태 endpoint로 `PROMOTED`를 만들 수 없다.
- Event, Log, Promote의 같은 key·같은 payload 재요청은 중복 row를 만들지 않는다.
- 같은 key·다른 payload는 `409`다.
- Swagger가 성공·인증·not-found·conflict·validation 응답을 정확히 표시한다.
- 기존 `/chat` 회귀 테스트가 유지된다.
