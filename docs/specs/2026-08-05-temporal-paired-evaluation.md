# 시간축 paired 평가 — baseline/challenger 두 조건 rolling-origin (#514)

- **상태**: Proposed
- **날짜**: 2026-08-05
- **이슈**: #514
- **선행 계약**:
  - `docs/specs/2026-08-04-temporal-signal-promotion-integration.md` (#485) — §2가 이
    범위를 이 이슈로 이관했다. §5의 `temporal_delta`·`direction_vs_offline_metric`을
    **채우는 쪽**이 이 spec이다.
  - `docs/specs/2026-08-03-model-degradation-rolling-origin-evaluation.md` (#471/#510) —
    단일 조건 측정 하네스. 이 spec이 확장하는 대상이다.
  - `docs/guides/training-experiment-provenance.md` §5 (write-once evidence, #423/#466/#478)

## 목적

`run_rolling_origin`은 조건 하나의 곡선만 낸다. 이 spec은 **baseline/challenger 두 조건을
같은 시간축 위에서 비교**할 수 있게 하고, "같은 조건에서 비교했다"는 사실이 검증 가능한
형태로 남는 결과 계약(`temporal-paired-evaluation-v1`)을 정의한다.

## 비목적

- **`#478`의 30 시드 paired 경로 변경** — §1에서 정한 대로 이 spec은 그 경로를 건드리지
  않는다. 두 축은 병행한다.
- **프로덕션 배선** — `ExperimentEvaluation.temporal_signal`을 실제로 채우고 발행
  payload에 싣는 것은 `#472` 소유다(`#485` spec §5.3). 이 spec은 그 배선이 필요로 하는
  **입력**(`temporal_delta`)을 만드는 데까지다.
- **승격 게이트 배선** — `#461`/`#472` 경계는 `#485` spec §4.2 그대로다.
- **production Feature Service·production alias 변경** — `#485` 제약 승계.
- **다중 origin(여러 cutoff)** — 선행 spec §10 범위. 이 spec도 단일 cutoff 기준이다.
- **조건별로 데이터셋이 달라지는 실험** — §4.2·§8.3의 결과로 challenger의 정의가
  **"같은 데이터셋 위에서 학습 피처 선택만 다른 경우"**로 좁혀진다. 따라서 조건마다
  다른 `window_days`·`cutoff_date`·`feature_service`·backfill 구간을 쓰는 실험은 이
  spec이 지원하지 않는다.

  **왜 좁히는가**: `_validate_fairness`가 두 조건의 `dataset_sha256` 동일을 요구하고
  (§8.3), 그것이 `paired_experiment`가 `dataset_fingerprint`를 최상위 단일 필드로 둔
  것과 같은 계약이다(§2.3). 조건별 데이터셋을 허용하면 "같은 조건에서 비교했다"는
  증거가 다시 사라진다 — 이 이슈가 존재하는 이유 자체가 무너진다.

  **이 제약이 실제 유스케이스를 막지 않는다**: `#396`의 `views_per_day` 가설처럼
  "피처 하나를 더하면 개선되는가"가 이 저장소의 전형적인 실험이고, 그 형태는 그대로
  지원된다. 학습 구간 자체를 바꿔 비교하는 실험이 필요해지면 별도 계약이 필요하며,
  그때는 "무엇을 동일 조건으로 볼 것인가"부터 다시 정의해야 한다.

## 1. 시드 정책 — 두 축을 분리한다 (이 spec의 핵심 결정)

`#514` 완료 조건에 "`#478`의 30개 paired seed·write-once receipt 검증과 하위호환된다"가
있는데, 선행 spec §1은 rolling-origin 곡선을 **단일 시드**로 정해 뒀다(곡선 반복 비용).
두 요구를 문자 그대로 합치면 30 시드 × 2 조건 = 60회 실행이고, 1회가 **학습 1번 +
평가일 H번 조립**이다. 실측(§8.1) 기준 1회에 수십 분이 걸렸으므로 성립하지 않는다.

**결론: 시드 수를 맞추지 않는다. 두 축은 서로 다른 질문에 답한다.**

| 축 | 질문 | 반복 대상 | 소유 |
| --- | --- | --- | --- |
| 다중 시드 A/B (`#478`) | **같은 시점** 데이터에서 신뢰도 있게 이겼는가 | 시드 30개 | `#466`/`#478` |
| rolling-origin (이 spec) | 그 우위가 **시간이 지나도** 유지되는가 | 평가일 H개 | `#514` |

근거는 17차 회의 멘토 피드백이다 — "랜덤 샘플링에서 뽑는 거, 통계적 유의성 보는 거"와
"우리 모델 특성상 최신성이 반영돼야 됩니다. 트렌드가 반영이 돼야 돼요"를 **서로 다른 두
축으로 각각 갖추라**는 요구였다. 하나의 계산에 두 요구를 욱여넣으라는 것이 아니다.
성격이 다른 질문이므로 시드 수를 맞출 이유가 원래 없다.

### 1.1 그렇다면 "paired"는 무엇을 짝짓는가

시간축에서 짝지어야 하는 것은 **시드가 아니라 평가일**이다.

```text
paired(시간축) = 두 조건이
  같은 cutoff_date, 같은 window_days/horizon_days,
  같은 평가일 집합, 같은 feature snapshot, 같은 split 규칙,
  그리고 **동일한 단일 시드**를 썼다
```

시드를 동일하게 **고정**하는 것은 통계적 유의성을 얻기 위해서가 아니라, 두 곡선의 차이에서
시드 변동을 제거하기 위해서다. 시드가 다르면 §6의 `temporal_delta`가 조건 차이인지 시드
노이즈인지 갈리지 않는다 — 실측에서 시드 간 `val_roc_auc` 표준편차가 0.00467로
`min_auc_drop`(0.00933)의 절반에 달했다(§8.1).

### 1.2 `#478` 하위호환의 해석

"하위호환"은 **그 경로를 변경하지 않는다**로 좁혀 해석한다. `POLICY_SEEDS`(42~71),
`PromotionEvidenceStore`의 write-once 검증, `evaluate_experiment`의 30 시드 요구는 이
spec의 어떤 변경에도 영향받지 않는다 — 이 spec은 `degradation_eval` 계열만 건드린다.

> **확인 필요(§8.2)**: 이 해석은 `#466`(statistical significance safeguards) 도메인에
> 닿는다. spec 리뷰 단계에서 그 소유자에게 "`#514`는 `#478` 30 시드 경로를 건드리지 않고
> 별도 축으로 간다"를 확인받은 뒤 이 절을 확정한다. 확인 전까지 **Proposed**로 둔다.

## 2. 재사용 vs 신규 — `#514` 본문 정정 포함

### 2.1 본문의 낡은 서술

`#514` 본문(2026-08-04 05:32 작성)은 `RollingOriginResult`의 필드가 8개 "전부"라고
적었으나, 그 뒤 `#520`이 5개를 더했다. **현재 13개**(코드 확인,
`degradation_eval.py`):

```text
cutoff_date, window_days, horizon_days,
baseline_val_roc_auc, forward_baseline_roc_auc, forward_baseline_source,
min_auc_drop, overall_roc_auc_mean, recent_roc_auc_mean, recent_window_days,
per_day, degradation_point, training_snapshot_manifest
```

**분리 근거 자체는 유효하다** — 추가된 5개 중 조건 식별자도, 두 실행을 묶는 기록도 없다.
본문의 결론은 그대로 두고 필드 목록만 정정한다.

### 2.2 재사용 자산 — 하나는 그대로 쓸 수 없다

| 필요한 것 | 자산 | 상태 |
| --- | --- | --- |
| paired seed 정책 | `experiment_evaluation.POLICY_SEEDS` | **쓰지 않는다**(§1) |
| write-once receipt 검증 | `promotion_evidence.PromotionEvidenceStore` | 그대로 |
| 조건 격리 좌표·결과 payload | `paired_experiment.PairedExperimentRequest/Result` | **재사용 불가**(§2.3) |
| 두 run의 snapshot·split·seed 재검증 | `training_comparison.verify_training_comparison` | **선행 작업 필요**(§2.4) |
| 단일 조건 측정 하네스 | `degradation_eval.run_rolling_origin` | 확장 대상 |
| 학습 스냅샷 동일성 재료 | `RollingOriginResult.training_snapshot_manifest` | 그대로 |

### 2.3 `PairedExperimentRequest`를 재사용할 수 없는 이유 (코드 확인)

`dataset_fingerprint`와 `split_hash`가 **최상위 단일 필드**다
(`paired_experiment.py:143-144`) — 두 조건이 같은 값을 쓸 것을 요구하는 구조이며,
`_validate_verified_lineage`(291행)가 이를 재검증된 comparison과 대조한다. 조건별
좌표를 담는 `ConditionLineage`(97-106행)에는 dataset/split 항목이 아예 없다
(`source_sha`/`image_digest`/`code_archive_sha`/`code_archive_uri`/`registry_uri`/
`feature_schema_fingerprint`/`model_uri`).

시간축 평가는 **평가일마다 데이터셋이 다르다.** 최상위 단일 fingerprint가 성립하지
않으므로 별도 계약이 필요하다(§4).

### 2.4 `verify_training_comparison` 재사용에는 선행 작업이 있다 — 본문이 놓친 부분

본문은 이 함수를 "그대로 사용"으로 분류했으나, 시그니처가
`verify_training_comparison(baseline_run_id, challenger_run_id, output_path, ...)`
(`training_comparison.py:508`)로 **MLflow run id 두 개**를 받는다.

그런데 `run_rolling_origin`은 cutoff 학습의 run id를 **버린다**. `train.main(...)`이
돌려주는 `TrainingOutcome`에는 `run_id`가 있는데(`train.py:157`), 하네스는
`outcome.val_roc_auc`만 읽고 나머지를 쓰지 않는다 — `degradation_eval.py` 전체에
`run_id` 문자열이 **0건**이다(grep 확인).

따라서 이 spec은 다음을 선행 작업으로 포함한다:

- `RollingOriginResult`에 cutoff 학습의 `training_run_id`를 기록한다.
- 이 필드는 **단일 조건 실행에서도 유효**하므로 기본값 없는 필수 필드가 아니라
  하위호환 가능한 형태로 더한다(기존 결과 JSON을 읽는 경로가 깨지지 않아야 한다).

**추가 주의**: rolling-origin 학습은 `defer_registration=True`로 돌아 **승격 후보로
등록되지 않는다**(`degradation_eval.py`, "측정·리포트 산출물이지 승격 후보가 아니다").
`verify_training_comparison`은 승격 evidence 검증을 전제로 만든 함수이므로, 등록되지 않은
측정 run에 그대로 적용할 수 있는지는 **구현 전 확인이 필요하다**(§8.3). 적용할 수 없으면
§3의 동일성 검증을 `training_snapshot_manifest` 대조만으로 구성한다 — 그 manifest에
dataset/schema sha256, registry generation·sha256, feature_service가 모두 들어 있어
(`training_provenance.py:102-115`) 자체로 성립한다.

## 3. 조건 동일성 검증 — `condition_mismatch`

`TemporalHoldReason`에는 현재 4개만 있다(`degradation_eval.py:612-615`:
`temporal_evidence_missing`, `temporal_ordering_violated`, `temporal_horizon_incomplete`,
`temporal_insufficient_valid_points`). `#485` spec §6이 "`condition_mismatch`는 두 조건
비교가 전제라 `#514` 소관"으로 남겨 뒀고, 이 spec이 추가한다.

### 3.1 검증 항목

| 항목 | 출처 | 왜 필요한가 |
| --- | --- | --- |
| `cutoff_date` | `RollingOriginResult` | 다르면 두 곡선의 `elapsed_days=k`가 **서로 다른 달력 날짜**를 가리킨다 |
| `window_days` | `RollingOriginResult` | 학습 표본 크기가 달라 곡선 높이 차이가 조건 차이로 오독된다 |
| `horizon_days` | `RollingOriginResult` | 관측 범위가 다르면 "열화 미탐지"의 의미가 조건별로 달라진다 |
| 평가일 집합 | `per_day[].date` | 결손일 분포가 다르면 같은 `elapsed_days`가 다른 관측을 가리킨다 |
| `dataset_sha256`/`schema_sha256` | `training_snapshot_manifest` | 학습 데이터가 같은지 |
| `registry_generation`/`registry_sha256` | `training_snapshot_manifest` | feature snapshot이 같은지 |
| `feature_service` | `training_snapshot_manifest` | PIT 조회 서비스가 같은지 |
| seed | §2.4의 신규 기록 필드 | §1.1의 "동일 시드 고정" |

**snapshot 해시만으로는 부족한 이유**: 해시가 다르면 "다르다"만 알고 "왜 다른가"는
모른다. `cutoff_date`/`window_days`/`horizon_days`는 시간축 고유 항목이라 별도로 비교해야
원인이 드러난다(`#485` spec §2가 넘긴 설계 요지).

### 3.2 fail-closed

불일치는 **통계 추정 없이 `hold`로 종료**한다. delta를 계산해 놓고 "참고용"으로 흘리지
않는다 — 근거 없는 숫자가 리포트에 남으면 소비자가 그것을 읽는다. `#485` spec §6의
"관측되지 않은 것을 '안전'으로 바꾸지 않는다"와 같은 결이다.

## 4. 결과 계약 — `temporal-paired-evaluation-v1`

### 4.1 `RollingOriginResult`를 두 조건용으로 개조하지 않는다

두 조건을 한 모델에 욱여넣으면 단일 조건 경로(`measure-degradation` CLI, `#472`가 쓸
`derive_hard_retrain_limit`, `#485`의 `temporal_signal_inputs`)가 전부 조건 선택 인자를
요구하게 된다. **결과 2개 + 페어링 레코드**로 둔다.

```text
TemporalPairedResult
  contract_version : "temporal-paired-evaluation-v1"
  baseline         : RollingOriginResult
  challenger       : RollingOriginResult
  condition_match  : ConditionMatch          (§3의 항목별 판정)
  hold_reason      : TemporalHoldReason|None (§3.2 + 각 조건의 hold 합산)
  delta            : TemporalDelta|None      (§6, hold가 있으면 None)
```

`baseline`/`challenger`는 **그대로 담는다** — 요약해 넣으면 나중에 원본을 다시 못 만든다.

### 4.2 호출 계약 — 조건별 축만 객체로 묶는다

`run_rolling_origin`의 인자는 현재 18개다. 두 조건을 지원한다고 이것을 **두 벌로
늘리지 않는다** — 18개 중 대부분은 두 조건이 **반드시 같아야 하는** 값이고, 두 벌로
두면 다르게 넣는 것이 문법적으로 가능해진다.

| 축 | 인자 | 두 조건 관계 |
| --- | --- | --- |
| 시간 정의 | `cutoff_date`, `window_days`, `horizon_days` | **같아야 함** |
| 데이터 가용성 기준 | `min_rows_per_day`, `min_coverage_days`, `recent_window_days` | **같아야 함** |
| 판정 기준 | `min_auc_drop` | **같아야 함** |
| 시드 | `seed` | **같아야 함**(§1.1) |
| 데이터 소스 | `bigquery_*` | **같아야 함** |
| 실행 제어 | `run_root`, `overwrite`, `best_effort` | §4.3 |
| **조건별** | `feature_service`, `extra_features`, `experiment` | **다를 수 있음** |

따라서 **조건별 축만** 객체로 묶는다.

```python
@dataclass(frozen=True)
class TemporalCondition:
    name: str                                  # "baseline" | "challenger"
    model_features: Sequence[str] | None = None  # train.main에 넘길 실험 피처 선택
    experiment: str | None = None
    source_sha: str | None = None              # 결과에 남길 조건 식별자
```

**`extra_features`는 조건별 필드가 아니다**(§8.3에서 확인). 이 인자는
`run_rolling_origin`에서 데이터셋 조립(`build_training_dataset.main`)과 모델 학습
(`train.main`) **양쪽에** 흘러가는데, 조립에 들어가면 학습 CSV가 물리적으로 달라져
`dataset_sha256`이 어긋나고 `_validate_fairness`가 비교를 거부한다.

```text
데이터셋 조립: 1회, extra_features = 두 조건 model_features의 **합집합**  (공유 축)
조건별 차이  : train.main에 넘기는 model_features                          (조건 축)
```

`feature_service`도 같은 이유로 **공유 축**이다 — 조회 서비스가 다르면 조립 결과가
달라진다. 합집합 피처를 모두 가진 서비스 하나를 상위에서 지정한다.

**행 구성이 달라지지 않음을 코드로 확인했다**(Task 1, 2026-08-05):
`require_extra_feature_columns`는 컬럼 존재만 검사하고 행을 거르지 않으며
(`feast_retrieval.py:153`), 행을 실제로 드롭하는 `drop_user_dynamic_gap_rows`는
`_USER_DYNAMIC_COLUMNS` **고정 6개**(prod 계약)만 본다(56-63행). `apply_cold_start_defaults`는
null을 채울 뿐 드롭하지 않는다. 따라서 합집합 조립은 **컬럼만 늘리고 행은 그대로**이며,
"같은 행 위에서 피처만 다르다"는 전제가 성립한다.

**§3의 런타임 검증보다 한 단계 앞에서 막는 것이 핵심이다.** 공유 축을 상위 호출이 한 번만
받으면 "같아야 하는 값을 다르게 넣는" 입력 자체가 만들어지지 않는다. §3의 검증은 그래도
남긴다 — 결과 두 개를 **밖에서 받아** 묶는 경로(이미 실행된 측정 2건을 사후 비교)가
있고, 그 경로에는 이 방어가 적용되지 않기 때문이다.

`paired_experiment.py`의 `ConditionLineage`가 코드 버전 축에서 같은 형태를 쓰므로 새
패턴이 아니다(`#514` 본문의 "코드 버전 축에서 푼 문제를 시간축에서 다시 푼다"와 같은 결).

`name`은 결과의 어느 슬롯에 들어갈지를 정하는 값이 아니라 **결과에 기록되는 식별자**다.
슬롯은 §4.1의 `baseline`/`challenger` 필드가 이미 이름을 갖는다.

### 4.3 실행 산출물 격리 — `run_root`를 공유하면 첫 조건이 삭제된다

**코드 확인(`degradation_eval.py:676-701`)**: `_prepare_run_root`는 `run_root` 아래에
`training/`과 `evaluation/<date>/`만 만든다 — **조건 차원이 없다.** 그리고 이미 채워진
`run_root`에 대해:

- `overwrite=False`면 `RunRootExistsError`로 막는다.
- `overwrite=True`면 **`shutil.rmtree(run_root)`로 통째로 지우고 다시 만든다.**

따라서 같은 `run_root`로 두 조건을 연달아 실행하면, 두 번째 조건이 **첫 조건의 학습·평가
산출물을 전부 삭제**한다. 두 조건 실행은 반드시 다음 중 하나를 따른다.

```text
채택: run_root/<condition.name>/{training,evaluation/<date>}/
```

- 상위 함수가 조건 이름으로 하위 디렉터리를 나눠 `run_rolling_origin`에 넘긴다.
- `_prepare_run_root`의 fail-closed 성질(이미 채워진 경로를 덮어쓰지 않음)이 조건별로
  그대로 작동한다 — **함수를 고치지 않는다.**
- `overwrite`는 상위에서 받아 두 조건에 같은 값으로 전달한다. 한쪽만 덮어쓰면 두
  산출물의 실행 시점이 어긋난다.

> **문서 드리프트 정정**: `degradation_eval.py` 모듈 docstring(22행)은 "산출물을
> `run_root` 아래 **조건**·평가일별로 격리해 이전 실행을 덮어쓰지 않는다"고 적었으나,
> 실제 레이아웃에 조건 층은 없다. 이 spec의 구현에서 조건 층이 실제로 생기므로,
> **그때 docstring이 사실이 된다.** 구현 커밋에서 이 문장을 확인만 하고 넘기지 말고,
> 조건 층이 상위 함수 소관임을 명시하도록 갱신한다.

### 4.4 hold 합산 규칙

두 조건 각각에 `evaluate_temporal_hold`를 적용하고, **하나라도 hold면 전체가 hold**다.
사유는 조건 이름과 함께 남긴다. 한쪽 곡선이 오염됐는데 다른 쪽이 멀쩡하다고 해서 비교가
성립하지는 않기 때문이다.

## 5. baseline 처리 — 조건별로 둔다

`forward_baseline_roc_auc`는 실행마다 따로 나온다(`per_day` 중 첫 valid 관측치,
`#485` spec §4.3). 두 조건에 **공유 baseline을 강요하지 않는다.**

**이유**: challenger 곡선을 baseline 조건의 기준선으로 재면, challenger가 처음부터 더
높은(또는 낮은) 출발점을 가진 경우 그 차이가 전부 "열화"로 계산된다. 각 조건의
`degradation_point`는 **자기 기준선 대비 언제 꺾였는가**를 뜻해야 한다.

대신 **두 baseline의 차이 자체를 결과에 남긴다**(§6의 `forward_baseline_delta`). 그것이
"출발점에서 이미 얼마나 달랐는가"라는 별개의 사실이고, `degradation_point` 비교를 해석할
때 반드시 함께 봐야 하는 값이다.

실측 근거: cutoff `val_roc_auc` 0.7245와 forward d0 0.5352가 0.19 차이났다(§8.1).
기준선을 잘못 잡으면 판정이 통째로 뒤집히는 크기다.

## 6. `temporal_delta` — `#425` 신호에 연결되는 값

`#485` spec §5.2의 `direction_vs_offline_metric`은 `offline_primary_delta`와
`temporal_delta` 둘 다 있어야 `agree`/`disagree`가 나온다. 단일 조건 실측에서는
`not_applicable`이었다(§8.1). 이 spec이 `temporal_delta`를 만든다.

```text
TemporalDelta
  forward_baseline_delta   : challenger - baseline (출발점 차이, §5)
  per_day_delta            : elapsed_days별 (challenger - baseline)
  overall_delta            : 유효일 delta의 평균
  recent_delta             : 최근 recent_window_days 유효일 delta의 평균
  degradation_point_delta  : challenger.elapsed_days - baseline.elapsed_days | None
```

**`#425`에 넘기는 `temporal_delta`는 `recent_delta`로 한다.**

- **열화 시점 차이가 아닌 이유**: `elapsed_days`는 정수이고 `horizon_days`가 8이면 값이
  0~7뿐이다. 해상도가 낮아 부호가 쉽게 뒤집히고, 한쪽이 미탐지면 `None`이 되어 방향
  판정이 불가능해진다.
- **`overall_delta`가 아닌 이유**: `#485` spec §3이 "최근 구간"을 따로 둔 목적이
  "지금 이 모델을 승격해도 되는가"에 답하는 것이다. 전체 평균은 이미 지난 구간의 우위를
  현재 판단에 섞는다.
- **양쪽 유효일이 `recent_window_days` 미만이면 `recent_delta = None`**이고, 그때
  `direction_vs_offline_metric`은 `not_applicable`이 된다 — 적은 표본으로 방향을 만들지
  않는다(§3.2와 같은 결).

**알려진 한계**: `recent_delta`의 표본은 `recent_window_days`(기본 3)개뿐이라 노이즈에
취약하다. 세 후보 중에서는 목적에 가장 맞지만, 하루치 변동이 부호를 뒤집을 수 있다.
**지금 풀지 않는다** — 실제로 노이즈가 판정을 뒤집는 사례가 실측되면 그때
`recent_window_days`를 늘리거나 신뢰구간을 더하는 후속 이슈로 대응한다. 관측 없이 미리
보정하면 이 spec의 범위가 커지고, 보정값 자체가 또 근거 없는 상수가 된다
(`min_auc_drop`을 실측 없이 정하지 않은 것과 같은 이유 — `#485` spec §7.3).

### 6.1 `evaluation_id` 해시 결정에 영향이 있다

`#485` spec §5.3은 `temporal_signal`을 `evaluation_id` 해시 payload에서 제외하면서
**"`#514`에서 `direction_vs_offline_metric`이 실제 판정 입력이 되면 id 근거도 함께
바꿔야 한다"**를 뒤집힐 조건으로 남겼다.

이 spec은 `temporal_delta`를 **만들기만** 하고 판정 입력으로 승격시키지 않는다 — §비목적의
"프로덕션 배선은 `#472` 소유"와 같은 경계다. 따라서 **이 spec 범위에서는 §5.3의 결정이
유지된다.** 뒤집는 판단은 배선 시점에 `#472`가 한다. 이 문장을 남기는 이유는, 나중에
"§5.3이 `#514`를 지목했는데 `#514` spec에는 아무 말이 없다"가 되지 않게 하기 위해서다.

## 7. 시각화 확장

`scripts/bench/degradation_curve_plot.py`는 현재 단일 조건 전제다 — `result["per_day"]`
하나에서 trace 하나를 그린다(29-39행).

- 두 조건 곡선을 같은 x축(`elapsed_days`)에 겹쳐 그린다.
- delta를 **보조 축 또는 하단 subplot**으로 분리한다. 같은 축에 두면 ROC-AUC(0.5~0.7)와
  delta(±0.05)의 스케일 차이로 delta가 평평한 선이 된다.
- 각 조건의 threshold선과 `degradation_point`를 조건 색으로 구분해 표시한다.
- `hold`가 있으면 **곡선을 그리되 경고 배너를 넣는다.** 그리지 않으면 "왜 안 나오지"를
  추적해야 하고, 경고 없이 그리면 근거 없는 곡선을 읽는다.

기존 결과 JSON(단일 조건)도 계속 그릴 수 있어야 한다 — `#510`/`#520` 산출물이 이미 있다.

## 8. 실측 근거와 미해결 항목

### 8.1 이 spec이 근거로 삼은 실측 (2026-08-04, 코드 `59ef767`)

기록: `experiments/2026-08-03_model-degradation-rolling-origin/notes.md`

```text
cutoff 2026-07-27, W=15, H=8, seed 42
seed_std(3시드)      = 0.0046651645
min_auc_drop         = max(0.005, 2 × seed_std) = 0.009330
cutoff val_roc_auc   = 0.7245
forward_baseline(d0) = 0.5352        ← 0.19 차이 (§5)
degradation_point    = elapsed_days 7
유효 평가일           = 8 / 8,  hold = None
temporal signal      = confidence high, direction=not_applicable  ← §6이 채운다
```

**이 조합을 이 spec의 검증 실험대로 쓴다.** 결손일 분포상 07-27~08-03이 유일한 연속
8일이며(notes.md), 두 조건 실행도 같은 구간을 써야 §3의 "평가일 집합 동일"이 성립한다.

### 8.2 `#466`/`#478` 소유자 확인 — §1.2 확정 조건

§1.2의 "하위호환 = 그 경로를 변경하지 않음" 해석은 statistical significance safeguards
도메인에 닿는다. **spec 리뷰 단계에서 확인받은 뒤 이 spec을 Accepted로 올린다.** 구두
확인은 근거로 적지 않고 이슈 코멘트 링크를 남긴다(`#485` spec §7.1에서 확립한 관례).

### 8.3 `verify_training_comparison` 적용 가능성 — **확인 완료(2026-08-05, Task 1)**

**결론: 등록 여부는 문제가 아니었다. 진짜 제약은 `dataset_sha256` 동일성 요구다.**

#### (1) `defer_registration=True`는 이 함수를 막지 않는다

`_load_verified_run`이 요구하는 것은 run에 붙은 **artifact 3개**뿐이다
(`training_comparison.py:54-56`):

```text
reproducibility/snapshot/training_dataset.csv
reproducibility/snapshot/snapshot_manifest.json
reproducibility/split/split_manifest.json
```

이 셋을 남기는 `_log_reproducibility_artifacts` 호출은 `train.py:620`의
**`if snapshot_manifest is not None:`** 안에 있다 — `defer_registration` 분기와 무관하다.
그리고 rolling-origin의 cutoff 데이터셋은 snapshot manifest를 갖는다(실측 결과 JSON에
`training_snapshot_manifest`가 채워져 있다). **따라서 artifact는 남는다.**

#### (2) 진짜 제약 — `_validate_fairness`가 두 조건의 데이터셋 동일을 요구한다

`_validate_fairness`(`training_comparison.py:282-306`)는 다음을 **모두 같아야 한다**고
본다: `dataset_sha256`, 세 split의 `row_count`·`membership_sha256`,
`split_seed`/`model_seed`/`sampler_seed`.

그런데 `run_rolling_origin`은 `extra_features`를 **두 곳에** 넘긴다:

- `build_training_dataset.main(..., extra_features=...)` (820행) — **물리 CSV가 달라진다**
- `train.main(..., extra_features=...)` (835행) — 모델 피처 컬럼 선택

즉 "challenger = baseline + 실험 피처"로 두 조건을 잡으면 **학습 CSV가 물리적으로 달라져
`dataset_sha256`이 어긋나고, `verify_training_comparison`이 그 비교를 거부한다.**

#### (3) 해소 방향 — 데이터셋은 한 번만 만든다 (**§4.2 설계 변경 필요**)

`paired_experiment`가 `dataset_fingerprint`를 **최상위 단일 필드**로 둔 것이 바로 이
계약이다(§2.3) — 두 조건은 **같은 데이터셋**을 쓰고 **코드/피처 선택만** 다르다.
시간축도 같은 형태를 따른다:

```text
데이터셋 조립: 1회, extra_features는 두 조건의 **합집합**  (공유 축)
조건별 차이  : train.main에 넘기는 모델 피처 선택          (조건 축)
```

이렇게 하면 `dataset_sha256`·split이 동일해져 `_validate_fairness`를 통과하고,
비교 자체도 강해진다 — **같은 행·같은 분할 위에서 피처 집합만 다른** 대조가 된다.

**따라서 §4.2의 `TemporalCondition`에서 `extra_features`를 그대로 조건별 필드로 두면
안 된다.** 조립용(공유)과 모델 피처 선택용(조건별)으로 역할을 나눠야 한다. 이 변경은
설계 결정이므로 리뷰 승인 후 §4.2에 반영한다.

#### (4) 남은 확인 (Task 2에서)

- `_verify_promotion_evidence`가 receipt 없는 run에서 `None`을 돌려주는지
  (docstring은 "양쪽 run에 receipt가 없으면 필요 없다"고 적었으나 코드 미확인).
- `_publish_verified_comparison`이 challenger run에 artifact를 **되쓴다** — 측정 run에
  부수효과를 남기는 것이 허용되는지.

### 8.4 `#472`에 넘기는 항목 (이 spec 범위 밖)

`#485` spec §4.2의 사유 표에 **`limit_days=0` / `reason=None`** 조합이 빠져 있다.
`safety_margin_days == degradation_point.elapsed_days`면 뺄셈이 음수가 아니라 정확히 0이라
clamp 분기를 타지 않는다(`degradation_eval.py:593`, 실측에서 확인). 표의 마지막 행이
"양수 | `None` | 정상 산출"이라 소비자가 표대로 구현하면 미처리로 남는다. 동작 버그는
아니고 표를 "0 이상"으로 넓히면 해소된다.

## 9. 구현 순서 (plan에서 상세화)

1. `RollingOriginResult`에 `training_run_id`·seed 기록 (§2.4, 하위호환)
2. `condition_match` 검증과 `condition_mismatch` hold 사유 (§3)
3. `TemporalPairedResult` 계약과 hold 합산 (§4)
4. `TemporalDelta` 산출 (§5·§6)
5. Plotly 두 조건 확장 (§7)
6. 실측 실험대(§8.1)로 2조건 실행 1회

1~5는 `degradation_eval` 계열과 bench 스크립트만 건드리며 `experiment_evaluation.py`·
`paired_experiment.py`·`promotion_gate.py`를 수정하지 않는다.
