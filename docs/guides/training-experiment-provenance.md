# 학습 실험 provenance 애플리케이션 설계

> 관련 이슈: #423 · 인프라 후속: SKYAHO/Autoresearch-infra#464

## 목적

Autoresearch 애플리케이션은 baseline과 challenger의 평가 차이가 **피처 또는
모델 변경**에서 왔는지 재현 가능하게 판단할 수 있어야 한다. 이를 위해 학습에
실제로 사용한 CSV, 그 CSV를 만든 조건, train/validation/test 분할, 각 난수 seed를
MLflow run에 보관하고 두 run을 비교 전에 자동 검증한다.

이 문서는 application이 생산·검증하는 데이터 계약과 실행 흐름을 설명한다. GCS
버킷, IAM, 수명 주기 같은 기반 자원은 다루지 않는다.

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
artifact 원본이다.

sidecar 없이 직접 실행한 기존 `train-model` 흐름은 계속 학습할 수 있다. 다만
reproducibility artifact가 없으므로 verified comparison의 입력으로는 사용할 수 없다.
`run-pipeline`은 바로 생성한 snapshot manifest가 없거나 검증에 실패하면 training을
시작하지 않는다.

### 4. 공정 비교

애플리케이션은 다음 CLI를 제공한다.

```text
python -m src.cli verify-comparison \
  --baseline-run-id <run-id> \
  --challenger-run-id <run-id> \
  --output comparison.json
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

성공한 `TrainingComparisonManifest`에는 두 run ID, 검증한 snapshot·split manifest
hash, 두 final feature column 목록·hash, UTC 검증 시각, `validation_status="verified"`를
기록한다. 통계적 유의성 계산(#407)이나 champion 승격은 이 CLI의 책임이 아니다.

## 저장소 경계

| 소유자 | 이 설계에서의 책임 |
| --- | --- |
| Autoresearch application (#423) | manifest 모델·hash·검증, registry generation pinning, MLflow artifact, 비교 CLI, 테스트 |
| Autoresearch-infra (#464) | content-addressed GCS snapshot 저장소, IAM, retention/lifecycle, 비용·rollback 운영 계약 |
| Autoresearch-airflow | application image 호출, DAG schedule/retry/timeout, 배포 시 artifact 전달 |
| MLflow Model Registry | model version과 source run 연결. snapshot/split equality gate는 이번 범위 밖 |

즉 application은 **무엇을 보관하고 어떤 run끼리 비교 가능한지**를 보장한다. infra는
그 artifact를 장기적으로 **어디에 어떤 권한·보존 정책으로 저장할지**를 보장한다.
Airflow는 그 application CLI를 **언제 어떤 재시도 정책으로 실행할지**를 보장한다.

## 실패 원칙과 검증

- registry URI, generation 조회, generation 고정 다운로드, hash 계산 중 하나라도
  실패하면 CSV와 snapshot manifest를 게시하지 않는다.
- CSV와 snapshot manifest가 다르면 model fit, MLflow artifact upload, registry
  registration 전에 실패한다.
- split manifest 생성 또는 required MLflow artifact upload에 실패하면 해당 run은
  verified comparison을 주장할 수 없으므로 실패한다.
- 비교 입력 중 하나라도 누락·변조·불일치하면 두 run을 수정하지 않고 실패한다.
- 내부 검증 오류에는 안전한 대상 경로, run ID, expected/actual hash만 포함하며
  credential, signed URL, 전체 환경 변수는 포함하지 않는다. CLI는 backend 예외
  원문을 노출하지 않고 고정된 `ComparisonValidationError` 진단만 stderr에 출력한다.

구현 검증은 manifest 단위 테스트, assembly의 generation pinning 테스트, training의
artifact·seed·split 테스트, CLI의 성공 및 각 불일치 실패 테스트로 구성한다. 회귀
검증은 전체 pytest, Ruff, `git diff --check`를 실행한다.

## 이번 범위에서 하지 않는 일

- Model Registry champion 승격 gate 변경
- Airflow DAG 인자·schedule·retry·artifact 전달 변경
- FeatureView/FeatureService 정의 변경
- run 간 중복 CSV를 제거하는 canonical GCS snapshot registry 도입

마지막 항목은 application이 비교 artifact를 이미 만들 수 있는지와 별개의 인프라
운영 문제다. infra #464의 저장소 계약이 완료되면 application은 해당 저장소를
중복 제거 목적의 canonical snapshot source로 연동할 수 있다.
