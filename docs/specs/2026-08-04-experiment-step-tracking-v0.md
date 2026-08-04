# 실험 Step 추적 v0 계약

## 목적

에이전트가 실험 한 건을 수행하는 **도중에** 무엇을 하고 있는지를 프론트가 실시간으로
표시할 수 있게 한다. 구체적으로 "어떤 피처를 조립 중인지", "새 파생 피처를 만드는지",
"어떤 모델에 무엇을 바꿔 학습 중인지"를 구조화된 리소스로 기록·조회한다.

## 배경 — 기존 API로 표현할 수 없는 이유

실험 워크벤치 v0(`docs/archive/specs/2026-08-01-agent-orchestration-experiment-workbench-v0.md`)의
네 리소스 중 어느 것도 이 용도를 담당하지 않는다. 코드 대조 결과는 다음과 같다.

| 리소스 | 표현 불가 사유 |
| --- | --- |
| Event | `to_status`가 `Literal[RUNNING, EVALUATING, PASSED, FAILED, ERROR]`로 고정(`schemas.py`). 실험 **생명주기 상태**이며 그 안의 작업 스텝을 담을 타입이 없다 |
| Log | `log_type`이 32자 자유 문자열이고 허용값 정의도 서버 검증도 없다. `content`는 평문 텍스트로, 구조화 필드가 없다 |
| Metadata | 쓰기 경로가 `create_experiment()` 하나뿐이고 갱신 endpoint가 없다. 실행 **도중** 갱신이 불가능하다 |
| Experiment | `metric_summary`는 평가 결과이며 진행 상태가 아니다 |

기존 `log_type`·`to_status`에 새 의미를 얹는 방식은 채택하지 않는다. 두 필드 모두
"스텝을 표현한다"고 계약된 적이 없으므로, 거기에 의미를 부여하면 파싱 규칙이 코드가
아니라 암묵적 관례로만 존재하게 된다. 따라서 **별도 `ExperimentStep` 리소스를 신설**한다.

## 범위

- `ExperimentStep` 테이블과 Alembic migration
- `POST /experiments/{id}/steps` (생성, 멱등)
- `GET /experiments/{id}/steps` (cursor polling)
- `PATCH /experiments/{id}/steps/{step_id}` (진행·완료 갱신)

## 비범위

- 에이전트 실행기가 이 endpoint를 실제로 호출하는 구현 (실행기 자체가 아직 없다)
- Streamlit 화면 구현 — 본 문서는 소비 계약만 정의한다
- 기존 Event/Log/Metadata 계약 변경
- Step 단위 재실행·취소·되감기

## 데이터 모델

### ExperimentStep

- `id`, `experiment_id`: UUID
- `idempotency_key: string(128)`
- `request_fingerprint: string(64)`; **생성** 시점 canonical payload의 SHA-256 hex digest
- `step_kind: string(32)`; 닫힌 enum, CHECK constraint로 강제
- `step_type: string(64)`; 자유 문자열
- `status: string(16)`; `STARTED | PROGRESS | COMPLETED | FAILED`, CHECK constraint로 강제
- `message: string(500)`; 사람이 읽는 짧은 요약 (실시간 표시용)
- `target: JSON object | null`; 구조화된 부가 정보, 직렬화 후 4096 byte 이하
- `created_at`, `updated_at`: timezone-aware server timestamp
- unique `(experiment_id, idempotency_key)`
- index `(experiment_id, created_at, id)`

`updated_at`의 `onupdate=func.now()`는 **ORM을 거치는 UPDATE에만 적용되는 애플리케이션
레벨 동작**이며 DB 트리거가 아니다. `psql` 직접 UPDATE 등 ORM 우회 쓰기에는 적용되지
않는다 (`models.py` 모듈 docstring의 `Experiment.updated_at` 주의사항과 동일).

## step_kind와 step_type

두 필드로 나누는 이유는 **프론트의 렌더 경로와 에이전트의 자유도를 분리**하기 위해서다.

