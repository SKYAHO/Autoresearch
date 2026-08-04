# paired offline 실험 배치·비교 결과 계약 (#454)

- **상태**: Proposed
- **날짜**: 2026-08-03
- **이슈**: #454 (하위: SKYAHO/Autoresearch-airflow#209, SKYAHO/Autoresearch-infra#485), #503(§5)
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

- 실험 조립(기본이 아닌 FeatureService 또는 실험 피처)은 출력 경로를 명시해야
  하며, 생략하면 조립 전에 거부한다. 기본 경로는 prod 학습 데이터셋이고, 학습은
  `MODEL_FEATURE_COLUMNS`만 선택하므로 실험 CSV가 그 자리를 덮어써도 컬럼 수와
  지표 어디에도 드러나지 않은 채 prod 학습이 실험 데이터로 진행된다.

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

필드와 검증 규칙의 정본은 `src/pipeline/paired_experiment.py`의
`PairedExperimentRequest`다(`extra="forbid"`이므로 아래 필드 이름과 정확히 같아야
한다).

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
  "registry_root": "gs://<registry-bucket>",
  "dataset_snapshot_uri": "gs://.../manifest.json",
  "dataset_fingerprint": "<sha256>",
  "split_hash": "<sha256>",
  "training_config_fingerprint": "<sha256>",
  "plan_receipt": { "plan": {}, "object": {} },
  "baseline": {
    "source_sha": "<base_dev_sha>",
    "image_digest": "sha256:...",
    "code_archive_sha": "<base_dev_sha>",
    "code_archive_uri": "gs://.../code/<sha>.tar.gz",
    "registry_uri": "gs://<registry-bucket>/experiments/449/primary/baseline/<sha>/registry.db",
    "feature_schema_fingerprint": "<sha256>"
  },
  "candidate": {
    "source_sha": "<candidate_sha>",
    "image_digest": "sha256:...",
    "code_archive_sha": "<candidate_sha>",
    "code_archive_uri": "gs://.../code/<sha>.tar.gz",
    "registry_uri": "gs://<registry-bucket>/experiments/449/primary/candidate/<sha>/registry.db",
    "feature_schema_fingerprint": "<sha256>",
    "model_uri": "models:/ctr-model/12"
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

- `candidate.model_uri`는 `models:/<name>/<version>` 형식의 **불변** 참조여야 한다.
  alias(`models:/name@champion`)는 나중에 다른 버전을 가리키므로 받지 않는다.
  baseline에는 필요 없다.
- `feature_schema_fingerprint`는 그 조건이 학습에 **실제로 사용한 feature column
  목록의 sha256**이며, 재검증된 comparison manifest의
  `baseline_feature_columns_sha256`/`challenger_feature_columns_sha256`와 대조된다.
- `dataset_fingerprint`/`split_hash`는 두 조건이 공유하는 dataset snapshot과 split의
  sha256이며, 같은 방식으로 manifest와 대조된다.

### 데이터셋은 하나, 학습 입력만 다르다

공정 비교(#423)는 두 run이 **같은 dataset snapshot과 같은 split**을 썼을 것을
요구한다. 따라서 실험 피처를 추가하는 가설에서도 데이터셋을 조건별로 따로 조립하지
않는다.

1. 실험 FeatureService와 `--extra-features`로 dataset을 **한 번** 조립한다. 그 CSV에는
   prod 계약 컬럼과 실험 컬럼이 함께 들어 있다(§2).
2. 두 조건이 같은 CSV·같은 split·같은 seed로 학습한다.
3. baseline은 `--extra-features` 없이 학습해 prod 계약 컬럼만 모델 입력으로 쓰고,
   candidate는 `--extra-features`로 실험 컬럼을 덧붙인다.

즉 조건 간 차이는 **모델 입력(feature columns)** 에 있고 데이터셋 바이트에는 없다.
조건별 Registry 격리는 조건마다 `feast apply`가 서로의 정의를 덮어쓰지 않게 하기
위한 것이다.

### 동작

1. 요청을 검증한다. SHA 형식, `experiment_id`/`run_id` 형식, 조건별
   `code_archive_sha == source_sha`, 조건별 `registry_uri`가 `registry_root`와 §1
   좌표에서 나온 URI와 **정확히 일치**, seed 집합이 정책 seed 집합과 일치,
   `candidate.model_uri` 존재.
2. seed마다 `verify_training_comparison`으로 baseline/candidate run의
   snapshot·split·seed 동일성을 재검증한다. 어느 한 쪽이라도 없으면 그 seed는
   **누락된 쌍**이다.
3. 재검증된 manifest와 요청의 선언값을 대조한다. 학습 feature columns의 차이가
   `extra_features` 선언과 정확히 같아야 하고(컬럼 제거는 어떤 선언으로도 설명되지
   않는다), 조건별 feature columns fingerprint와 dataset/split hash가 manifest와
   같아야 한다. 요청이 스스로 신고한 값만으로는 아무것도 증명되지 않는다.
4. `create_paired_seed_evidence` → `evaluate_experiment` → `decide_promotion`을
   호출한다. 통계 판정과 reason code는 `promotion-policy-v1`을 그대로 따른다.
5. 판정을 결과 outcome으로 사상한다.

   | decide_promotion | 결과 outcome |
   | --- | --- |
   | `eligible` | `comparison_passed` |
   | `reject` | `comparison_rejected` |
   | `hold` | `comparison_failed` |

   요청 검증 실패, 쌍 누락, lineage 불일치는 판정 엔진을 호출하지 않고 바로
   `comparison_failed`가 된다. **모르는 상태는 통과가 아니다.**
6. 결과 payload를 원자적으로 기록한다. 실패·기각도 같은 형식으로 남긴다.

### 결과 계약 (`paired-offline-experiment-result-v1`)

정본은 `PairedExperimentResult`다.

```text
contract_version, outcome, decision_reason, reason_codes
issue_number, issue_branch, experiment_id
base_dev_sha, candidate_sha
baseline, candidate                      # 조건별 ConditionLineage
feature_service, extra_features, registry_root
dataset_snapshot_uri, dataset_fingerprint, split_hash, training_config_fingerprint
plan_id, evidence_id, evaluation_id, decision_id, policy_version
metric_name, primary_baseline, primary_candidate, paired_delta_mean
confidence_interval_lower, confidence_interval_upper
seeds, runs[]: seed, run_id, comparison_id, artifact_uri, log_uri
model_uri, evaluated_at
```

- `primary_baseline`/`primary_candidate`는 seed 하나의 값이 아니라 검증된 metric의
  **집계 평균**이다. 승격 게이트의 `primary_baseline`/`primary_candidate` 입력을
  변환 없이 채운다.
- `model_uri`는 `comparison_passed`일 때만 채운다. 통과가 아니면 `candidate`
  lineage의 `model_uri`도 함께 비워, outcome을 먼저 보지 않는 소비자가 실패·기각
  결과에서 승격 후보를 읽어내지 못하게 한다.
- `seeds`와 `runs`는 seed 오름차순으로 고정한다.
- `comparison_passed`가 아닌 결과는 promote ref·PR을 만들지 않는다.
- guardrail 지표는 이 결과가 **싣지 않는다**. 판정 엔진(`promotion-policy-v1`)이
  guardrail을 다루지 않기 때문이다. Issue Form에 guardrail을 선언한 실험은 승격
  게이트가 `guardrail_metric_missing`으로 fail-closed하므로, guardrail을 쓰는 실험의
  자동 승격은 별도 작업이 필요하다(아래 "알려진 계약 간극").

## 4. 승격 게이트 수용 규칙

`.github/workflows/auto-research-promotion.yml`의 `registry_uri` 검증은 `gs://`로
시작하는 URI 중 다음 두 형태를 모두 통과시킨다.

```text
/experiments/<issue>/<experiment_id>/<candidate_sha>/registry.db            (legacy)
/experiments/<issue>/<experiment_id>/candidate/<candidate_sha>/registry.db  (조건 격리)
```

`baseline` 조건 경로는 승격 입력으로 받지 않는다. 승격 후보는 candidate 조건의
산출물만이다.

producer가 `outcome`을 함께 보내면 `comparison_passed`가 아닌 결과는 승격 브랜치와
Draft PR을 만들지 않는다. `outcome`을 보내지 않는 기존 producer는 그대로 받아들인다.

게이트의 suffix 검사는 애플리케이션의 `registry_uri_matches`보다 약하다(root
anchoring이 없다). 두 검증은 각각 다른 신뢰 경계에서 동작하며, 결과 payload를 만드는
애플리케이션 쪽이 더 엄격한 규칙을 적용한다.


## 5. 자체검증 실패모드에서 도출된 계약 (#503)

로컬 capability probe 트랙에서 **에이전트가 자기 실험 결과를 검증할 때 놓치는
실패 모드 3건**을 실측했다. 근거 원본은 병합 대상이 아닌 worktree에만 있으므로
결론과 그로부터 나온 계약 요구만 여기 남긴다. 세 건 모두 **사람이 질문하기
전까지 발견되지 않았다** — 자발적 재검토는 0회였다.

### 5-1. 피처 생성 시점 — 분할 이후가 기본값이다

피처 함수를 전체 데이터에 적용한 뒤 분할하면, `groupby().rank()`처럼 **행 간
집계**를 하는 피처는 train 행의 값이 test 행의 존재에 의존하게 된다.

| 피처군 | 분할 前 생성 | 분할 後 생성 | 누수분 |
| --- | --- | --- | --- |
| row-wise만(로그·비율·상호작용) | 0.9761 | 0.9761 | **0.0000** |
| 행 간 집계 포함(groupby rank) | 0.9154 | 0.8949 | **+0.0205** |

누수 상태에서는 해당 피처군이 **+0.0130 도움**으로 보였으나, 제거하면 baseline
대비 **−0.0075 악화**였다. 결론이 뒤집힌다.

**계약 요구:**
- `paired-offline-experiment-v1` 요청은 **피처 파생이 row-wise인지 행 간 집계를
  포함하는지 선언**해야 한다. 행 간 집계가 하나라도 있으면 분할 이후 생성을
  강제하고, 그러지 못하는 요청은 fail-closed한다.
- row-wise만 선언한 요청도 **분할 전/후 산출물이 동일한지 실측 확인**을 통과해야
  한다(위 표의 `0.0000`이 그 확인이다). 선언을 신뢰하지 않는다 — 판정 엔진이
  전달된 JSON을 신뢰하지 않는 것과 같은 이유다.
- `extra_features`가 이미 entity/passthrough 컬럼을 거르지 않는 기존 간극과
  함께 봐야 한다. 이름 검사만으로는 누수를 못 막는다.

### 5-2. 후보 선택 규칙은 결과 조회 **전에** 고정한다

후보 정렬에서 검증셋 지표가 **정확히 동점**(소수점 9자리 일치)인 상황이 발생했고,
그중 **holdout 지표가 높은 쪽**이 선택됐다. "holdout으로 고르지 않는다"는 원칙을
세워두고도 동점에서 위반한 것이다. 간명성(입력 피처가 적은 쪽)으로 깨면 선택이
달라지고, 그 선택은 "피처 변경 없이 모델만 바꾼 조합"이었다.

**계약 요구:**
- 선택 규칙에 **동점 처리(tie-break)를 명시**한다. 권장 순서:
  검증 지표 → 입력 피처 수(적을수록) → 후보 SHA 사전순.
- 규칙은 결과 payload에 **기록**되어, 어떤 규칙으로 무엇이 선택됐는지 사후에
  재구성 가능해야 한다. 명시하지 않은 tie-break는 holdout 누수와 구분되지 않는다.

### 5-3. 검증 이력은 결론이 아니라 **주장마다** 기록한다

주 결론에는 다중 시드 스윕과 paired t를 적용했으나, 그것을 떠받치는 **원인 분해는
단일 분할**로 계산했다. 10개 분할로 다시 재니 부호가 뒤집혔다(−0.0151 → +0.0043,
표준편차가 평균의 6배). "검증 완료" 플래그 하나로 의무가 끝났다고 간주한 결과다.

**계약 요구:**
- `paired-offline-experiment-result-v1`의 각 수치에 **그 수치가 통과한 검증
  수준**을 함께 싣는다(단일 분할 / N-seed paired / 누수 검사 여부).
- 결론을 지지하는 보조 수치라도 검증 수준이 낮으면 그 사실이 payload에 드러나야
  한다. 승격 게이트는 주지표만 보지만, 사람이 읽는 리포트는 보조 수치를 함께
  읽으므로 검증 수준이 섞이면 오독한다.

### 5-4. 통계 판정의 전제 — 다중 비교

여러 후보를 격자로 탐색해 최고를 고른 뒤 그 후보에 paired t를 적용하면,
**"N개 중 최고를 뽑았다"는 사실이 보정되지 않는다.** 실측 사례에서 35개 후보 중
최고를 골랐고, 관측 Δ가 판정 경계 대비 여유가 크지 않았다.

기존 판정 엔진은 `multiple_candidates_require_independent_holdout`으로 **다중
candidate 자동 승격을 이미 차단**하므로 이 경로는 fail-closed다. 다만 **단일
candidate로 제출됐지만 그 candidate가 오프라인 격자 탐색의 산물인 경우**는
구분되지 않는다.

**계약 요구:**
- 요청에 **후보가 사전 지정인지 탐색 산물인지**를 선언하는 필드를 둔다.
  탐색 산물이면 독립 holdout 재검증을 요구하거나 판정 경계를 보정한다.
- 선언이 없으면 사전 지정으로 간주하지 않는다(fail-closed).

### 5-5. 에이전트 설계 함의 — 자기 검증은 같은 컨텍스트에 맡기지 않는다

세 오류 중 둘은 **결론을 강화하는 방향**이었다(더 극적인 분해 값, 더 큰 헤드라인
수치). 결론에 유리한 숫자는 검산을 유발하지 않는다. 나머지 하나는 결론과
어긋나는 이상 신호였는데 **기록만 하고 추적하지 않았다.** 유리하면 의심하지 않고
불리하면 쫓지 않는 — 양방향 모두 결론을 흔들 작업의 회피다.

이는 프롬프트 지침으로 교정되지 않는다. `agent_orchestration`이 자율 실험을
운영할 때 **별도 컨텍스트의 리뷰어 단계**가 파이프라인에 포함되어야 하며,
위 5-1~5-4는 사람이 매번 기억할 규칙이 아니라 **요청 스키마와 결과 스키마가
강제할 계약**이다.

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
- guardrail 지표를 선언한 실험은 이 경로로 자동 승격될 수 없다. 판정 엔진이
  guardrail을 계산하지 않으므로 결과 payload에도 싣지 않으며, 승격 게이트는
  guardrail 값이 없으면 fail-closed한다. 안전한 방향의 실패지만 기능 공백이다.
- `extra_features` 이름은 prod 계약 충돌·중복·라벨만 거부한다. 조회 결과에 존재하는
  entity/passthrough 컬럼(`user_id` 등)을 선언하면 그대로 학습 입력이 된다.
- 격자 탐색으로 고른 후보에 대한 **다중 비교 보정이 없다**(§5-4). 판정 엔진은
  다중 candidate 동시 제출은 막지만, **단일 candidate로 제출된 탐색 산물**은
  구분하지 못한다. 후보의 출처(사전 지정 / 탐색 산물)를 선언하는 필드가 필요하다.
- 피처 파생이 행 간 집계를 포함하는지 선언·검증하는 경로가 없다(§5-1). 현재
  `extra_features` 검사는 이름 충돌만 보므로 분할 전 생성으로 인한 누수를
  막지 못한다.
- seed 30개 실험은 seed마다 `verify_training_comparison`과 판정 엔진 내부의
  `revalidate_training_comparison`이 각각 수행되어 MLflow artifact 다운로드·GCS
  receipt 검증이 60회 일어난다. 의도된 이중 검증이며(판정 엔진은 전달된 JSON을
  신뢰하지 않는다), Airflow Job의 timeout·retry 산정에 반영해야 한다.
