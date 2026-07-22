# BigQuery Feature Materialization

## 목적

BigQuery raw 테이블을 CTR 피처 스키마로 변환하고, Terraform이 관리하는
feature 테이블에 전체 갱신하는 공개 batch CLI를 제공한다.

현재 `user_static_feature` SQL은 Parquet의 문자열 목록이 BigQuery에서
`STRUCT<list ARRAY<STRUCT<element STRING>>>`로 적재되는 실제 스키마와
호환되지 않아 컴파일할 수 없다. 또한 변환 SQL은 문서에만 있고 실행 경로가
없어 feature 테이블이 비어 있다.

## 범위

- `user_static_feature`, `user_dynamic_feature`, `video_feature`를 대상
  테이블로 한다.
- `user_static_feature`에서 nested list wrapper를 `ARRAY<STRING>`으로
  평탄화한다.
- 기존 공개 batch 명령 패턴을 따르는 materialization CLI를 추가한다.
- 각 테이블을 raw 데이터 기준 snapshot으로 전체 갱신한다.
- 변환 결과가 비어 있으면 실패시키고, 실행 job ID와 행 수를 기록한다.
- SQL 계약과 batch 실행 동작을 단위 테스트 및 BigQuery dry-run으로 검증한다.

## 제외 범위

- `training_entity` materialization 및 Feast historical retrieval 전환
- `user_category_similarity` materialization
- 아직 존재하지 않는 `user_topic_embedding`, `category_embedding` 원천
  테이블 생성
- `src.pipeline.build_feature_tables`의 수정, 삭제 또는 실행 계약 변경. 이
  모듈은 `training_entity`도 함께 다루는 별도 구현으로 유지한다.
- Airflow DAG, 스케줄, KubernetesPodOperator 및 GCP Terraform 변경

## 공존 정책

- 운영자가 호출하는 공개 batch 경로는
  `python -m autoresearch.jobs.feature_materialize`로 한정한다.
- `src.pipeline.build_feature_tables`는 이번 변경에서 보존한다. 다만 이 모듈은
  공개 batch JSONL/exit-code 계약을 제공하지 않으며, raw virtual-user nested
  list wrapper를 평탄화하지 않는다. 운영 materialization에 사용하지 않는다.
- 두 모듈이 같은 target table을 갱신하므로, Airflow 등 오케스트레이터는 한
  schedule에서 둘을 함께 호출하지 않는다.

## 인터페이스

공개 batch CLI는 프로젝트, feature target dataset, raw source dataset을
명시적으로 받는다. 대상 테이블은 CLI 내부의 고정된 순서로 실행한다.

```bash
python -m autoresearch.jobs.feature_materialize \
  --project <project-id> \
  --dataset <feature-dataset-id> \
  --raw-dataset <raw-dataset-id>
```

`--dataset`은 `user_static_feature`, `user_dynamic_feature`, `video_feature`
target table을 가리킨다. `--raw-dataset`은 `data_lake_action_log`,
`data_lake_youtube_trending_kr`, `asset_virtual_user_vu_1000` source table을
가리킨다. 세 raw source table은 materialization 전에 raw dataset에 적재되어
있어야 한다. 기본값과 세부 인자 명명은 기존 `autoresearch.jobs` 공개 명령
계약에 맞춘다.

## 데이터 흐름

1. CLI가 BigQuery client를 생성하고 feature target dataset의 대상 테이블
   존재를 확인한다.
2. `user_static_feature` SQL은 `--raw-dataset`의 virtual-user nested list를
   `UNNEST(field.list)` 후 `element`를 추출해 repeated string 컬럼을 만든다.
3. `user_dynamic_feature`와 `video_feature` SQL은 `--raw-dataset`의 현재
   문서화된 raw schema 기반 집계 규칙을 사용한다.
4. 테이블마다 BigQuery transaction에서 기존 행을 삭제하고 새 변환 결과를
   삽입한 뒤 commit한다.
5. commit 후 같은 script의 final `SELECT COUNT(*)`로 target table의 최종 행 수를
   조회한다. 성공 `job_summary`의 `row_counts`는 table name별 이 JSON integer
   값을 기록하며, 결과가 정확히 한 행의 integer가 아니면 세부 값을 노출하지 않고
   runtime failure로 처리한다. 0행이면 transaction 안에서 실패로 처리한다.

## 갱신 및 실패 계약

- `CREATE OR REPLACE TABLE`을 사용하지 않는다. Terraform이 설정한
  partition, label, description을 보존해야 한다.
- 한 테이블의 `DELETE`와 `INSERT`는 같은 transaction에서 수행한다.
  삽입 또는 검증이 실패하면 해당 테이블의 기존 행은 유지된다.
- 대상 테이블이 없거나 변환 결과가 0행이면 명확한 오류로 종료한다.
- 앞선 테이블이 실패하면 뒤 대상 테이블은 실행하지 않는다.
- 전체 실행은 테이블 간 원자성을 보장하지 않는다. 각 테이블은 독립적인
  snapshot이며, Airflow 오케스트레이션은 외부 저장소가 소유한다.

## 검증

- unit test: static nested list 평탄화 SQL, 대상 실행 순서, 실패 시 중단,
  빈 결과 거부, 실행 결과 로그.
- BigQuery dry-run: 실제 raw 스키마에서 세 SELECT가 컴파일되는지 검증한다.
- 통합 실행: 별도 승인된 환경에서 각 대상 테이블이 0행이 아닌지와
  FeatureView schema 호환성을 확인한다.

### 실행 기록 체크리스트

- [ ] unit test: `uv run python -m pytest -v tests/test_feature_materialize_job.py`
  실행 결과와 통과 개수를 기록한다.
- [ ] BigQuery dry-run: 승인된 프로젝트와 dataset에서 세 대상 SELECT를
  dry-run하고 각 job ID와 컴파일 결과를 기록한다.
- [ ] 통합 실행: 실제 row를 적재하는 materialization은 별도 승인된 환경에서만
   수행하며, 대상 테이블별 0행 여부와 FeatureView schema 호환성 결과를 기록한다.
- [ ] transaction rollback write verification: 실제 DML과 rollback 검증은 별도
  승인된 BigQuery integration checklist에서만 수행한다. 이 저장소의 unit test와
  dry-run은 write 또는 rollback integration을 실행하지 않는다.

## 소유권

- 이 저장소는 SQL, 공개 batch CLI, 테스트와 실행 문서를 소유한다.
- `Autoresearch-airflow`는 CLI 호출 schedule과 운영 재시도를 소유한다.
- `Autoresearch-infra`는 feature 테이블 정의, partition 및 IAM을 소유한다.