- `step_kind` — 닫힌 enum. 프론트는 **오직 이 값으로만** 그룹핑·아이콘·정렬을 결정한다
- `step_type` — 자유 문자열. 에이전트가 각 단계에서 필요한 세부 라벨을 자율적으로 남긴다

이 분리로 모르는 `step_type`이 들어와도 렌더 경로가 이미 정해져 있어 조용히 누락되지
않으며, 프론트에만 존재하는 암묵적 허용값 목록이 생기지 않는다.

`step_kind` 값은 `ExperimentStatus`와 동일한 방식으로 `models.py`에 enum과 CHECK SQL을
두고 `schemas.py`가 그로부터 `Literal`을 파생시킨다. 새 패턴을 만들지 않는다.

```text
FEATURE_ASSEMBLY | FEATURE_DERIVE | TRAIN | EVALUATE | OTHER
```

**이 목록은 잠정이다.** 에이전트 실행기 코드가 아직 없어 실제 스텝 구분을 코드로 대조할
수 없으므로, 실행기 구현 시점에 한 번 더 검토해 확정한다. `OTHER`는 그때까지의 폴백이자
이후로도 미분류 스텝의 수용 경로다.

## target 계약

- 서버는 **JSON object로 파싱 가능한지와 크기만** 검증하고 내부 필드 스키마는 강제하지
  않는다. pydantic `dict` 타입이 파싱 검증을 담당하므로 별도 구현이 필요 없다
  (`metric_snapshot`, `deployment_metadata`가 쓰는 기존 패턴과 동일)
- 직렬화 후 4096 byte를 넘으면 `422`. 이 제한은 **생성과 갱신 양쪽에 동일하게** 적용한다 —
  제한의 근거가 polling으로 반복 조회되는 **저장된 상태**의 크기이므로, 한쪽 경로만 막으면
  근거가 성립하지 않는다
- 실시간 한 줄 표시는 `message`가 담당한다. `target`은 **있으면 더 보여주는 부가 구조화
  정보**이며, 프론트는 `target` 없이도 화면을 구성할 수 있어야 한다

### 크기 제한을 주변과 다르게 둔 근거

`metric_summary`·`metric_snapshot`·`deployment_metadata`는 `JSONB`로만 선언되어 크기
제한이 전혀 없다(`migrations/versions/0001_create_experiment_tables.py`). `target`에만
제한을 두는 것은 새 규칙이며, 근거는 **호출 빈도**다. 앞의 세 필드는 실험당 수 회
기록되지만 `target`은 스텝마다 기록되고 1초 polling으로 반복 조회되므로, 상한 없이
두면 polling 응답 크기가 실험 진행에 따라 선형으로 커진다. 텍스트 필드 상한(8192자)이
아니라 4096 byte를 택한 것도 같은 이유이며, 이 값은 실행기 구현 후 실측으로 조정할 수 있다.

## 생성 멱등성 계약

Event·Log·Promote와 동일하게 클라이언트가 1~128자 `idempotency_key`를 보낸다. 서버는
키를 제외한 의미 있는 payload(`step_kind`, `step_type`, `status`, `message`, `target`)를
키 정렬·공백 없는 canonical JSON으로 직렬화해 SHA-256 fingerprint를 저장한다.

- 새 key: 요청을 한 번 처리하고 결과를 저장한다
- 같은 key와 같은 fingerprint: 저장된 기존 row를 반환한다
- 같은 key와 다른 fingerprint: `409`
- 동시 요청: unique constraint를 최종 방어선으로 사용한다

**생성 경로는 `create_experiment_log`의 구조를 따른다** — row lock 없이 실험 존재 확인 →
기존 key 조회 → INSERT → `IntegrityError` 복구. Step은 `experiments.status`를 변경하지
않으므로 `_transition_experiment`의 `for_update` row lock이나 `promote_experiment`의
이중 방어는 사용하지 않는다.

`idempotency_key`는 응답에 포함하고 `request_fingerprint`는 내부 필드로 숨긴다.

## 갱신(PATCH) 멱등성 계약

