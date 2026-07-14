# GCS 데이터 레이크 raw 데이터 BigQuery 적재 스크립트

- 이슈: #113
- 상태: 승인됨 (2026-07-11)

## 배경

`docs/data-warehouse.md`의 피처 테이블 SQL은 BigQuery의 raw 테이블을
원천으로 참조하지만, GCS 데이터 레이크의 raw 데이터를 BigQuery로 적재하는
수단이 없다. 피처 테이블 생성과 Feast offline store 연동의 전제로 raw 적재
스크립트를 추가한다.

## 결정: BigQuery Load Job (네이티브 테이블)

검토한 대안:

| 대안 | 판단 |
| --- | --- |
| **A. Load Job (채택)** | GCS URI를 지정해 BigQuery가 서버사이드로 적재. 데이터가 로컬을 거치지 않고, load job은 무과금, parquet 스키마 보존 |
| B. External Table | Feast 관점에서는 동작하지만(Feast는 중간 피처 테이블만 읽음), 피처 SQL이 raw를 반복 참조해 스캔 비용·성능이 불리하고, 스키마 문제가 쿼리 시점에야 드러나며, 향후 증분 적재 전환 경로가 없음 |
| C. 로컬 경유 (pandas → load_table_from_dataframe) | 수십만 행을 로컬로 왕복해 느리고 dtype 변환 왜곡 위험 |

## 적재 대상

GCS 버킷(`YOUTUBE_LAKE_BUCKET`) 기준. 대상 테이블 이름은
`docs/data-warehouse.md`의 피처 SQL이 참조하는 이름을 그대로 사용한다.

| 소스 (GCS) | 대상 테이블 | 파티션 처리 |
| --- | --- | --- |
| `data_lake/action_log/dt=*/*.parquet` | `data_lake_action_log` | hive partitioning으로 `dt` 컬럼 복원 |
| `data_lake/youtube_trending_kr/dt=*/*.parquet` | `data_lake_youtube_trending_kr` | 동일 |
| `asset/virtual_user/vu_1000.parquet` | `asset_virtual_user_vu_1000` | 단일 파일, 파티션 없음 |

## 스크립트 인터페이스

`scripts/load_raw_to_bigquery.py`

```bash
python scripts/load_raw_to_bigquery.py                             # 3종 전부
python scripts/load_raw_to_bigquery.py --tables action_log,video   # 일부만
```

- 설정: `.env`의 `GCP_PROJECT_ID`, `BQ_DATASET`, `BQ_LOCATION`,
  `YOUTUBE_LAKE_BUCKET`을 기본값으로 읽고 `--project/--dataset/--bucket`으로
  override (기존 `scripts/generate_and_upload_dummy_data.py` 패턴)
- `YOUTUBE_LAKE_BUCKET` 미설정 시 명확한 에러 메시지와 함께 종료

## 동작 계약

- Load Job 설정: `source_format=PARQUET`, `write_disposition=WRITE_TRUNCATE`
  (전체 재적재, 멱등), 스키마 autodetect
- 테이블별로 job 제출 → 완료 대기 → 적재 행 수 로깅
- 한 테이블 실패가 나머지를 막지 않는다. 마지막에 성공/실패 요약을 출력하고
  실패가 하나라도 있으면 exit code 1

## 검증

- 단위 테스트 `tests/test_load_raw_to_bigquery.py`: URI·job config 구성
  로직을 BigQuery 클라이언트 mock으로 검증 (실제 GCP 호출 없음)
- 실전 검증: 실제 실행 후 3개 테이블 행 수를 GCS 원본과 대조, 재실행 멱등
  확인

## 후속 (별도 이슈)

- Airflow DAG으로 일 단위 증분 적재 자동화 (dt 파티션 단위 append/교체)
