# 실험별 Feast Registry·offline 실행 격리

## 목적

실험 브랜치를 공용 `dev`에 병합하지 않고 조건별(baseline/candidate) SHA 이미지와
Feast Registry로 여러 오프라인 실험을 병렬 실행한다. 실험 경로에서는 Redis와
온라인 materialize를 사용하지 않는다.

## 실행 context 계약

`#449`가 만든 branch event는 다음 식별자를 제공한다.

- `experiment_id`: 이슈 안의 실험 변형 이름
- `condition`: `baseline` 또는 `candidate`
- `source_sha`: 그 조건의 이미지에 구워질 40자리 커밋 SHA
  (baseline은 `base_dev_sha`, candidate는 `candidate_sha`)
- `registry_key`:
  `experiments/<issue>/<experiment_id>/<condition>/<source_sha>/registry.db`

실행 환경은 구성된 GCS registry root와 key를 결합해 다음 URI를 만든다.

```text
gs://<registry-bucket>/experiments/<issue>/<experiment_id>/<condition>/<source_sha>/registry.db
```

결과와 로그는 Registry와 다른 경로에 `run_id`를 포함해 저장한다.

```text
gs://<artifact-bucket>/experiments/<issue>/<experiment_id>/<condition>/<source_sha>/<run_id>/
```

조건 구간이 없는 이전 형식
(`experiments/<issue>/<experiment_id>/<candidate_sha>/registry.db`)은 candidate
좌표로만 계속 인정한다. baseline은 이 형식을 가질 수 없다(#454).

## 동작 계약

1. 각 조건의 source SHA에서 이미지와 immutable digest를 만든다.
2. 실행 Job에 `GCS_REGISTRY_PATH`, `EXPERIMENT_ID`, `CANDIDATE_SHA`, `RUN_ID`를 주입한다.
3. Job은 자기 조건의 Registry에만 `feast apply`를 수행하고 같은 Registry로
   offline retrieval·학습을 실행한다.
4. Job은 metric, dataset snapshot, image digest, Registry URI, artifact/log URI를
   결과 payload로 반환한다.
5. 조건별 실행이 모두 끝나면 `compare-paired-experiment`가 seed별 결과를 짝지어
   `comparison_passed`/`comparison_rejected`/`comparison_failed`를 판정한다(#454).
6. `#450`은 통과 payload를 검증하고 main Draft PR을, 실패·기각이면 원래 이슈
   comment를 만든다.

## 격리 규칙

- 같은 Registry object를 서로 다른 실험이 쓰지 않는다.
- baseline과 candidate는 source SHA가 같더라도 Registry를 공유하지 않는다.
- BigQuery 원본/offline 데이터는 읽기 전용으로 공유할 수 있다.
- 실험별 결과를 append할 때는 `experiment_id`, `condition`, `source_sha`,
  `run_id`를 함께 기록한다.
- Redis 접속, online materialize, 공용 dev/prod 배포는 이 실행 경로에서 금지한다.
- 동일 `experiment_id`·`condition`·`source_sha`의 재실행은 Registry URI를
  재사용하고 결과만 `run_id`로 분리한다.

## 저장소 경계

- Autoresearch: context 생성·검증, 공개 batch CLI, 비교·결과 payload 계약
- Autoresearch-airflow: Job 오케스트레이션·실행·결과 callback
- Autoresearch-infra: namespace, service account, GCS·BigQuery IAM, quota·cleanup

## 관련 문서

- `docs/specs/2026-08-03-paired-offline-experiment-comparison.md` — 조건 격리
  좌표를 소비하는 paired 비교·판정 계약(#454)