에이전트 실행기는 자동 재시도가 가능한 호출자이므로, 갱신 경로도 재시도에 안전해야
한다. `PATCH /experiments/{id}/status`가 멱등하지 않아 생긴 문제를 Step에서 반복하지
않기 위해 **터미널 확정 가드**를 둔다.

### PATCH는 전체 교체다

**PATCH는 부분 병합이 아니라 전체 교체다** — 요청에 없는 선택적 필드는 "이전 값 유지"가
아니라 현재 상태 전체를 재진술한 결과로 간주하며, 생략된 `message`·`target`은 `null`로
갱신한다. 에이전트는 갱신할 때마다 그 시점의 상태 전체를 보낸다.

부분 병합을 택하지 않는 근거는 둘이다.

- 재시도 판정이 "요청에 포함된 필드 집합"에 의존하게 되어, 같은 논리적 재시도인데 필드
  집합만 다르면(최초에는 `message`만, 재시도에는 `message`+`target`) 다른 요청으로
  판정될 수 있다
- 에이전트가 "지금까지 무엇을 보냈는지"를 기억해야 하므로 호출자 구현이 더 무거워진다

전체 교체이므로 비교 대상 세 값이 항상 확정된다 — 생략된 필드와 명시적 `null`이 같은
값으로 정규화되어, 재시도 판정에 모호성이 남지 않는다.

- 현재 `status`가 `COMPLETED` 또는 `FAILED`(터미널)인 경우
  - 요청 payload가 현재 저장 값과 **정규화 비교로 동일**하면 재시도로 간주하고 `200`과
    현재 row를 반환한다
  - 다르면 `409`
- 현재 `status`가 `STARTED` 또는 `PROGRESS`인 경우
  - `STARTED`·`PROGRESS`·`COMPLETED`·`FAILED` 어디로든 갱신을 허용한다
  - 진행 표시용 상태이므로 전이 그래프를 두지 않는다

### 터미널 전이는 원자적이어야 한다

위 가드를 "현재 상태를 읽고 → 판단하고 → UPDATE"로 구현하면 검사와 실행 사이에 창이
열린다. 두 요청이 동시에 현재를 `PROGRESS`로 읽고 각각 `COMPLETED`와 `FAILED`를 쓰면 둘 다
가드를 통과해, 나중에 커밋한 쪽이 **조용히** 이긴다. 터미널 확정이라는 가드의 목적 자체가
무력화되므로 허용하지 않는다.

**터미널 상태로 전이하는 UPDATE는 `WHERE status NOT IN ('COMPLETED', 'FAILED')` 조건을
함께 건다.** 매치된 row가 0건이면 그 사이 다른 요청이 터미널을 확정한 것이므로, 현재 row를
다시 읽어 위 터미널 가드를 그대로 적용한다 — 동일 payload면 `200`, 다르면 `409`다.

비터미널 → 비터미널 갱신에는 이 조건을 걸지 않는다. "비터미널은 자유 전이"라는 결정을
그대로 유지하며, 정당한 동시 진행 갱신이 튕기지 않는다. 즉 이 조건은 **터미널을 쓰는
순간에만** 작동하는 최소 직렬화다.

FSM급 전이 그래프를 새로 만들지 않으면서, 터미널 확정이라는 최소 불변식만 서버가
보장한다. PATCH 전용 `idempotency_key` 컬럼을 추가하지 않으므로 생성 경로의 fingerprint
저장 메커니즘을 갱신 경로까지 복제할 필요가 없다.

### 정규화 비교의 정의

비교 대상은 `(status, message, target)` 세 값이며, `target`의 JSON key 순서에 영향받지
않아야 한다. 생성 경로가 쓰는 `_request_fingerprint()`(canonical JSON + SHA-256)를 그대로
재사용해 요청 값과 현재 저장 값 양쪽의 digest를 구해 비교한다.

