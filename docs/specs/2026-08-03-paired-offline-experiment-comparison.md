# paired offline 실험 배치·비교 결과 계약 (#454)

- **상태**: Proposed
- **날짜**: 2026-08-03
- **이슈**: #454 (하위: SKYAHO/Autoresearch-airflow#209, SKYAHO/Autoresearch-infra#485)
- **선행 계약**: `2026-07-31-experiment-isolated-offline-run.md`(실행 context),
  `2026-07-31-experiment-promotion-draft-pr.md`(#450/#461 승격 게이트),
  `docs/guides/training-experiment-provenance.md` §5(write-once evidence, #423/#466)

## 목적

하나의 Auto Research 이슈 브랜치에서 고정한 `base_dev_sha`(baseline)와
`candidate_sha`를 **동일 조건**으로 paired 실행하고, 그 결과를 후속 dev 입성
게이트가 그대로 소비할 수 있는 하나의 결과 payload로 만든다.

이 문서는 **애플리케이션 경계**만 정의한다. Job 오케스트레이션(Airflow)과 IAM·
저장소 경계(infra)는 각 저장소가 소유한다.

## 무엇이 이미 있고 무엇이 없는가

이미 구현되어 **재사용**한다.

| 자산 | 위치 | 역할 |
| --- | --- | --- |
| write-once evidence | `src/pipeline/promotion_evidence.py` | plan/metric receipt의 create-only 게시와 generation 고정 재검증 |
| 공정 비교 재검증 | `src/pipeline/training_comparison.py` | 두 MLflow run의 snapshot·split·seed 동일성 검증과 `TrainingComparisonManifest` 생성 |
| paired 판정 엔진 | `src/pipeline/experiment_evaluation.py` | `eligible`/`hold`/`reject`와 reason code, 95% CI 판정 |
| 통계 계층 | `src/pipeline/seed_sweep.py` | paired delta, `t_critical_95` |
| 승격 게이트 | `.github/workflows/auto-research-promotion.yml` + `autoresearch/experiments/promotion_gate.py` | 이슈 기준 metric 판정과 Draft main PR |
| 실행 context | `autoresearch/experiments/context.py` | 실험별 Registry·artifact 경로 결정 |

없어서 이 spec이 정의한다.

1. baseline/candidate **조건**을 분리하는 Registry·artifact 좌표.
2. FeatureService와 실험 피처를 **데이터 조립까지** 전달하고 학습 CSV에 보존하는 계약.
3. seed별 paired run을 모아 판정하고 단일 결과 payload를 만드는 진입점.
4. 조건 격리 경로를 승격 게이트가 수용하는 규칙.

## 1. 조건 격리 좌표

`ExperimentContext`는 조건(`baseline`|`candidate`)과 그 조건의 source SHA로
결정된다.

```text
registry_key = experiments/<issue_number>/<experiment_id>/<condition>/<source_sha>/registry.db
registry_uri = <registry_root>/<registry_key>
artifact_uri = <artifact_root>/experiments/<issue_number>/<experiment_id>/<condition>/<source_sha>/<run_id>/
```

- `condition`은 `baseline`과 `candidate` 두 값만 허용한다.
- `source_sha`는 그 조건이 실행한 40자 소문자 커밋 SHA다. baseline은
  `base_dev_sha`, candidate는 `candidate_sha`를 쓴다.
- 두 조건은 source SHA가 같더라도 같은 Registry object를 쓰지 않는다.
- 같은 `(issue, experiment_id, condition, source_sha)`의 재실행은 같은 Registry
  URI를 재사용하고 결과만 `run_id`로 분리한다.

**하위 호환**: 조건 구간이 없는 legacy 경로
(`experiments/<issue>/<experiment_id>/<candidate_sha>/registry.db`)는 계속
유효한 candidate 좌표로 인정한다. 승격 게이트는 두 형태를 모두 수용한다.

## 2. 데이터 조립 피처 보존 계약

`--extra-features`(#405)는 **데이터셋에 이미 있는 컬럼**만 학습 입력으로
승격한다. 그런데 조립 경로가 `MODEL_FEATURE_COLUMNS + clicked`만 CSV에 쓰므로,
FeatureService에 파생 피처를 추가해도 학습 직전에 잘려나가 가설이 실행되지
않는다.

계약을 다음과 같이 바꾼다.

- 조립 경로는 `feature_service`(기본 `ctr_training_v1`)와 `extra_features`를 받는다.
- 학습 CSV의 컬럼은 `[*MODEL_FEATURE_COLUMNS, *extra_features, "clicked"]`이며
  순서가 고정된다. `extra_features`가 비면 기존 22컬럼 계약과 동일하다.
- `extra_features`에 prod 계약(`MODEL_FEATURE_COLUMNS`)에 이미 있는 이름이나
  `clicked`, 중복 이름을 주면 조립 전에 거부한다.
- 조회 결과에 선언한 추가 컬럼이 없으면 **CSV를 쓰기 전에** fail-closed한다.
  실패 메시지는 FeatureService 이름과 누락 컬럼을 포함한다.
- snapshot manifest의 `feature_service`에는 실제 사용한 이름을 기록한다.
- 추가 컬럼의 null은 cold-start 기본값으로 채우지 않는다. prod 계약 컬럼의
  cold-start 규칙(`apply_cold_start_defaults`)은 그대로 두고, 가설이 추가한
  컬럼의 결측 의미는 가설 소유자가 정의한다. 이 사실은 학습 CSV의 스키마
  fingerprint로 결과에 남는다.

`--extra-features`가 지정된 조립 결과는 prod 학습 데이터셋과 물리 스키마가
다르므로, prod 경로(`run-pipeline` 기본값)와 같은 파일 경로를 재사용하지 않는다.

## 3. paired 비교 진입점

Airflow는 조건마다 **다른 이미지**로 학습 Job을 실행하므로, 하나의 프로세스가
두 조건을 모두 학습할 수 없다. 애플리케이션은 실행이 끝난 뒤의 **집계·판정**을
공개 명령으로 제공한다.

```text
python -m src.cli compare-paired-experiment \
  --request request.json \
  --promotion-evidence-root gs://<bucket>/<prefix> \
  --output result.json
```

### 요청 계약 (`paired-offline-experiment-v1`)

```json
{
  "contract_version": "paired-offline-experiment-v1",
  "issue_number": 449,
  "issue_branch": "exp/449-example",
  "experiment_id": "primary",
  "base_dev_sha": "<40자리 소문자 SHA>",
  "candidate_sha": "<40자리 소문자 SHA>",
  "feature_service": "ctr_training_v1",
  "extra_features": ["views_per_day"],
  "dataset_snapshot_uri": "gs://.../manifest.json",
  "dataset_fingerprint": "<sha256>",
  "split_hash": "<sha256>",
  "training_config_fingerprint": "<sha256>",
  "plan_receipt": { "plan": {...}, "object": {...} },
  "conditions": {
    "baseline": {
      "source_sha": "<base_dev_sha>",
      "image_digest": "sha256:...",
      "code_archive_sha": "<base_dev_sha>",
      "code_archive_uri": "gs://.../code/<sha>.tar.gz",
      "registry_uri": "gs://.../experiments/449/primary/baseline/<sha>/registry.db",
      "feature_schema_fingerprint": "<sha256>"
    },
    "candidate": { "...": "같은 형식" }
  },
  "runs": [
    {
      "seed": 42,
      "run_id": "seed-42",
      "baseline_mlflow_run_id": "...",
      "candidate_mlflow_run_id": "...",
      "artifact_uri": "gs://.../449/primary/candidate/<sha>/seed-42/",
      "log_uri": "gs://.../449/primary/candidate/<sha>/seed-42/log.txt"
    }
  ]
}
```

### 동작

1. 요청을 검증한다. SHA 형식, `experiment_id`/`run_id` 형식, 조건별
   `code_archive_sha == source_sha`, 조건별 `registry_uri`가 §1 좌표와 일치,
   seed 중복 없음, `runs`의 seed 집합이 승격 정책 seed 집합과 일치.
2. seed마다 `verify_training_comparison`으로 baseline/candidate run의
   snapshot·split·seed 동일성을 재검증한다. 어느 한 쪽이라도 없으면 그 seed는
   **누락된 쌍**이다.
3. 검증된 comparison으로 `create_paired_seed_evidence` →
   `evaluate_experiment` → `decide_promotion`을 호출한다. 통계 판정과 reason
   code는 `promotion-policy-v1`을 그대로 따른다.
4. 판정을 결과 outcome으로 사상한다.

   | evaluate_experiment | 결과 outcome |
   | --- | --- |
   | `eligible` | `comparison_passed` |
   | `reject` | `comparison_rejected` |
   | `hold` | `comparison_failed` |

   요청 검증 실패, 쌍 누락, lineage 불일치는 판정 엔진을 호출하지 않고 바로
   `comparison_failed`가 된다. **모르는 상태는 통과가 아니다.**
5. 결과 payload를 원자적으로 기록한다. 실패·기각도 같은 형식으로 남긴다.

### 결과 계약 (`paired-offline-experiment-result-v1`)

```text
contract_version, outcome, decision_reason, reason_codes
issue_number, issue_branch, experiment_id
base_dev_sha, candidate_sha
conditions.{baseline,candidate}: source_sha, image_digest, code_archive_sha,
  code_archive_uri, registry_uri, feature_schema_fingerprint
dataset_snapshot_uri, dataset_fingerprint, split_hash,
  training_config_fingerprint, feature_service, extra_features
seeds, runs[]: seed, run_id, comparison_id, artifact_uri, log_uri
primary_metric, primary_baseline, primary_candidate, paired_delta_mean,
  confidence_interval_lower, confidence_interval_upper, policy_version
model_uri (통과 시 필수, 그 외 null)
```

- `primary_baseline`/`primary_candidate`는 seed 하나의 값이 아니라 검증된
  metric의 **집계 평균**이다. 승격 게이트의 `primary_baseline`/
  `primary_candidate` 입력을 변환 없이 채운다.
- `comparison_passed`가 아닌 결과는 promote ref·PR을 만들지 않는다.
- guardrail 지표는 현재 판정 엔진이 다루지 않는다. 이슈 기준의 guardrail
  판정은 승격 게이트(`promotion_gate.evaluate`)가 수행하며, 결과 payload는
  guardrail 값을 전달만 한다.

## 4. 승격 게이트 수용 규칙

`.github/workflows/auto-research-promotion.yml`의 `registry_uri` 검증은 다음
두 형태를 모두 통과시킨다.

```text
/experiments/<issue>/<experiment_id>/<candidate_sha>/registry.db            (legacy)
/experiments/<issue>/<experiment_id>/candidate/<candidate_sha>/registry.db  (조건 격리)
```

`baseline` 조건 경로는 승격 입력으로 받지 않는다. 승격 후보는 candidate 조건의
산출물만이다.

## 범위 제외

- 새 BigQuery source feature의 Terraform schema migration, feature build SQL
  확장, dev backfill, production migration/rollback.
- Redis online store 설정·materialize와 공용 dev/prod 배포.
- Airflow DAG, KubernetesPodOperator, retry/timeout/pool.
- GCP IAM, namespace, quota·cleanup 정책.
- champion alias 이동과 prod 배포(#470).

## 알려진 계약 간극

- Issue Form(`.github/ISSUE_TEMPLATE/auto_research.yml`)의 `랜덤 시드 목록`
  기본값은 3개지만 `promotion-policy-v1`은 seed 42..71의 30개를 요구한다.
  자동 승격 판정의 정본은 정책이며, Issue Form 기본값 정렬은 별도 이슈에서
  다룬다. 이 간극 때문에 seed 집합이 어긋난 요청은 `comparison_failed`로
  끝나고 승격되지 않는다 — 조용한 통과는 발생하지 않는다.
- 판정 엔진은 단일 candidate만 자동 승격 대상으로 인정한다. 여러 candidate를
  비교하려면 독립 holdout 재검증이 필요하다(`multiple_candidates_require_independent_holdout`).
