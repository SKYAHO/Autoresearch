# action log shard 생성 및 merge 설계

## 목적

단일 Airflow KPO Pod가 전체 virtual user를 처리하던 daily action-log 생성을
user shard 단위로 병렬화한다. OpenRouter 호출은 shard Pod들이 나눠 수행하고,
최종 merge 단계가 기존 전역 CTR 정규화와 event log 출력 계약을 유지한다.

## 출력 경로

- Shard work parquet:
  `data_lake/action_log_work/dt=YYYY-MM-DD/shard=NNN/part-0.parquet`
- Shard quarantine:
  `data_lake/action_log_quarantine_work/dt=YYYY-MM-DD/shard=NNN/quarantine.jsonl`
- Final action log:
  `data_lake/action_log/dt=YYYY-MM-DD/part-0.parquet`
- Final quarantine:
  `data_lake/action_log_quarantine/dt=YYYY-MM-DD/quarantine.jsonl`

## Shard Work 계약

Shard parquet은 최종 `EventLog`가 아니라 `ImpressionDraft` work row를 담는다.

필드:

- `user_id`
- `video_id`
- `click_propensity`
- `watch_fraction`
- `would_like`
- `duration_sec`

이 선택은 shard별로 클릭을 먼저 확정하지 않기 위한 것이다. Shard가 최종
event log를 쓰면 각 shard 안에서만 `round(target_ctr * N)`이 적용되어 기존
"전체 impression 기준 전역 CTR 정규화" 계약이 깨진다.

## Sharding 방식

Virtual user parquet의 행 순서를 기준으로 contiguous slice를 사용한다.

```text
start = total_users * shard_index // shard_count
end = total_users * (shard_index + 1) // shard_count
```

Merge가 shard index 순서로 draft를 읽으면 단일 실행과 같은 user 순서를 최대한
보존한다. `shard_count`는 1 이상, `shard_index`는 `0 <= index < shard_count`
이어야 한다.

## Merge 방식

Merge 단계는 모든 shard draft parquet을 읽고 기존 action-log pipeline의
`_clicked_indices`와 event expansion 로직을 한 번만 적용한다.

- 클릭 선정: 전체 draft 기준 `round(target_ctr * impressions)`
- event expansion: `impression`, `click`, `view`, `like` long event stream 생성
- `event_id`: merge 단계에서 `evt_00000000`부터 결정론적으로 한 번만 부여
- partition 검증: 모든 event timestamp의 KST 날짜가 `partition_date`와 같아야 함
- quarantine: shard quarantine JSONL을 최종 quarantine JSONL로 병합

## 운영 제약

Airflow delivery repo가 fan-out/fan-in DAG와 batch entrypoint 인자를 제공한다.
Autoresearch 앱 코드 변경은 새 batch image를 빌드하고 Airflow Helm values의
`AUTORESEARCH_BATCH_IMAGE` tag를 갱신해야 live에 반영된다.