**구현 시 주의**: 비교 대상은 저장된 `request_fingerprint` **컬럼이 아니다.** 그 컬럼은
생성 시점 payload(`step_kind`·`step_type` 포함)의 digest이므로 key 집합이 다르다. PATCH
시점에 현재 row의 컬럼 값으로 digest를 새로 계산해 비교한다.

## API 계약

모든 요청 모델은 추가 필드를 금지한다. UUID·body 검증 실패는 `422`, 인증 실패는 `401`,
없는 실험·없는 step은 `404`다. 모든 요청은 기존과 동일한 `X-Orch-Token`을 요구한다.

### `POST /experiments/{id}/steps`

요청은 `idempotency_key`, `step_kind`, `step_type`, 선택적 `status`(기본 `STARTED`),
선택적 `message`, 선택적 `target`이다. 성공은 `201`과 Step 응답이다. 터미널 상태 실험에
대한 생성 허용 여부는 Log와 동일하게 제한하지 않는다.

### `GET /experiments/{id}/steps`

`limit=100`(1~100), 선택적 `after_id`, 선택적 `step_kind` 필터를 받는다. `created_at ASC,
id ASC` 순으로 정렬하며 동일 timestamp에서는 UUID `id`를 tie-breaker로 사용한다.
`after_id`가 가리키는 `(created_at, id)`보다 뒤의 `items`와 마지막 item의 ID인
`next_cursor`를 반환한다. `after_id`가 해당 실험에 없으면 `404`다.

### `PATCH /experiments/{id}/steps/{step_id}`

요청은 `status`, 선택적 `message`, 선택적 `target`이다. **전체 교체 시맨틱**이므로 생략된
선택적 필드는 `null`로 갱신된다. 위 "갱신 멱등성 계약"을 적용하며 성공은 `200`과 Step
응답이다.

## DB와 migration

실험 테이블과 동일하게 Alembic만 생성하며 startup `create_all()`을 사용하지 않는다. UUID
기본키는 `gen_random_uuid()` server default를 정본으로 사용한다. unique constraint가
만드는 `(experiment_id, idempotency_key)` 인덱스를 별도 중복 생성하지 않는다.

### 인덱스는 3컬럼 — 기존 패턴의 의도적 개선

`experiment_steps`의 polling 인덱스는 **`(experiment_id, created_at, id)` 3컬럼**으로
만든다. 이는 `ix_events_experiment_created`·`ix_logs_experiment_created`와 **동일한
패턴이 아니라 의도적 개선**이다.

기존 두 인덱스는 `(experiment_id, created_at)` 2컬럼인데, 실제 cursor 쿼리는
`created_at > c OR (created_at = c AND id > c.id)` 조건과 `ORDER BY created_at ASC, id ASC`를
사용한다(`repository.py`). 즉 인덱스가 keyset을 정확히 덮지 못한다. Step은 새로 만드는
테이블이므로 `id`를 인덱스에 포함해 keyset과 정확히 일치시킨다.

기존 두 인덱스를 함께 변경하지는 않는다 — 동작 변경과 구조 변경을 분리한다.

## 구현 시 함정

원본 코드 주석에 있는 전제이며, 본 스펙만 보고 구현할 때 놓치기 쉬우므로 옮겨 적는다.

- **`session.expunge(row)`를 `session.rollback()`보다 먼저 호출한다.** `IntegrityError`
  복구 경로에서 순서가 뒤바뀌면 rollback이 세션에 남은 객체를 expire시켜, 응답 직렬화가
  "이미 로드된 컬럼만 읽는다"는 전제가 깨진다
- **Step 응답 스키마에 relationship 필드를 넣지 않는다.** 위 전제는 응답 스키마가
  relationship을 참조하지 않는다는 조건에서만 성립한다

## Streamlit 소비 계약

워크벤치는 기존 1초 polling에 다음 호출을 추가한다.

```text
GET /experiments/{id}/steps?after_id=<last_step_id>
```

- 프론트는 `step_kind`로 렌더 경로를 결정하고 `step_type`은 라벨로만 표시한다
- 알 수 없는 `step_kind`는 발생할 수 없다(서버 CHECK로 강제). 알 수 없는 `step_type`은
  라벨 원문을 그대로 표시한다
