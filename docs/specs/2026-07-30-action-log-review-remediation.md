# Action Log Streaming Review Remediation Design

**Status:** Approved in conversation on 2026-07-30
**Scope:** Autoresearch PR #415 review remediation for issue #393
**Owner path:** `autoresearch/action_logs/pipeline.py` single-mode execution

## Goal

PR #415의 bounded user streaming 방향과 기존 결정론을 유지하면서 Claude bot이
확인한 운영 회귀를 제거한다. 완료 조건은 다음과 같다.

- `generated_at`은 legacy batch와 같은 의미인 **전체 생성 완료 시각**을 사용한다.
- DAG 실행 중 active user, buffered draft/event, in-flight work를 구조화 로그로
  관찰할 수 있다.
- 느린 선두 사용자가 worker 전체를 놀리지 않도록 worker 수와 active-user
  memory window를 분리한다.
- 최종 Parquet row group과 footer 크기는 전체 사용자 수가 아니라 목표 row 수에
  따라 증가한다.
- quarantine 초과 실행은 quarantine만 보존하고 최종 Parquet/warehouse를 만들지
  않는다.
- exposure metadata 소비 계약과 파일 handle 타입을 코드에 명시한다.

Airflow KPO resource, public CLI, shard/checkpoint/merge 경로는 변경하지 않는다.

## Verified Problems

1. 새 single streaming 경로는 legacy `ActionLogTelemetryReporter`를 통과하지 않아
   15초 progress event와 work latency telemetry를 잃는다.
2. `max_active_users == max_workers`이고 입력 첫 사용자가 끝나야 다음 사용자를
   활성화하므로 head-of-line blocking이 생긴다.
3. 사용자 drain마다 `ParquetWriter.write_table()`을 호출해 event가 있는 사용자
   수만큼 row group을 만든다. 동일한 17-column schema로 10,000 row group을
   생성한 측정에서 footer serialized size가 17,056,516 bytes였다.
4. streaming writer 생성 시각을 모든 row의 `generated_at`으로 기록해 legacy의
   완료 시각 의미를 시작 시각으로 바꾼다.
5. mutable exposure metadata를 사용자 drain마다 비우지만 public 함수 계약에는
   이 mutation이 충분히 드러나지 않는다.
6. quarantine limit 검사를 writer 종료 뒤 수행해 실패 실행도 Parquet/warehouse를
   끝까지 작성한다.

## Selected Architecture

### 1. Transactional Streaming Sink

single 실행은 최종 Parquet에 직접 쓰지 않고 run-local 임시 spool에 기록한다.

- event spool: Arrow IPC stream
- warehouse spool: UTF-8 JSONL
- quarantine spool: UTF-8 JSONL

Arrow IPC event schema는 최종 schema에서 `generated_at`만 제외한다. 각 사용자가
drain되면 event record batch를 IPC에 append하고 Python `EventLog` 및 draft
참조를 즉시 해제한다. IPC stream은 footer에 전체 batch metadata를 모으지 않으므로
사용자 수에 비례하는 Parquet footer를 generation 단계에 만들지 않는다.

모든 사용자가 끝난 뒤 다음 순서로 commit한다.

1. Arrow IPC와 JSONL spool을 닫는다.
2. 최종 quarantine ratio를 검사한다.
3. 실패하면 quarantine spool만 `request.quarantine_output_path`로 원자적으로
   이동하고 event/warehouse spool은 제거한 뒤 기존 오류를 발생시킨다.
4. 성공하면 `datetime.now(UTC).replace(microsecond=0).isoformat()`을 한 번
   계산한다. 이 시점은 legacy가 draft 생성·quarantine 검사를 끝내고
   `EventLogBatch`를 만드는 시점과 같은 의미다.
5. IPC batch를 순서대로 읽어 `generated_at` column을 추가하고, 누적
   50,000 row를 기준으로 최종 Parquet row group을 쓴다.
