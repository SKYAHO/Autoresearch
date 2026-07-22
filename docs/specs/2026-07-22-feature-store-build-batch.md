# Feature Store Build Batch (data_lake_raw → feast_offline_store)

- **상태**: Proposed
- **날짜**: 2026-07-22
- **관련 문서**: `docs/guides/data-warehouse.md`, `docs/guides/feature-store.md`,
  `docs/specs/2026-07-13-public-batch-execution-contract.md`

## 목적

BigQuery raw 계층(`data_lake_raw`)에 dt 파티션 적재가 끝난 뒤, feature 계층
(`feast_offline_store`)의 Feast source 테이블을 재구축하는 공개 batch 명령을
정의한다. 이 명령이 없어서 일일 파이프라인이 다음과 같이 끊겨 있었다.

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
| `--tables` | 전체 | 재구축할 테이블 부분집합 (comma-separated) |
| `--dry-run` | `false` | BigQuery dry-run으로 SQL만 검증 |

exit code는 batch-contract-v1을 따른다: `0` 성공, `2` 인자 오류, `1` 실행 실패.
stdout 마지막 줄은 `job_summary` JSON 한 줄이며 `job=feature_store_build`,
`mode`(`rebuild` 또는 `dry_run`), `tables`를 포함한다.

## 대상 테이블

| 테이블 | 원본 | Feature View |
| --- | --- | --- |
| `user_static_feature` | `{dataset}.asset_virtual_user_vu_1000` | `UserStaticView` |
| `user_dynamic_feature` | `{raw_dataset}.data_lake_action_log`, `{raw_dataset}.data_lake_youtube_trending_kr` | `UserDynamicView` |
| `video_feature` | `{raw_dataset}.data_lake_youtube_trending_kr` | `VideoFeatureView` |

SELECT 본문은 `docs/guides/data-warehouse.md`의 SQL을 그대로 옮긴 것이며, 그
문서가 계약의 단일 출처다. SQL 규칙이 바뀌면 문서와 이 module을 같은 PR에서
갱신한다.

### 범위에서 제외: `user_category_similarity`

원본인 `user_topic_embedding`과 `category_embedding` artifact 테이블을 BigQuery에
적재하는 배치가 아직 없다. 현재 `topic_similarity`는 학습 파이프라인
(`src/features/assembly.py`)이 Vertex AI 임베딩으로 in-memory 계산하며, offline
store의 `user_category_similarity` 테이블은 더미 데이터
(`scripts/generate_and_upload_dummy_data.py`) 상태다.

따라서 이 배치는 해당 테이블을 건드리지 않고 기존 데이터를 그대로 둔다. 두
embedding artifact 테이블을 적재하는 배치가 생기면 이 spec을 갱신하고
`FEATURE_TABLES`에 추가한다.

## 적재 방식: TRUNCATE + INSERT (WRITE_TRUNCATE 금지)

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
TRUNCATE TABLE `{project}.{dataset}.{table}`;
INSERT INTO `{project}.{dataset}.{table}` (컬럼 목록)
<data-warehouse.md의 SELECT 본문>;
```

DML은 스키마를 바꾸지 않으므로 terraform 정의가 보존되고, REQUIRED 컬럼에 NULL이
들어오면 BigQuery가 거부한다.

### 멱등성

세 테이블 모두 원본 전체를 다시 읽는 전체 재구축이다. 같은 날 재실행하거나
과거 logical date로 재실행해도 결과가 같다. 대신 `TRUNCATE` 성공 후 `INSERT`가
실패하면 대상 테이블이 빈 상태로 남는다. 이 경우 배치가 실패해 downstream
materialize를 트리거하는 Dataset이 갱신되지 않으므로 online store에는 반영되지
않는다.

## 검증

테이블마다 적재 직후 assertion query를 실행하고 위반 시 `ERROR()`로 job을
실패시킨다.

1. **비어있지 않을 것** — 전체 재구축이므로 빈 결과는 원본 소실을 의미한다.
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
logical date 결합이 없으므로 과거 dt 파티션을 수동 재적재해도 그 검증이 성공하는
즉시 feature build와 materialize가 다시 돈다. 이 배치가 원본 전체를 다시 읽는
전체 재구축이라는 점과 맞물려, 어느 시점의 재적재든 한 번의 재실행으로 반영된다.

## 권한

batch GSA(`autoresearch-batch`)에 필요한 최소 권한이다.

1. `roles/bigquery.jobUser` (프로젝트 단위)
2. `data_lake_raw`: `roles/bigquery.dataViewer`
3. `feast_offline_store`: `roles/bigquery.dataEditor`

## 후속 과제

- `user_topic_embedding` / `category_embedding` 적재 배치와
  `user_category_similarity` 재구축 추가.
- `asset_virtual_user_vu_1000` 삭제 예정(`docs/guides/data-warehouse.md` 경고)에
  따른 `user_static_feature` 원본 재지정.
- `user_dynamic_feature`는 action log 전체 기간의 일자별 snapshot을 매번 다시
  만든다. 기간이 길어지면 파티션 단위 증분 적재로 전환한다.
