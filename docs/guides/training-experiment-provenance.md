# 학습 실험 provenance 애플리케이션 설계

> 관련 이슈: #423 · #466 · 인프라 의존성: SKYAHO/Autoresearch-infra#485

## 목적

Autoresearch 애플리케이션은 baseline과 challenger의 평가 차이가 **피처 또는
모델 변경**에서 왔는지 재현 가능하게 판단할 수 있어야 한다. 이를 위해 학습에
실제로 사용한 CSV, 그 CSV를 만든 조건, train/validation/test 분할, 각 난수 seed를
MLflow run에 보관하고 두 run을 비교 전에 자동 검증한다.

이 문서는 application이 생산·검증하는 데이터 계약과 실행 흐름을 설명한다. GCS
버킷, IAM, 수명 주기 같은 기반 자원은 infra가 소유한다. 다만 #466 자동 판정은
infra #485가 제공하는 write-once GCS evidence 계약을 소비하므로, application이
요구하는 object 좌표와 검증 절차는 이 문서에서 명시한다.

## 데이터와 metadata의 구분

BigQuery의 `training_entity`와 Feast 조회 결과는 학습에 쓰이는 **실제 데이터**다.
`training_dataset.csv`에는 모델 입력 피처 값과 `clicked` label이 들어간다.

반면 snapshot manifest는 그 CSV 자체를 대체하지 않는 **metadata/provenance**다.
어느 기간의 어떤 FeatureService와 registry 상태로 CSV를 만들었는지, CSV 바이트와
스키마가 무엇인지 기록한다. 피처를 바꾸는 실험에서는 final feature column 목록도
run별로 기록한다. 따라서 비교기는 동일한 데이터를 썼는지 확인하면서도, 무엇을
바꾼 실험인지 함께 보여 줄 수 있다.

## 애플리케이션 실행 흐름

```text
BigQuery training_entity + Feast FeatureService
                    │
                    ▼
       build_training_dataset
                    │ CSV + snapshot manifest
                    ▼
                train.main
                    │ split manifest + 모델 + 지표
                    ▼
              MLflow run artifacts
                    │
                    ▼
          verify-comparison CLI
                    │ verified comparison manifest
                    ▼
       baseline/challenger 비교 결과
```

### 1. dataset assembly

`build_training_dataset`은 BigQuery의 entity spine을 기준으로 Feast offline point-in-time
조회 결과를 조립해 `training_dataset.csv`를 만든다. 성공한 CSV와 함께
`training_dataset.csv.snapshot.json` sidecar를 생성한다.

snapshot manifest에는 다음을 기록한다.

- CSV 전체 바이트의 SHA-256, 행 수, column 순서·dtype hash와 원본 목록
- events 시작·종료일과 `Asia/Seoul` timezone
- assembly source(`feast`), FeatureService(`ctr_training_v1`)
- 실제 사용한 Feast registry의 URI, object generation, 바이트 SHA-256
- container에서 주입되는 경우 code archive Git SHA

registry는 조회 시점의 generation을 확정한 뒤 그 generation의 local snapshot을
FeatureStore에 전달한다. 그러므로 manifest에 적힌 registry와 다른 정의가 조립 중에
읽히지 않는다. CSV와 manifest는 임시 파일에서 완성·검증한 후 CSV를 먼저,
manifest를 마지막에 원자적으로 게시한다.

### 2. training

`train.main`은 snapshot sidecar가 있으면 학습·모델 등록보다 먼저 CSV와 snapshot
manifest의 hash, schema, 행 수를 재검증한다. `require_snapshot=True` 호출은 sidecar가
없거나 검증에 실패할 때 model fit 전에 중단한다. `run-pipeline`이 이 옵션을 사용하고,
직접 `train-model`은 sidecar 없이도 기존 학습을 유지하지만 verified comparison
artifact를 만들지 않는다. 검증된 dataset에서 train/validation/test 3-way split을
확정한 뒤 `test_set.csv.split.json`을 만든다.

split manifest에는 snapshot hash, snapshot manifest 파일 hash, 세 분할의 행 수와
membership hash, effective seed, 최종 모델 입력 피처 목록·hash를 기록한다.
final CSV에 upstream ID가 없으므로 membership은 **검증된 source CSV의 0-based row
position** 목록을 canonical JSON으로 만든 SHA-256이다. snapshot hash와 함께
해석하므로 다른 CSV의 같은 row position을 같은 분할로 오인하지 않는다.

난수 조건은 아래 세 값으로 분리한다.

