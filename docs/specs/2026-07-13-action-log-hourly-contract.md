# 액션 로그 Hourly 실행 계약

## 목적

액션 로그 도메인 로직을 Airflow와 분리하고, 한 시간 데이터 구간을 명시적으로
입력받아 재현 가능하게 실행합니다.

## 입력 계약

- `interval_start`, `interval_end`는 timezone-aware ISO 8601 값입니다.
- 구간은 정확히 한 시간이며 시작 시각은 정시에 정렬됩니다.
- `partition_date`는 `interval_start`의 Asia/Seoul 날짜와 일치해야 합니다.
- Hourly 기본 Persona 상한은 300명입니다.
- Persona 선택은 `seed`, UTC 구간 시작, `user_id`의 SHA-256 순서로 결정합니다.

## 출력 계약

- 최종 로그: `dt=YYYY-MM-DD/hour=HH/part-0.parquet`
- 격리 로그: `dt=YYYY-MM-DD/hour=HH/quarantine.jsonl`
- Shard 산출물: `dt=YYYY-MM-DD/hour=HH/shard=NNN/*`
- Checkpoint와 progress도 동일한 `dt/hour/shard` 경계를 사용합니다.
- 이벤트 시각은 반개구간 `[interval_start, interval_end)` 안에 있어야 합니다.

## 책임 경계

- 이 저장소는 Persona 선택, LLM 호출, 스키마, 파티션 및 멱등성 계약을 소유합니다.
- Airflow 저장소는 스케줄, Sensor, KPO, Retry, Timeout, Pool을 소유합니다.
- 공개 CLI는 Airflow 패키지나 Jinja 템플릿에 의존하지 않습니다.

## 호환성

기존 Daily 함수는 유지합니다. 시간 구간을 생략하면 기존 `dt` 파티션과 하루 단위
동작을 사용하며, 구간을 제공하면 Hourly 계약을 적용합니다.
