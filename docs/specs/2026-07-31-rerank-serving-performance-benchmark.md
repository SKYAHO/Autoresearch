# Rerank Serving 성능·비용·안정성 벤치마크

## 목적

`POST /rerank`의 **실제 처리 병목**을 관측하고, 하나의 근거 기반 개선으로
지연시간·처리량·리소스 효율·오류 안정성을 함께 비교한다. 결과는 재현 가능한
벤치마크 보고서와 이력서용 수치 문장으로 남긴다.

## 배경과 현재 경계

현재 요청 경로는 다음 순서로 동작한다.

1. Pydantic 요청 검증과 readiness 검사
2. `(user_id, video_id)` 단위 Feast 1차 batch 조회
3. 고유 category 단위 Feast 2차 batch 조회
4. 21개 모델 피처 조립, CTR 확률 예측, 선택적 calibration
5. 입력 `video_ids` 순서를 보존한 응답 생성

공개 `RerankRequest`/`RerankResponse` 스키마, 입력 순서 보존, 422·503·500
오류 의미는 변경하지 않는다. 이번 범위는 성능 측정과 병목 개선이며, Pod 종료
실험, replica 변경, 외부 LoadBalancer/Ingress 도입은 포함하지 않는다.

## 관측 계약

기존 메트릭은 전체 요청 수, 후보 수 histogram, 전체 요청 duration, readiness를
제공한다. 이 작업은 다음의 낮은 cardinality 관측치를 추가한다.

| 지표 | 고정 label | 용도 |
| --- | --- | --- |
| `rerank_phase_duration_seconds` | `phase` | `feature_read_first`, `feature_read_second`, `feature_assemble`, `model_predict`, `response_build` 구간 분해 |
| `rerank_outcomes_total` | `outcome` | `success`, `feature_error`, `prediction_error`, `unavailable` 결과 비율 |
| `rerank_in_flight` | 없음 | 동시 요청 압력과 포화 관찰 |

`user_id`, `video_id`, 예외 문자열, model ID, benchmark 실행 ID는 label로
사용하지 않는다. 기존 `rerank_duration_seconds`는 end-to-end 지연시간의
정본으로 유지한다.

`feature_read_first`와 `feature_read_second`는 각각 `ServingFeatureBuilder`가
Feast reader에 내리는 batch read 호출 전체 시간을 잰다. 이는 Redis Cluster 내부의
shard별 통신 횟수를 주장하는 값이 아니라, 서빙 애플리케이션이 관측하는 두 단계
조회 비용이다. `feature_assemble`은 조회 결과 검증·카테고리 중복 제거·21개 모델
피처 후보 조립, `model_predict`는 DataFrame 준비·예측·calibration·정렬,
`response_build`는 결과 ID 검증·진단 계측·응답 모델 생성을 포함한다.

`ServingFeatureBuilder`에는 phase duration을 돌려주는 내부 전용 경로를 추가한다.
기존 `build()`의 후보 목록 반환 계약은 유지하고, HTTP 경로만 timing 결과를
사용한다. 따라서 피처 값·cold-start 처리·정렬·응답 계약은 바뀌지 않는다.

## 벤치마크 설계

### 실행 환경과 소유 경계

주된 성능 수치는 dev GKE에서 얻는다. k6는 노트북·port-forward가 아니라 GKE
내부의 일회성 Job으로 실행해 ClusterIP Service
`autoresearch-serving.autoresearch.svc.cluster.local:8000`을 호출한다. 이렇게
해야 serving Pod의 실제 네트워크 경로, CPU/RSS, Redis-backed Feast 조회 비용을
같은 조건에서 함께 관측할 수 있다.

Job은 앱 namespace에 두지 않는다. `loadtest` 전용 namespace, 권한 없는 KSA,
deny-all ingress, DNS와 serving Service TCP/8000만 허용하는 egress를
`Autoresearch-infra`가 소유한다. Job에는 GCP Workload Identity, application
Secret, DB·Redis 직접 egress 권한을 주지 않는다. GitHub Actions는 수동 실행만
허용하고 이 namespace의 Job·Pod·Pod log·load-test ConfigMap만 다룬다.

| 저장소 | 책임 |
| --- | --- |
| `Autoresearch` | 단계별 metric, fixture provisioner, k6 스크립트·검증, 수동 benchmark workflow, 보고서 |
| `Autoresearch-infra` | `loadtest` namespace/KSA/RBAC/NetworkPolicy와 Grafana·Prometheus 조회 경로 |

infra 변경은 별도 이슈·브랜치·PR로 추적한다. `autoresearch-serving` Deployment의
replica·Service type·외부 노출은 이번 이슈에서 변경하지 않는다.

### 결정론적 Feast fixture

부하 요청은 실제 Feature Store 경로를 거치되 공유 dev 피처 데이터를 덮어쓰지 않는
고정 fixture를 사용한다.

- entity는 `loadtest-user-001`과 `loadtest-video-001`부터
  `loadtest-video-200`까지로 고정한다.
- provisioner는 `user_static_feature`, `user_dynamic_feature`,
  `video_feature`, `user_category_similarity`의 정확한 `loadtest_` 행만
  삭제·삽입하는 멱등 DML을 사용한다. `WRITE_TRUNCATE`, `CREATE OR REPLACE`, Redis
  직접 쓰기는 금지한다.