| Seed | 책임 |
| --- | --- |
| `split_seed` | train/validation/test membership |
| `model_seed` | LightGBM 초기화 |
| `sampler_seed` | train split negative downsampling |

기존 `random_state`는 호환을 위해 유지한다. 새 seed를 하나라도 지정한 호출은 세
값을 모두 지정해야 하며, `random_state`와 함께 사용하면 오류로 끝낸다. 새 seed가
없으면 기존 `random_state`, 그다음 config `data.random_state`를 세 effective seed에
동일하게 적용한다.

`train-model`과 `run-pipeline`은 `--split-seed`, `--model-seed`,
`--sampler-seed` 옵션을 제공한다. `run-pipeline`은 이 값을 검증된 snapshot과 함께
학습에 전달하고 `require_snapshot=True`를 강제한다. `sweep-seeds`는 기존
`--seeds` 목록을 각 호출의 `random_state`로 전달하는 호환 경로를 유지한다.

### 3. MLflow run artifact

검증된 학습 run은 아래 artifact를 같은 MLflow run에 업로드한다.

| Artifact | 경로 |
| --- | --- |
| 실제 training CSV | `reproducibility/snapshot/training_dataset.csv` |
| snapshot manifest | `reproducibility/snapshot/snapshot_manifest.json` |
| split manifest | `reproducibility/split/split_manifest.json` |
| 성공한 비교 manifest | `reproducibility/comparisons/<comparison-id>.json` |

Model Registry는 dataset snapshot을 별도 model version으로 등록하지 않는다.
모델 version은 기존과 같이 source MLflow run을 가리키고, 필요하면 snapshot과 split
manifest hash를 검색용 tag로 남긴다. tag는 변경 가능하므로 검증 근거는 항상 run
artifact 원본이다. 다만 MLflow artifact는 완료된 run에도 추가될 수 있으므로, #466의
"학습 전에 계획이 고정되었는가"와 "held-out 지표가 어느 실행에서 나왔는가"를
증명하는 시간·불변성 근거로는 사용하지 않는다.

sidecar 없이 직접 실행한 기존 `train-model` 흐름은 계속 학습할 수 있다. 다만
reproducibility artifact가 없으므로 verified comparison의 입력으로는 사용할 수 없다.
`run-pipeline`은 바로 생성한 snapshot manifest가 없거나 검증에 실패하면 training을
시작하지 않는다.

### 4. 공정 비교

애플리케이션은 다음 CLI를 제공한다.

```text
python -m autoresearch.cli verify-comparison \
  --baseline-run-id <run-id> \
  --challenger-run-id <run-id> \
  --output comparison.json \
  --promotion-evidence-root gs://<bucket>/<prefix>
```

명령은 두 MLflow run의 snapshot·split artifact를 내려받아 CSV와 manifest의 무결성을
먼저 확인한다. 이후 아래 항목이 모두 같은 경우에만 comparison manifest를 output
파일과 challenger run artifact에 쓴다.

- snapshot CSV SHA-256 및 snapshot manifest 파일 SHA-256
- train, validation, test 각각의 membership hash와 행 수
- `split_seed`, `model_seed`, `sampler_seed`

피처 목록과 hyperparameter는 같아야 하는 조건이 아니라 provenance 기록이다. 이들이
다르기 때문에 baseline/challenger 실험이 성립할 수 있다. snapshot, split 또는 seed가
다르거나 artifact가 누락·변조되면 명령은 non-zero로 종료하며 output 파일과 challenger
artifact를 만들지 않는다.

`--promotion-evidence-root`를 생략한 비교는 plan·metric receipt가 모두 없는 legacy
run에만 쓸 수 있다. receipt가 하나라도 있으면 root 없이 comparison을 만들지 않으며,
자동 승격 평가는 legacy comparison을 항상 `hold`로 처리한다.