- **`message`는 선택적이며 PATCH 전체 교체로 `null`이 될 수 있다.** 값이 없으면
  `step_kind`+`step_type` 라벨을 대신 표시한다 — 한 줄 표시가 비는 경우는 없다
- 터미널 상태 실험에서는 마지막 Step page를 한 번 더 가져온 뒤 polling을 중단한다

## 알려진 한계

### cursor 경합 (기존 리소스에서 상속)

워크벤치 v0 스펙이 기록한 한계가 Step에도 **그대로 적용된다.** `created_at`은
`func.now()`(트랜잭션 시작 시각)로 채워지는데 다른 세션에 row가 보이는 시점은 커밋
순간이다. 먼저 시작하고 나중에 커밋한 트랜잭션의 row는, 그 사이 polling이 cursor를 더
늦은 `created_at`으로 전진시켰다면 이후 어떤 polling에서도 반환되지 않는다.

Step 생성은 Log와 같이 `experiments` row를 잠그지 않으므로 이 경합에 실제로 노출된다.
tie-breaker인 `id`도 `gen_random_uuid()`라 append 순서를 보존하지 않는다.

**3컬럼 인덱스는 이 문제를 해결하지 않는다.** 인덱스 커버리지 개선일 뿐이며, 근본
해결은 BIGSERIAL 등 단조 증가 cursor 키가 필요하다. 기존 이슈와 함께 다룬다.

Step은 진행 표시가 목적이라 누락 시 "기록되지 않은 스텝"으로 보이므로 Log보다 체감
영향이 크다. v0에서 감수하는 근거는 기존과 동일하다 — 소수 인원의 로컬/dev 조회이고
스텝 append 동시성이 낮다.

### 진행 역행 (PATCH 가드가 막지 않는 범위)

`STARTED`·`PROGRESS` 사이 자유 전이를 허용하므로 두 가지가 남는다.

- 네트워크 재정렬로 이전 진행 상태의 PATCH가 늦게 도착하면 화면상 진행이 뒤로 갈 수 있다
- 두 요청이 동시에 서로 다른 **비터미널** 상태를 쓰면 나중에 커밋한 쪽이 이긴다 (lost update)

둘 다 다음 갱신이 도착하면 스스로 복구되는 일시적 현상이라 v0에서 감수한다. 터미널
전이에는 위 "터미널 전이는 원자적이어야 한다"의 조건부 UPDATE가 걸리므로 이 감수 범위에
**포함되지 않는다** — 확정된 결과가 조용히 뒤집히는 경우는 없다.

필요해지면 `updated_at` 기반 조건부 갱신으로 비터미널 구간도 낮은 비용에 완화할 수 있다.

## 완료 조건

- `step_kind`와 `status`의 허용값이 서버 CHECK constraint로 강제되고 위반은 `422`다
- 같은 key·같은 payload의 생성 재요청이 중복 row를 만들지 않는다
- 같은 key·다른 payload의 생성 요청은 `409`다
- 터미널 Step에 대한 동일 payload PATCH는 `200`, 다른 payload PATCH는 `409`다
- PATCH에서 생략된 선택적 필드가 이전 값 유지가 아니라 `null`로 갱신된다
- `target` 4096 byte 초과가 생성·갱신 **양쪽에서** `422`다
- 비터미널 Step에 서로 다른 터미널 상태를 동시에 쓰면 한쪽만 확정되고 다른 쪽은 `409`다
- 비터미널 Step은 `STARTED`·`PROGRESS` 사이를 오갈 수 있다
- `target`의 JSON key 순서가 달라도 재시도 판정이 동일하다
- `GET /steps`가 `created_at ASC, id ASC` 순서와 `next_cursor`를 반환한다
- Swagger가 성공·인증·not-found·conflict·validation 응답을 정확히 표시한다
- 기존 Event/Log/Metadata 회귀 테스트가 유지된다
