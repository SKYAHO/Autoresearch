# Daily Action Log DAG 설계

- 날짜: 2026-07-07
- 상태: 설계 확정
- 범위: YouTube daily partition이 GCS에 적재된 뒤, 같은 날짜의 virtual user action log를 생성해 GCS에 저장한다.

## 1. 목적

매일 YouTube API로 수집된 KR trending 영상 200개를 기반으로, GCS에 저장된 virtual user 약 7천 명의 daily action log를 생성한다. 산출물은 CTR 학습셋의 원천 이벤트 로그이며, 기존 long event schema를 유지한다.

## 2. 입력

- YouTube daily video partition:
  - `gs://<YOUTUBE_LAKE_BUCKET>/data_lake/youtube_trending_kr/dt=YYYY-MM-DD/part-0.parquet`
  - `youtube_collection.load.write_partition`이 만든 정규화 스키마를 사용한다.
- Virtual users:
  - 기본값 `asset/virtual_user/vu_1000.parquet`
  - 실제 현재 row 수는 6,983명이며, `user_id`, `primary_categories`, `interest_keywords`, `hobby_keywords`, `lifestyle_keywords`를 action log 생성에 사용한다.

## 3. 후보 노출 믹스

각 유저는 매일 들어온 200개 영상 전체가 아니라 일부 후보만 impression으로 본다.

- 기본 `candidates_per_user=24`
- 후보 구성:
  - 70% personalized relevance: 유저 키워드와 영상 title/tags/description 관련도 상위
  - 20% popular/trending: 해당 daily pool에서 view_count가 높은 영상
  - 10% random exploration: 남은 영상 중 랜덤
- 6,983명 기준 하루 impression 수는 `6,983 * 24 = 167,592`개다.
- click/view/like 이벤트는 기존 `target_ctr` 규칙에 따라 전역 click propensity 상위 N개에서만 생성한다.

## 4. 출력

Daily action log는 날짜 파티션으로 저장한다.

```text
gs://<YOUTUBE_LAKE_BUCKET>/data_lake/action_log/dt=YYYY-MM-DD/part-0.parquet
```

보조 산출물은 운영 디버깅용으로 같은 날짜 아래에 저장한다.

```text
gs://<YOUTUBE_LAKE_BUCKET>/data_lake/action_log_quarantine/dt=YYYY-MM-DD/quarantine.jsonl
```

warehouse jsonl은 GCS daily 운영 경로에서는 필수 산출물이 아니므로 DAG의 기본 출력에서 제외한다. 기존 로컬 생성 경로는 유지한다.

## 5. Airflow 동작

- `youtube_trending_kr_daily`가 YouTube partition을 적재한다.
- `youtube_action_log_daily` DAG는 같은 날짜 partition을 읽어 action log를 생성한다.
- 운영 자동화는 Airflow Dataset 또는 시간 기반 schedule 중 하나를 사용할 수 있다. 이번 구현은 기존 DAG와의 결합을 작게 유지하기 위해 `youtube_trending_kr_daily`보다 뒤에 실행되는 daily schedule과 입력 partition 존재 검증을 사용한다.
- partition이 없으면 명확한 예외로 실패한다.

## 6. 설정

환경변수 또는 Airflow Variable:

- `YOUTUBE_LAKE_BUCKET`: GCS 버킷명, `gs://` 제외
- `ACTION_LOG_VIRTUAL_USERS_PATH`: virtual user parquet 경로. 기본값은 `<bucket>/asset/virtual_user/vu_1000.parquet`
- `ACTION_LOG_OUTPUT_DIR`: action log 출력 root. 기본값은 `<bucket>/data_lake/action_log`
- `ACTION_LOG_QUARANTINE_DIR`: quarantine 출력 root. 기본값은 `<bucket>/data_lake/action_log_quarantine`
- `ACTION_LOG_GENERATOR`: `rule_based` 또는 `openrouter`. 기본값은 `rule_based`
- `OPENROUTER_API_KEY`: `ACTION_LOG_GENERATOR=openrouter`일 때 필요

## 7. 비범위

- 추천 서버 기반 Phase 2 action log
- BigQuery 적재
- 과거 모든 날짜 action log backfill
- 100k 이상 대규모 병렬 최적화