성공한 `TrainingComparisonManifest`에는 두 run ID, 검증한 snapshot·split manifest
hash, 두 final feature column 목록·hash, UTC 검증 시각, `validation_status="verified"`를
기록한다. 통계적 유의성 계산(#407)이나 champion 승격은 이 CLI의 책임이 아니다.

### 5. write-once 승격 evidence (#466, 구현됨)

자동 승격의 입력은 사람이 작성한 JSON이나 LLM의 서술이 아니라, 학습 전에
write-once GCS에 고정한 계획과 학습 runtime이 만든 held-out metric object다. 이 둘의
신뢰 경계는 MLflow가 아닌 infra #485의 bucket IAM·prefix 분리·retention이다.

```text
가설 / control / candidate / v1 정책
                  │
                  ▼
 create-experiment-plan (plan publisher)
                  │ ifGenerationMatch=0
                  ▼
 GCS plans/<plan-sha256>.json ──► ExperimentPlanReceipt
                  │                         │ generation으로 고정 read
                  └──────────────┬──────────┘
                                 ▼
                    30개 paired training run
                                 │
      split manifest + active-run held-out ROC-AUC 계산
                                 │ ifGenerationMatch=0
                                 ▼
 GCS metrics/<training-run-id>/<metric-sha256>.json ─► HeldOutMetricReceipt
                                 │
                                 ▼
   verify-comparison: 두 receipt를 generation pin·rehash하여 comparison 생성
                                 │
                                 ▼
      30개 comparison의 결정론적 평가 ──► #470의 후속 소비자
```

#### 실제 CLI 실행 순서

아래 세 명령은 application이 제공하는 plan publish, 학습, evidence-aware comparison
순서다. `<...>`는 실행마다 정하는 식별자·경로이며, root는 infra #485가 제공하는
`gs://bucket/prefix`를 명시한다.

```bash
python -m autoresearch.cli create-experiment-plan \
  --hypothesis-id issue-466-h1 \
  --control-id <baseline-control-id> \
  --candidate-id <challenger-candidate-id> \
  --promotion-evidence-root gs://<bucket>/<prefix> \
  --output data/processed/experiment-plan-receipt.json

python -m autoresearch.cli train-model \
  --experiment-plan-receipt data/processed/experiment-plan-receipt.json \
  --promotion-evidence-root gs://<bucket>/<prefix> \
  --split-seed 42 --model-seed 42 --sampler-seed 42

python -m autoresearch.cli verify-comparison \
  --baseline-run-id <baseline-run-id> \
  --challenger-run-id <challenger-run-id> \
  --output data/processed/comparison-42.json \
  --promotion-evidence-root gs://<bucket>/<prefix>
```

`run-pipeline`도 `train-model`과 같은
`--experiment-plan-receipt`·`--promotion-evidence-root` 쌍을 받으며 둘 중 하나만
주면 학습 전에 종료한다. 생성된 receipt JSON과 comparison JSON은 전달용 envelope일
뿐 신뢰 근거가 아니다. 다음 단계는 항상 receipt의 `(uri, generation)`으로 GCS를
다시 읽어 server metadata와 byte SHA-256을 재검증한다.

#### 계획의 사전 선언과 receipt

application은 `ExperimentPlan`에 가설 식별자, control 식별자, 정확히 하나인
candidate 식별자, `promotion-policy-v1`, 그리고 감사용 `created_at`을 canonical JSON으로
기록한다. `plan_id`는 이 내용의 SHA-256 기반 식별자다. payload의 `created_at`은
호출자가 제공할 수 있으므로 사전 선언의 신뢰 근거가 아니다.

`create-experiment-plan` 명령은 infra가 전달한 명시적
`--promotion-evidence-root` 아래
`plans/<plan-sha256>.json`에 `ifGenerationMatch=0`으로만 object를 만든다. 성공하면
application은 아래 정보를 가진 `ExperimentPlanReceipt`를 다음 학습 단계에 전달한다.

| Receipt 필드 | 의미 |
| --- | --- |
| `plan_id`, `policy_version` | canonical plan 내용과 적용 정책 |
| `uri`, `generation`, `metageneration` | 정확히 어느 GCS object version을 읽을지 정하는 좌표 |
| `time_created` | GCS server가 기록한 object 생성 시각(UTC) |
| `sha256` | 내려받은 object byte의 SHA-256 |

학습 시작 전 application은 receipt의 `(uri, generation)`으로 object를 다시 읽고,
metadata의 generation·metageneration·`time_created` 및 byte SHA-256을 모두 대조한다.
이 검증이 실패하면 model fit 전에 중단한다. 기존 object를 덮어쓰거나 삭제하는
권한은 application runtime에 없으며, 같은 plan을 다시 publish하려는 요청도
precondition 실패로 끝난다.

#### 학습 runtime의 held-out metric evidence

각 baseline/challenger run은 위의 검증된 같은 plan receipt를 `TrainingSplitManifest`에
기록한다. 학습 runtime은 model fit 뒤 **active MLflow run 안에서** held-out `test` split의
정책 지표(`roc_auc`, `pr_auc`, `log_loss`)를 예측 1회로 계산하고, **지표마다** 다음
결합 정보를 가진 `HeldOutMetricEvidence` payload를 write-once GCS에 게시한다(#493).

- MLflow `run_id`, `plan_id`와 plan receipt의 `(uri, generation, sha256)`
- `metric_name`(정책 allowlist `SUPPORTED_HELD_OUT_METRIC_NAMES`),
  `dataset_split="test"`, 지표별 값 범위 — `roc_auc`/`pr_auc`는 `[0, 1]`,
  `log_loss`는 `[0, ∞)`이며 셋 다 유한해야 한다
- 해당 split manifest SHA-256 및 test membership hash
- 평가에 사용한 model artifact 경로와 byte SHA-256
- GCS가 발급한 metric object의 `uri`, `generation`, `metageneration`, `time_created`,
  byte SHA-256으로 구성된 `HeldOutMetricReceipt`

metric object는
`metrics/<training-run-id>/<metric-sha256>.json`에 `ifGenerationMatch=0`으로만 쓴다.
key가 evidence body 전체에 content-addressed 되어 있으므로 한 run이 지표 여러 건을
게시해도 key가 서로 다르고, 기존 `roc_auc` evidence의 key도 그대로다.
`verify-comparison`이 소비하는 MLflow artifact
(`reproducibility/metrics/held_out_metric_receipt.json`)에는 주 지표 receipt만 남는다 —
판정이 다중 지표를 받아들이는 것은 후속 단계다.
runtime은 자기 `training-run-id` prefix에만 쓸 수 있고 plan prefix나 다른 run prefix에는
쓸 수 없다. application은 metric receipt를 다시 generation pin으로 읽어 rehash하고,
metric의 plan·run·split·model binding이 local manifest와 일치하는지 확인한다. 또한 plan
object의 GCS `time_created`가 baseline과 challenger의 MLflow server run start보다 늦지
않고, metric object의 GCS `time_created`가 해당 run의 start/end 범위 안인지 확인한다.

이 계약은 runtime 자체가 신뢰된 학습 코드를 실행한다는 전제를 둔다. runtime 권한을
가진 임의 코드가 거짓 지표를 새 object로 쓰는 위협은 application의 JSON 검증만으로
해결할 수 없으므로, #485의 실행 identity 분리와 runtime image·job 제어 범위에서
다룬다.

#### comparison과 자동 판정의 fail-closed 규칙

`verify-comparison`은 기존 snapshot/split/seed equality 검증 뒤, 두 run의 plan receipt가
동일한 immutable object를 가리키는지와 각 run의 held-out metric receipt가 유효한지를
검증한다. 성공한 comparison에는 검증된 receipt와 object에서 읽은 값만 기록한다.
호출자가 `experiment_plan_id`나 ROC-AUC 숫자를 별도 인자로 주입해 자동 승격의 근거를
만들 수 없다.

평가기는 정확히 `42..71`의 30개 paired comparison을 다시 receipt 기준으로 읽는다.
모든 seed에서 baseline/challenger가 같은 snapshot·split·plan·정책에 묶이고 held-out
`roc_auc`가 유효할 때만 평균 개선과 양측 95% t 신뢰구간을 계산한다. 하한이 양수면
`eligible`, 상한이 0 이하이면 `reject`, 그 밖 또는 근거 결손은 `hold`다. LLM은 이
결정의 설명을 작성할 수 있지만 verdict·reason code·metric value를 결정하거나 바꾸지
못한다.

legacy snapshot/split/comparison manifest는 읽을 수 있게 유지한다. 그러나 plan receipt나
metric receipt가 없는 legacy run은 공정 비교 용도로만 쓸 수 있고, 자동 승격 평가는
반드시 `hold`한다. 부분 receipt, generation 불일치, SHA 불일치, plan 사후 생성,
metric 시간 범위 이탈, 중복·누락 seed도 같은 방식으로 fail-closed 한다. comparison
또는 paired evidence를 호출자가 직접 JSON으로 조립해도 evaluator는 MLflow run과 GCS
receipt에서 canonical comparison을 다시 만들지 못하면 통계를 계산하지 않는다.

### 6. paired offline 실험 실행과의 접합 (#454)

위 판정 엔진은 라이브러리이고, 그것을 실제 실험 실행 결과에 적용하는 진입점은
`compare-paired-experiment`(`autoresearch/model_evaluation/paired_experiment.py`)다. 이 명령은
조건별(baseline/candidate) 학습이 끝난 뒤 seed별 두 MLflow run을 짝지어
`verify-comparison`과 같은 재검증을 수행하고, 판정 결과를
`comparison_passed`/`comparison_rejected`/`comparison_failed`로 사상한다.

판정 엔진을 부르기 전에 실행 계보 자체를 fail-closed로 검사한다: 조건별
`code_archive_sha`가 그 조건의 source SHA와 같은지(코드 아카이브 fallback 차단),
Registry URI가 조건 격리 좌표에서 나왔는지, 두 조건이 Registry를 공유하지 않는지,
짝이 빠진 seed가 없는지, 학습 스키마 차이가 선언한 실험 피처로 설명되는지.
계약 정본은 `docs/specs/2026-08-03-paired-offline-experiment-comparison.md`다.

## 저장소 경계

| 소유자 | 이 설계에서의 책임 |
| --- | --- |
| Autoresearch application (#423, #466) | manifest 모델·hash·검증, registry generation pinning, plan/metric receipt 검증, MLflow artifact, 비교·평가 CLI, 테스트 |
| Autoresearch-infra (#485) | promotion evidence GCS prefix, create-only IAM 조건, publisher/runtime/verifier identity 분리, retention/lifecycle, production prefix deny |
| Autoresearch-airflow | application image 호출, DAG schedule/retry/timeout, 배포 시 artifact 전달 |
| MLflow Model Registry | model version과 source run 연결. alias 이동·동시성·rollback은 #470 이후 범위 |

즉 application은 **무엇을 보관하고 어떤 run끼리 비교·평가 가능한지**를 보장한다.
infra는 promotion evidence를 **누가 어느 prefix에 한 번만 쓰고 보존하는지**를
집행한다. Airflow는 그 application CLI를 **언제 어떤 재시도 정책으로 실행할지**를
보장한다.

## 실패 원칙과 검증

- registry URI, generation 조회, generation 고정 다운로드, hash 계산 중 하나라도
  실패하면 CSV와 snapshot manifest를 게시하지 않는다.
- CSV와 snapshot manifest가 다르면 model fit, MLflow artifact upload, registry
  registration 전에 실패한다.
- split manifest 생성 또는 required MLflow artifact upload에 실패하면 해당 run은
  verified comparison을 주장할 수 없으므로 실패한다.
- 비교 입력 중 하나라도 누락·변조·불일치하면 두 run을 수정하지 않고 실패한다.
- plan/metric receipt의 generation, metageneration, GCS server 생성 시각 또는 byte
  SHA-256이 실제 object와 다르면 자동 승격 근거로 사용하지 않는다.
- plan이 두 학습 run보다 늦거나 metric이 해당 run의 실행 시간 밖에서 생성되면
  자동 승격 평가는 `hold`한다. 부족한 근거를 새 숫자·새 plan으로 보정하지 않는다.
- plan 또는 metric publish가 이미 존재하는 object를 덮어쓰려 하면 성공으로
  간주하지 않고 precondition 오류로 종료한다.
- 내부 검증 오류에는 안전한 대상 경로, run ID, expected/actual hash만 포함하며
  credential, signed URL, 전체 환경 변수는 포함하지 않는다. CLI는 backend 예외
  원문을 노출하지 않고 고정된 `ComparisonValidationError` 진단만 stderr에 출력한다.

application 구현 검증은 manifest 단위 테스트, assembly의 generation pinning 테스트,
plan/metric의 create-only 응답·generation pinning·rehash 테스트, training의
artifact·seed·split·held-out metric 테스트, CLI의 성공 및 각 불일치 실패 테스트로
구성한다. late plan, missing generation, SHA 불일치는 application 음성 테스트로 둔다.
다른 run prefix write와 production prefix write 거부는 #485의 IAM 통합 acceptance
test로 검증한다. 회귀 검증은 전체 pytest, Ruff, `git diff --check`를 실행한다.

## 이번 범위에서 하지 않는 일

- Model Registry alias 이동, compare-and-swap, lock, 자동 rollback (#470 후속)
- Airflow DAG 인자·schedule·retry·artifact 전달 변경 — 새 CLI 인자는 이 PR에서
  Airflow에 전달하지 않는다.
- FeatureView/FeatureService 정의 변경
- run 간 중복 CSV를 제거하는 canonical GCS snapshot registry 도입

따라서 이 PR은 Model Registry alias를 이동하거나 dev/production 상태를 바꾸지 않는다.

마지막 항목은 application이 비교 artifact를 이미 만들 수 있는지와 별개의 인프라
운영 문제다. canonical snapshot 저장소의 infra 계약이 완료되면 application은 해당 저장소를
중복 제거 목적의 canonical snapshot source로 연동할 수 있다.