- 값과 카테고리 배치는 결정론적으로 고정한다. 사용자 정적·동적 특성, 영상 200개,
  사용된 고유 카테고리별 similarity 행이 모두 존재해야 한다.
- fixture의 네 FeatureView는 기존 Airflow
  `feast_online_store_materialize` DAG를 수동 trigger해 Redis online store로
  materialize한다. 이 DAG는 Feast registry watermark부터 현재 시각까지 증분
  처리하므로, fixture DML 뒤 그 task의 `job_summary.status=succeeded`를 확인한
  뒤에만 benchmark를 시작한다. 자정 cron까지 기다리거나 별도 materialize 경로를
  만들지 않는다.
- `UserDynamicView`의 TTL은 60시간이므로 기준선·개선 후 실행 전에 fixture
  timestamp를 새로 적재하고 같은 절차로 materialize한다.
- k6 Job 전에는 24개·200개 canary 요청이 모두 HTTP 200, 요청 ID 순서, 항목 수,
  단일 model ID, 유한 score를 만족하는지 확인한다.

과거 `scripts/generate_and_upload_dummy_data.py`는 네 source table을
`WRITE_TRUNCATE`하고 무작위 데이터를 만들므로 이 benchmark에 재사용하지 않는다.

### 고정 부하 프로필

각 실행은 다음을 기록한다: 이미지/커밋 SHA, Python·모델 표현, CPU·메모리
할당, 실행 시각, fixture, 워밍업 여부.

| 변수 | 값 |
| --- | --- |
| 후보 수 | 24(일반 경로), 200(API 상한) |
| 동시성 | 1 → 2 → 4 → 8 VU. 각 조건은 별도 Job이며, 직전 조건 오류율이 1% 미만일 때만 다음 단계 실행 |
| 워밍업 | 60초. 결과에서 제외 |
| 측정 | 조건당 5분. Prometheus 30초 scrape 기준 10개 이상 관측 구간 확보 |
| 비교 | 개선 전·후에 동일한 후보 수, VU, 워밍업·측정 시간, fixture 값, 모델 버전, Pod CPU·메모리 할당을 사용. serving image digest·Git SHA는 의도한 코드 차이를 식별하도록 기록 |

각 k6 요청은 200 응답, 입력 ID 순서와 항목 수, 단일 model ID, 유한 score를
검증한다. 실패 응답은 오류율에 포함한다. 측정 결과는 p50/p95/p99, RPS, 요청 수,
상태 코드별 오류율, CPU, RSS, CPU-seconds/request를 포함한다. 비용은 이 값에서
먼저 표현한다. 클라우드 요금 또는 Pod 리소스 요청·한도가 같은 조건으로 확보된
경우에만 비용 단위로 환산하며, 서로 다른 측정 단위를 직접 비교하지 않는다.

## 개선 선택 규칙

기준선 뒤 가장 큰 p95 기여 구간 하나만 선택한다.

- Feast 1·2차 조회가 우세하면 안정적 피처의 제한된 TTL cache 또는 중복 요청
  병합을 검토한다. TTL·무효화·cold-start 의미와 FeatureStore 정합성 테스트가
  필수다.
- 모델 예측이 우세하면 입력 batch 처리 또는 모델 실행 경로를 검토한다. 점수,
  calibration, 입력 순서, model ID 계약은 보존해야 한다.
- HTTP/직렬화 또는 큐잉이 우세하면 request lifecycle과 worker 구성을 검토한다.
  Uvicorn multi-worker는 Prometheus multiprocess 집계가 함께 설계되기 전에는
  처리량 개선으로 주장하지 않는다.

한 실험에서는 병목 개선을 하나만 적용한다. 여러 변경을 묶어 효과 원인을
알 수 없게 만들지 않는다.

## 검증과 보고

1. 새 계측과 각 오류 outcome에 대한 단위·API 테스트를 작성한다.
2. fixture DML의 prefix 제한·멱등성·canary 계약과 k6 요청 검증을 테스트한다.
3. 각 Job의 시작·종료 시각, Job 이름, fixture version, serving image digest·Git
   SHA, 후보 수, VU를 메타데이터로 기록한다.
4. k6 summary JSON과 Prometheus range-query 원시 응답은 GitHub Actions artifact로
   보관한다. artifact의 식별자·해시·시간 범위, 계산식, 비교표, 한계, 재실행
   명령은 `docs/reports/`의 HTML benchmark report에 기록한다.
5. 개선 전·후 각각에서 동일한 프로필을 실행한다.

이력서에는 측정된 값만 사용한다. 표준 문장은 다음과 같다.

> Feast 기반 CTR reranking API의 [관측된 병목]을 계측·개선해, 후보 [N]개와
> 동시성 [C] 조건에서 p95를 [X]ms에서 [Y]ms로, 처리량을 [A]에서 [B] RPS로
> 개선하고 오류율 [E]를 유지했다.

`X`, `Y`, `A`, `B`, `E`는 보고서의 같은 조건 비교가 완료되기 전에는 채우지
않는다.

## 완료 기준

- 동일 부하 프로필의 개선 전·후 보고서가 존재한다.
- 속도, 리소스 효율, 안정성 지표가 같은 표에서 비교 가능하다.
- 선택한 변경이 공개 API와 FeatureStore 의미를 유지한다.
- 이력서 수치마다 실행 환경과 원시 측정 근거를 추적할 수 있다.
