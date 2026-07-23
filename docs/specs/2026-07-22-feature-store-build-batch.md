# Feature Store Build Batch (data_lake_raw → feast_offline_store)

- **상태**: Accepted
- **날짜**: 2026-07-22 (2026-07-22 증분 적재로 개정, #261)
- **관련 문서**: `docs/guides/data-warehouse.md`, `docs/guides/feature-store.md`,
  `docs/specs/2026-07-13-public-batch-execution-contract.md`

## 목적

BigQuery raw 계층(`data_lake_raw`)에 dt 파티션 적재가 끝난 뒤, feature 계층
(`feast_offline_store`)의 Feast source 테이블에 **대상 날짜 하루치를 증분
적재**하는 공개 batch 명령을 정의한다. 이 명령이 없어서 일일 파이프라인이 다음과 같이 끊겨 있었다.

```text
GCS 적재 → lake_to_bigquery_incremental → (없음) → feast_online_store_materialize
```

`feast_materialize`는 offline store의 feature 테이블을 Redis로 옮길 뿐 만들지
않으므로, 그 사이에 SQL feature build 단계가 필요하다.

## 공개 명령

```text
python -m autoresearch.jobs.feature_store_build [options]
```

| 인자 | 기본값 | 설명 |
| --- | --- | --- |
| `--project` | `CTR_TRAINING_BQ_PROJECT` 또는 `ar-infra-501607` | GCP 프로젝트 |
| `--dataset` | `CTR_TRAINING_BQ_DATASET` 또는 `feast_offline_store` | feature 계층 dataset |
| `--raw-dataset` | `CTR_TRAINING_BQ_RAW_DATASET` 또는 `data_lake_raw` | raw 계층 dataset |
| `--location` | `CTR_TRAINING_BQ_LOCATION` 또는 `asia-northeast3` | BigQuery job location |
| `--partition-date` | **필수** | 적재할 대상 날짜 (KST, `YYYY-MM-DD`) |
| `--tables` | 전체 | 적재할 테이블 부분집합 (comma-separated) |
| `--dry-run` | `false` | BigQuery dry-run으로 SQL만 검증 |

exit code는 batch-contract-v1을 따른다: `0` 성공, `2` 인자 오류, `1` 실행 실패.
stdout 마지막 줄은 `job_summary` JSON 한 줄이며 `job=feature_store_build`,
`mode`(`incremental` 또는 `dry_run`), `partition_date`, `tables`를 포함한다.

`--partition-date`는 `YYYY-MM-DD`만 받는다. 이 값이 SQL 리터럴로 전개되므로 형식
검증이 곧 주입 방어다. 형식 위반과 인자 누락은 모두 exit 2다.

## 대상 테이블

| 테이블 | 원본 | Feature View |
| --- | --- | --- |
| `user_dynamic_feature` | `{raw_dataset}.data_lake_action_log`, `{raw_dataset}.data_lake_youtube_trending_kr` | `UserDynamicView` |
| `video_feature` | `{raw_dataset}.data_lake_youtube_trending_kr` | `VideoFeatureView` |

SELECT 본문은 `docs/guides/data-warehouse.md`의 SQL을 그대로 옮긴 것이며, 그
문서가 계약의 단일 출처다. SQL 규칙이 바뀌면 문서와 이 module을 같은 PR에서
갱신한다.

`data_lake_action_log`의 각 `dt` 파티션은 독립적인 30일 히스토리 스냅샷이다.
따라서 `user_dynamic_feature`는 `--partition-date`와 같은 `dt` 하나만 읽으며,
여러 action log 파티션을 합치면 동일 이벤트가 중복 집계된다.

### 범위에서 제외: 정적 feature 2종

`user_static_feature`와 `user_category_similarity`는 persona나 카테고리 설명문이
바뀔 때만 갱신되는 정적 feature다. `event_timestamp`가 고정값이라 대상 날짜라는
개념 자체가 없으므로 증분 적재 대상이 아니며, `scripts/build_static_features.py`가
소유한다. 이 명령에 `--tables user_static_feature`를 주면 unknown table로 거부한다.

#### `user_category_similarity`

원본인 `user_topic_embedding`과 `category_embedding` artifact 테이블을 BigQuery에
적재하는 배치가 아직 없다. 현재 `topic_similarity`는 학습 파이프라인
(`src/features/assembly.py`)이 Vertex AI 임베딩으로 in-memory 계산하며, offline
store의 `user_category_similarity` 테이블은 더미 데이터
(`scripts/generate_and_upload_dummy_data.py`) 상태다.

따라서 이 배치는 해당 테이블을 건드리지 않고 기존 데이터를 그대로 둔다. 두
embedding artifact 테이블을 적재하는 배치가 생기면 이 spec을 갱신하고
`FEATURE_TABLES`에 추가한다.

## 적재 방식: 대상 날짜 DELETE + INSERT (WRITE_TRUNCATE 금지)

feature 테이블 4종의 **스키마는 Terraform이 소유한다**
(`Autoresearch-infra` `terraform/envs/dev/bigquery.tf`, #280). Feast FeatureView
정의(`feature_repo/feature_definitions.py`)가 선언한 컬럼명·타입·mode를 terraform
plan 단계에서 강제하기 위함이다.

그래서 다음 두 방식은 쓰지 않는다.

- `CREATE OR REPLACE TABLE`: 테이블을 재생성해 terraform 소유 정의를 파괴한다.
- load/query job의 `WRITE_TRUNCATE`: `createDisposition=CREATE_NEVER`는 테이블
  *생성*만 막고 스키마 교체는 막지 못한다. 2026-07-21 실측에서 REQUIRED가
  NULLABLE로 파괴되는 것이 확인됐다.

대신 테이블마다 BigQuery multi-statement script를 실행한다.

```sql
DELETE FROM `{project}.{dataset}.{table}`
WHERE <대상 날짜 조건>;
INSERT INTO `{project}.{dataset}.{table}` (컬럼 목록)
<data-warehouse.md의 SELECT 본문>;
```

DML은 스키마를 바꾸지 않으므로 terraform 정의가 보존되고, REQUIRED 컬럼에 NULL이
들어오면 BigQuery가 거부한다.

대상 날짜 조건은 테이블마다 `event_timestamp`의 의미가 달라 따로 선언한다
(`FeatureTableSpec.partition_predicate`). `DELETE`와 적재 후 검증이 같은 선언을
쓰므로 지우는 범위와 검사하는 범위가 어긋날 수 없다.

| 테이블 | `event_timestamp` | 대상 날짜 조건 |
| --- | --- | --- |
| `user_dynamic_feature` | 대상 날짜 KST 자정 (스냅샷 1개) | `event_timestamp = TIMESTAMP(DATE '{partition_date}', 'Asia/Seoul')` |
| `video_feature` | `collected_at` 그대로 (하루에 여러 시각) | `DATE(event_timestamp, 'Asia/Seoul') = DATE '{partition_date}'` |

### 룩백 윈도우

`user_dynamic_feature`는 대상 날짜 스냅샷 하나만 만들지만, `recent_*_7d`는 7일,
`historical_category_affinity`는 30일을 되돌아본다. 따라서 raw 스캔 윈도우는
`[대상 날짜 자정 - 30일, 대상 날짜 자정)`이다. 상한이 미포함이므로 2026-07-21
스냅샷은 07-14~07-20 이벤트를 집계한다.

이 30일 룩백은 선택한 단일 action log `dt` 파티션 안에서만 수행한다. 파티션
여러 개를 UNION하면 각 파티션이 가진 독립적인 히스토리가 중복 집계된다.

카테고리 조회용 trending 스캔에도 같은 30일 제한이 걸린다. 30일 넘게 트렌딩에
없던 영상은 `video_category`가 붙지 않아 `historical_category_affinity` 집계에서
빠진다. 전체 재구축 대비 유일한 결과 차이다.

### 유저 커버리지

대상 날짜 스냅샷을 받는 유저는 `기존 feature 테이블의 distinct user_id`
`UNION DISTINCT` `룩백 윈도우 action_log의 distinct user_id`다. 한 번이라도
등장한 유저는 활동이 끊겨도 계속 행을 받고, `LEFT JOIN` + `COUNTIF` 구조가 전부
0으로 채운다.

유저를 활동 기준으로 좁히면 안 된다. `UserDynamicView`에 ttl이 없어
(`feature_repo/feature_definitions.py`) 그날 행이 없는 유저는 Feast가 마지막으로
존재하는 스냅샷을 돌려주고, 결과적으로 오래된 `recent_click_count_7d` 같은 값이
"최근 7일" 값으로 학습·서빙에 흘러든다.

### 멱등성

`DELETE`가 대상 날짜 행을 먼저 걷어내므로 같은 날짜로 몇 번을 실행해도 결과가
같다. 다른 날짜 행은 건드리지 않는다. `DELETE` 성공 후 `INSERT`가 실패하면 그
날짜만 빈 상태로 남으며, 배치가 실패해 downstream materialize를 트리거하는
Dataset이 갱신되지 않으므로 online store에는 반영되지 않는다.

### 전체 재계산

전체 기간을 다시 만드는 모드는 제공하지 않는다. SQL 로직을 바꿔 과거를 재계산해야
하면 날짜를 바꿔 가며 반복 실행한다(`docs/guides/data-warehouse.md` 참조).

## 검증

테이블마다 적재 직후 assertion query를 실행하고 위반 시 `ERROR()`로 job을
실패시킨다.

검사 범위는 대상 날짜다. 이번 run이 만든 결과만 책임지므로 과거 데이터 문제로
일일 run이 죽지 않고, 테이블이 커져도 검증 스캔량이 늘지 않는다.

1. **비어있지 않을 것** — 대상 날짜 행이 하나도 없으면 원본 적재 누락을 의미한다.
2. **entity key와 `event_timestamp`에 NULL이 없을 것** — terraform REQUIRED와
   중복되는 안전망이며, 어느 컬럼이 몇 건 위반인지 로그로 남긴다.
3. **(entity key, `event_timestamp`) 중복이 없을 것** — Feast point-in-time
   join의 유일성 전제다. 위반하면 materialize 결과가 비결정적이 된다.

3번은 원본 데이터 품질 문제이므로 재실행으로 해결되지 않는다. 원본 적재
파이프라인을 먼저 확인한다.

## 실행 위치

`Autoresearch-airflow`의 `feast_offline_feature_build` DAG가 canonical
application image(`AUTORESEARCH_BATCH_IMAGE`)로 이 명령을 실행한다. 이 저장소는
명령과 SQL 계약만 소유하고, 트리거·재시도 정책은 그 저장소가 소유한다.

```text
lake_to_bigquery_incremental ⇢ feast_offline_feature_build ⇢ feast_online_store_materialize
```

`⇢`는 Airflow Dataset 갱신에 의한 트리거다(ExternalTaskSensor가 아니다).
logical date 결합이 없으므로 대상 날짜는 DAG가 정해 `--partition-date`로 넘긴다.
규칙은 `dag_run.conf.partition_date`가 있으면 그 값, 없으면 `data_interval_end`의
KST 날짜이며 `lake_to_bigquery_incremental`과 같다. 과거 dt 파티션을 수동
재적재하면 같은 `partition_date`를 conf로 넘겨 그 날짜만 다시 만든다.

## 권한

batch GSA(`autoresearch-batch`)에 필요한 최소 권한이다.

1. `roles/bigquery.jobUser` (프로젝트 단위)
2. `data_lake_raw`: `roles/bigquery.dataViewer`
3. `feast_offline_store`: `roles/bigquery.dataEditor`

## 후속 과제

- `user_topic_embedding` / `category_embedding` 적재 배치와
  `user_category_similarity` 재구축 추가.
- 대상 feature 테이블의 파티셔닝. 현재 `DELETE` 조건이 파티션 프루닝을 받는지는
  `Autoresearch-infra`의 `terraform/envs/dev/bigquery.tf` 정의에 달려 있다.
  파티션 컬럼이 없으면 DML마다 풀스캔이므로 별건으로 다룬다.