6. warehouse와 quarantine spool을 각 최종 경로로 원자적으로 이동한다.

최종화 중에는 최대 50,000 row와 현재 IPC batch만 메모리에 유지한다. 마지막
row group은 50,000 row보다 작을 수 있고, 한 batch가 경계를 넘으면 batch를
slice하여 정확한 row-group 상한을 지킨다.

빈 event 실행도 기존 schema를 가진 빈 Parquet과 빈 JSONL을 만든다.

### 2. Concurrency and Active-User Window

두 상한을 분리한다.

- `max_workers = max(1, request.max_concurrency)`
- `max_active_users = 4 * max_workers`

현재 운영값 `max_concurrency=2`, 사용자당 후보 24개에서는 active user가 최대
8명이고, 완성 draft 상한은 대략 192개다. 전체 사용자 수와 무관한 상수배
상한이다.

executor에 제출한 Future는 최대 `max_workers`개만 유지한다. coordinator는 active
user들의 아직 제출하지 않은 chunk를 deterministic queue에 두고 Future가 끝날
때마다 다음 work를 제출한다. 입력 첫 사용자가 느려도 window 안의 뒤 사용자
work가 worker를 채울 수 있다.

완료 결과는 사용자와 chunk 위치에 보관하지만 최종 drain은 계속 원본 사용자
순서로만 수행한다. window 전체가 완료되고 선두 하나만 느린 최악의 경우에는
bounded memory를 위해 더 활성화하지 않는다. 이 제한은 의도된 backpressure다.

### 3. Operational Retention Telemetry

`_retention_observer` callback 자체를 DAG에 전달하지 않는다. callback은
in-process 테스트용으로 유지하고, 같은 `_StreamingRetentionSnapshot`을
구조화 JSON event에도 사용한다.

event 이름은 `action_log_streaming_progress`이고 기본 15초 간격으로 다음 field를
기록한다.

- `phase`: `generating` 또는 `finalizing`
- `active_users`
- `buffered_drafts`
- `buffered_events`
- `in_flight_work`
- `activated_users`
- `total_users`
- `submitted_work`
- `completed_work`
- `failed_work`
- `pending_work`: 모든 provider를 순회하기 전에는 `null`, 이후에는 정확한 값
- `throughput_per_min`
- `latency_p50_ms`
- `latency_p95_ms`

시작과 종료 snapshot은 interval과 무관하게 강제로 기록한다.
`ACTION_LOG_TELEMETRY_INTERVAL_SEC`은 기존 10~30초 검증을 그대로 사용한다.

`ACTION_LOG_TELEMETRY_DETAIL_MAX_WORK`도 유지한다. streaming에서는 총 work를
처음부터 알 수 없으므로 aggregate progress는 즉시 출력하고, 상세 work metric은
설정된 상한까지만 bounded buffer에 보관한다. 모든 provider를 순회해 정확한
total work가 상한 이하임을 확인하면 work-sequence 순서로 상세 event를 출력하고,
상한을 넘으면 상세 buffer를 버리고 aggregate만 유지한다.

로그에는 user ID, persona, prompt, raw LLM response, secret을 포함하지 않는다.

### 4. Exposure Metadata and Types

`generate_action_log_single()`의 streaming 계약은 mutable exposure metadata를
요구하며 타입을 `MutableMapping`으로 좁힌다. docstring에는 다음을 명시한다.

- drain된 사용자의 `(user_id, video_id)` entry를 제거한다.
- 정상 streaming 종료 후 전달된 mutable mapping은 비어 있다.
- runtime에 read-only `Mapping`이 전달되면 호환성을 위해 legacy batch로
  fallback하고 mapping을 소비하지 않는다.

`_warehouse_file`과 `_quarantine_file`은 `TextIO | None`으로 annotation한다.
`_retention_observer`는 회귀 테스트 hook이며 운영에서는 동일 snapshot을
structured telemetry로 출력한다고 docstring에 적는다.

