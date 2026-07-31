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
오류 의미는 변경하지 않는다. Airflow, Helm, GKE 리소스 구성과 FeatureStore
스키마는 이 작업의 범위 밖이다.

## 관측 계약

기존 메트릭은 전체 요청 수, 후보 수 histogram, 전체 요청 duration, readiness를
제공한다. 이 작업은 다음의 낮은 cardinality 관측치를 추가한다.

| 지표 | 고정 label | 용도 |
| --- | --- | --- |
| `rerank_phase_duration_seconds` | `phase` | `feature_read_first`, `feature_read_second`, `feature_assemble`, `model_predict`, `response_build` 구간 분해 |
| `rerank_outcomes_total` | `outcome` | `success`, `feature_error`, `prediction_error` 결과 비율 |
| `rerank_in_flight` | 없음 | 동시 요청 압력과 포화 관찰 |

`user_id`, `video_id`, 예외 문자열, model ID는 label로 사용하지 않는다. 기존
`rerank_duration_seconds`는 end-to-end 지연시간의 정본으로 유지한다.

## 벤치마크 설계

### 두 단계 환경

1. **결정론적 격리 환경**: 고정 모델과 fake online reader를 주입한다. HTTP
   처리·피처 조립·모델 추론의 반복 가능한 비교를 담당하며 외부 Redis·GCP에
   영향을 주지 않는다.
2. **제어된 통합 환경**: 비운영 user/video fixture와 비운영 Feast 좌표가
   명시적으로 준비된 경우에만 수행한다. 실제 온라인 조회 지연과 오류율을
   확인한다. 운영 identity, 운영 트래픽, 무제한 부하는 금지한다.

### 고정 부하 프로필

각 실행은 다음을 기록한다: 이미지/커밋 SHA, Python·모델 표현, CPU·메모리
할당, 실행 시각, fixture, 워밍업 여부.

| 변수 | 값 |
| --- | --- |
| 후보 수 | 24(일반 경로), 200(API 상한) |
| 동시성 | 1 → 2 → 4 → 8, 직전 단계가 오류 예산 안일 때만 증가 |
| 워밍업 | 측정 전 별도 요청 구간 |
| 비교 | 개선 전·후에 동일한 후보 수, 동시성, 런타임, fixture 사용 |

측정 결과는 p50/p95/p99, RPS, 요청 수, 상태 코드별 오류율, CPU, RSS,
CPU-seconds/request를 포함한다. 비용은 이 값에서 먼저 표현한다. 클라우드
요금 또는 Pod 리소스 요청·한도가 같은 조건으로 확보된 경우에만 비용 단위로
환산하며, 서로 다른 측정 단위를 직접 비교하지 않는다.

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
2. 부하 도구의 요청 수·응답 검증·분위수 계산을 테스트한다.
3. 개선 전·후 각각에서 동일한 프로필을 실행한다.
4. `docs/reports/`에 환경, 원시 관측치, 계산식, 비교표, 한계, 재실행 명령을
   기록한다.

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
