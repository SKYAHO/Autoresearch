# action log shard 생성 및 merge 구현 계획

## 작업 범위

- `autoresearch/action_logs/pipeline.py`
  - draft parquet schema와 read/write helper 추가
  - LLM draft 생성과 event expansion을 별도 public 함수로 분리
- `autoresearch/action_logs/daily.py`
  - `run_daily_action_log_shard` 추가
  - `merge_daily_action_log_shards` 추가
  - shard path와 contiguous user slice helper 추가
- `tests/test_action_logs_daily.py`
  - shard work parquet 생성과 final merge 검증 추가

## 검증 계획

1. `uv run python -m pytest tests/test_action_logs_daily.py tests/test_action_logs_pipeline.py -v`
2. 필요 시 전체 회귀: `uv run python -m pytest -v`
3. `git diff --check`

## 완료 조건

- Shard work parquet이 `dt=YYYY-MM-DD/shard=NNN/part-0.parquet`에 생성된다.
- Merge가 shard work parquet 전체를 읽어 최종 `dt=YYYY-MM-DD/part-0.parquet`을 쓴다.
- 최종 event_id가 중복 없이 `evt_00000000`부터 재부여된다.
- Shard-local CTR 정규화가 아니라 전체 draft 기준 전역 CTR 정규화를 유지한다.