## Determinism and Compatibility

다음 불변식은 변경하지 않는다.

- candidate provider는 coordinator thread에서 입력 사용자 순서대로 한 번 호출
- 사용자 전체 chunk 조립 뒤 한 번만 click 선정
- chunk Future 완료 순서와 무관한 사용자/event 순서
- legacy와 동일한 seed 기반 timestamp와 click 결과
- 전역 event ID sequence
- warehouse JSONL과 Parquet row 순서
- quarantine record 순서와 error classification
- duplicate/missing user ID 및 read-only metadata legacy fallback
- public CLI와 single-mode dispatch
- shard/checkpoint/merge 동작

Arrow IPC는 내부 run-local 구현 세부사항이며 외부 산출물이나 checkpoint 계약이
아니다.

## Error Handling and Cleanup

- spool open 중 일부만 성공하면 `ExitStack`이 이미 열린 handle과 임시 파일을
  모두 정리한다.
- generation 내부 버그가 발생하면 최종 산출물로 commit하지 않고 spool을
  정리한 뒤 원래 예외를 전파한다.
- quarantine limit 실패는 quarantine JSONL만 보존한다.
- Parquet finalization 또는 atomic move 실패는 성공으로 보고하지 않으며, 이미
  존재하던 last-known-good publish 대상은 daily publish 단계에서 그대로 보존한다.
- 최종화 중 telemetry는 `phase=finalizing`과 현재 `buffered_events`를 기록한다.

## TDD Acceptance Tests

각 production 변경 전에 아래 실패 테스트를 먼저 추가하고 RED를 확인한다.

1. 시작과 종료 시각을 다르게 고정했을 때 모든 Parquet row의 `generated_at`이
   legacy와 같은 종료 시각이다.
2. 10,000 사용자 규모를 작은 fixture로 축소한 contract test에서 row group 수가
   사용자 수가 아니라 `ceil(total_rows / 50_000)` 규칙을 따른다.
3. 느린 첫 사용자가 있어도 active window 안의 뒤 사용자 work가 실행되어
   worker가 유지된다.
4. active user, draft, Future, event buffer가 각각 정의된 상한을 넘지 않는다.
5. structured telemetry에 retention, work progress, latency field가 있고 interval
   환경 변수가 적용된다.
6. 작은 work 실행은 detailed telemetry를 보존하고 큰 실행은 aggregate만 남긴다.
7. quarantine limit 실패는 quarantine 파일만 남기고 Parquet/warehouse를 남기지
   않는다.
8. mutable exposure metadata 소비 및 read-only legacy fallback 계약이 유지된다.
9. streaming과 legacy의 draft/event/click/order/event ID/seed 결과가 동일하다.
10. daily staging 검증과 최종 publish가 전체 파일 materialization 없이 동작한다.

## Rejected Alternatives

### Direct Parquet with Placeholder `generated_at`

generation 중 임시 값을 넣은 Parquet을 만든 뒤 다시 쓰는 방법이다. Parquet
encoding을 두 번 수행하고 중간 파일에도 사용자 수만큼 row group/footer를 만들기
때문에 선택하지 않는다.

### Change `generated_at` to Run Start

추가 I/O가 없지만 기존 column 의미를 변경한다. 승인된 요구사항이 legacy 완료
시각 보존이므로 선택하지 않는다.

### Unbounded Sliding Window

느린 선두 사용자가 있어도 worker utilization은 유지하지만 완료된 뒤 사용자
draft가 전체 사용자 수에 비례해 쌓일 수 있다. issue #393의 핵심 목표와
충돌하므로 선택하지 않는다.

## Out of Scope

- Airflow `memory_request` 또는 `memory_limit` 변경
- single mode를 shard/checkpoint mode로 전환
- checkpoint format 변경
- GCP/Airflow 배포 설정 변경
- 12시간 운영 run의 Grafana plateau 판정 자체
