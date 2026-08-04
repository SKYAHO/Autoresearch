# temporal signal 승격 판정 연결 — 유효 기간·hard retrain limit·다중 신호 (#485 잔여 범위)

- **상태**: Proposed
- **날짜**: 2026-08-04
- **이슈**: #485 (재사용 컴포넌트는 #510에서 착지 완료)
- **선행 계약**:
  - `docs/specs/2026-08-03-model-degradation-rolling-origin-evaluation.md` — rolling-origin
    측정 하네스(`run_rolling_origin`/`RollingOriginResult`/`detect_degradation_point`).
    이 spec이 **소비하는 입력**을 정의한다.
  - `docs/specs/2026-08-04-reranking-metric-alignment.md` (#505/#506) — `PASSTHROUGH_COLUMNS`
    계약. §5의 staleness 확장이 이 기제를 쓴다.
  - `docs/plans/2026-08-03-experiment-evaluation-unification.md` (#493) — 판정 엔진 단일화.
    §6의 미해결 항목이 이 문서와 충돌 가능성을 다룬다.
  - `docs/guides/training-experiment-provenance.md` §5 (write-once evidence, #423/#466/#478)

## 목적

`#510`이 착지시킨 rolling-origin 측정 하네스는 "언제 성능이 꺾이는가"를 관측만 한다.
이 spec은 그 관측 결과를 **판정에 연결**한다 — 모델 유효 기간과 hard retrain limit을
결정론적으로 산출하고, temporal signal을 `#425`의 다중 신호 판정 스키마에 실어
`confidence`/`robustness_note`로 신호 충돌을 설명한다.

## 비목적

- **측정 하네스 재구현** — `run_rolling_origin`은 그대로 호출한다(#510 착지분).
- **`#472`의 게이트 배선** — hard retrain limit **값 산출 절차·기준**은 이 spec이, 그
  값을 `#461` 승격 게이트에 배선하는 것은 `#472`가 소유한다(`#472` 본문 2026-08-04 갱신
  기준). 이 spec은 값 자체를 확정하지 않는다(§4.1·§4.2, 사유 포함).
- **baseline/challenger 두 조건 비교** — `#514`로 이관(§2). 이 spec은 단일 조건 기준이다.
- **production Feature Service·production alias 변경** — `#485` 본문의 명시 제약.
- **주 지표 교체** — `#506`이 grouped ROC-AUC를 **병기**만 한 것과 같은 이유로, 이
  spec도 temporal signal을 주 지표로 승격시키지 않는다. 주 지표 일반화는 `#493` 소유.

## 1. 재사용 vs 신규 (착지분 기준 갱신)

| 구분 | 항목 | 상태 |
| --- | --- | --- |
| 재사용 | `run_rolling_origin`, `RollingOriginResult`, `PerDayResult` | #510 착지 완료 |
| 재사용 | `detect_degradation_point`, `compute_min_auc_drop` | #510 착지 완료 |
| 재사용 | Plotly 곡선(`degradation_curve_plot.py`), `measure-degradation` CLI | #510 착지 완료 |
| 재사용 | paired seed 정책(`POLICY_SEEDS`, 42..71 30개), write-once evidence | #478 기존 계약 |
| 재사용 | `verify_training_comparison`(snapshot·split·seed 동일성 재검증) | #454 기존 계약 |
| **신규** | 최근 구간 vs 전체 구간 지표 분리 (§3) | 이 spec |
| **신규** | baseline 재정의(`forward_baseline_roc_auc`) (§4.3) | 이 spec |
| **신규** | 유효 기간·hard retrain limit 산출 (§4.1·§4.2) | 이 spec |
| **신규** | temporal signal → `#425` 스키마 연결 (§5) | 이 spec (부착 지점 미해결, §7.1) |
| **신규** | fail-closed `hold` 종료 조건 (§6) | 이 spec |
| **이관** | baseline/challenger 두 조건 확장·시간축 paired 계약 (§2) | **`#514`** |

## 2. baseline/challenger 동일 조건 검증 — **`#514`로 이관**

`#485` 작업 범위 "baseline과 challenger가 동일한 as-of cutoff, feature snapshot,
split 규칙, paired seed를 사용하도록 검증한다"는 **하네스 구조 변경**을 요구하므로
`#514`로 분리했다. 이 spec은 단일 조건 기준으로 §3~§6을 확정한다.

**분리한 구조적 이유**: `RollingOriginResult`에는 조건 식별자(`condition`/`source_sha`/
`model_uri`)도, 두 실행을 묶는 기록도 없다(필드는 `cutoff_date`/`window_days`/
`horizon_days`/`baseline_val_roc_auc`/`min_auc_drop`/`per_day`/`degradation_point`/
`training_snapshot_manifest`가 전부다 — 코드 확인). 두 번 호출하면 서로 무관한 관측
2건이 남을 뿐, "같은 조건에서 비교했다"는 사실이 검증 가능한 형태로 어디에도 남지
않는다. `paired_experiment.py`가 코드 버전 축에서 `ConditionLineage`로 푼 문제를
시간축에서 다시 풀어야 하는 이유다 — "그냥 두 번 부르면 되지 않나"가 안 되는 지점이
여기다.

`#514`가 이어받는 설계 요지(이 spec이 넘기는 것):

- 재사용 가능: `verify_training_comparison`(#454, snapshot·split·seed 동일성),
  `POLICY_SEEDS`(#478, 42~71), `PromotionEvidenceStore`(#478).
- 신규 필요: 시간축 고유 항목(`cutoff_date`/`window_days`/`horizon_days`) 동일성 검증.
  cutoff가 다르면 두 곡선의 `elapsed_days=k`가 서로 다른 달력 날짜를 가리켜 같은 x축에
  놓을 수 없는데, snapshot 해시로는 "다르다"만 알고 "왜 다른가"는 모른다.
- **재사용 불가**: `PairedExperimentRequest`는 `dataset_fingerprint`/`split_hash`를
  최상위 **단일 필드**로 두고 두 조건이 같은 값을 쓸 것을 요구한다
  (`_validate_verified_lineage`). 시간축 평가는 평가일마다 다른 데이터셋을 쓰므로 그
  전제가 성립하지 않는다 → 별도 계약(`temporal-paired-evaluation-v1`, 가칭).

## 3. 최근 구간 vs 전체 구간 지표 분리

`#485` 완료 조건: "최근 평가 구간의 성능과 전체 평가 기간의 성능이 구분되어 저장된다."

```text
overall_roc_auc_mean   = per_day에서 status==valid인 날의 roc_auc 평균
recent_roc_auc_mean    = 위 중 최근 recent_window_days개 유효일의 평균
recent_window_days     = 기본 3 (근거는 아래)
```

- **왜 평균인가**: `#506`이 grouped AUC에서 매크로 평균(그룹 동등 가중)을 택한 것과
  같은 이유 — 행 수가 많은 날이 지표를 지배하면 "날의 평균"이 아니라 "행의 평균"이
  된다. 열화는 날 단위 현상이므로 날 동등 가중이 맞다.
- **`recent_window_days=3`의 근거**: `UserDynamicView.ttl=60h`(약 2.5일)가 이 저장소에서
  "최신"의 실질 경계다(`feature_repo/feature_definitions.py`). 3일보다 짧으면 유효일
  결손 하나로 표본이 1~2개가 되고, 길면 "최근"이라는 말이 무의미해진다. **plan 단계에서
  실측 후 재조정 대상**이며 확정 정책값이 아니다.
- 유효일이 `recent_window_days` 미만이면 `recent_roc_auc_mean=None`이고 사유를 남긴다 —
  적은 표본으로 평균을 만들어 "최근 성능"이라고 부르지 않는다.

## 4. baseline 재정의와 hard retrain limit

`#485` 작업 범위: "성능과 무관하게 일정 기간이 지나면 재학습하도록 hard retrain limit과
다음 재학습 시각을 **정의**한다."

**§4.3(baseline 재정의)을 먼저 읽어야 한다** — §4.1·§4.2가 전부 그 곡선 위에서
계산되므로, 기준선이 틀리면 나머지도 틀린다.

### 4.1 hard retrain limit 산출

```text
hard_retrain_limit_days = degradation_point.elapsed_days - safety_margin_days
next_retrain_at         = last_trained_at + hard_retrain_limit_days
```

- **`degradation_point`가 없으면 값을 만들지 않는다.** `no_degradation_detected`면
  `None` + 사유 `no_degradation_observed_within_horizon`,
  `insufficient_valid_points`면 `None` + 그 사유 그대로 전달.
  **관측되지 않은 것을 "안전하다"로 바꾸지 않는다** — `horizon_days`가 짧아서 못 본
  것과 실제로 안 꺾인 것은 다른 사실인데, 하한값(예: "마지막 유효일 + 1")으로 채우면
  둘이 같은 숫자가 된다. 이 fail-closed 태도가 §6의 `hold` 조건과 같은 결이다.
- `safety_margin_days` 기본값은 이 spec이 정하지 않는다 — 단일 cutoff·단일 시드 관측
  하나로 운영 정책 상수를 못 박으면 선행 spec §7이 경고한 "축소 설정의 결론을 정책
  근거로 쓰는 것"이 된다. 다중 origin(선행 spec §10) 관측이 쌓인 뒤 `#472`에서 확정한다.
- `safety_margin_days`가 `degradation_point.elapsed_days`보다 크면 계산이 음수가 된다.
  이 경우 `limit_days=0`으로 clamp하되 사유를 남긴다 — **소비자가 처리해야 하는 사유가
  총 3가지**이며 §4.2의 표가 정본이다.

### 4.2 산출물은 `RollingOriginResult`에 얹지 않는다

`derive_hard_retrain_limit(result: RollingOriginResult, *, safety_margin_days: int)`
형태의 **별도 함수**가 결과를 입력으로 받아 계산한다.

측정(관측 사실)과 정책(운영 판단)을 같은 payload에 섞지 않기 위해서다 — `#472`가
게이트 배선을, 이 spec이 값 산출 절차·기준을 소유하는 경계와 같은 결이다.

`#485` 참고 사항의 "재학습 리밋 값은 구현 전 spec에서 확정한다"에 대해, 이 spec은
**값이 아니라 산출 절차·기준을 확정**한다. 값을 정하려면 데이터 가용성 실측
(선행 spec §3의 `A`/`D`, 실행 시점마다 달라짐)과 `min_auc_drop` calibration이
선행돼야 하고, 둘 다 GCP 자격증명이 있는 환경에서만 가능하다. 이 스코프 조정은
`#485`에 코멘트로 남겨 이슈 오너 확인을 받는다.

#### `HardRetrainLimit`이 낼 수 있는 사유 — **소비자가 전부 처리해야 한다**

`limit_days`가 `None`이거나 `0`일 때 `reason`이 함께 온다. **`#472`가 이 값을 `#461`
게이트에 배선할 때 아래 세 가지를 모두 분기해야 한다** — `limit_days` 숫자만 보고
판단하면 안 된다.

| `limit_days` | `reason` | 의미 |
| --- | --- | --- |
| `None` | `no_degradation_observed_within_horizon` | 관측 범위 안에서 열화가 안 잡혔다. **"안전하다"가 아니다** — `horizon_days`가 짧아서 못 봤을 수 있다. |
| `None` | `insufficient_valid_points` | 유효 관측치가 2개 미만이라 곡선 자체가 없다. 측정 단계의 사유를 그대로 전달한다. |
| `0` | `safety_margin_exceeds_degradation_point` | `safety_margin_days`가 `degradation_point.elapsed_days`보다 커서 계산이 음수가 됐다. **이미 재학습 시점을 지났다**는 뜻이다. |
| 양수 | `None` | 정상 산출. `elapsed_days - safety_margin_days`. |

세 번째 행이 특히 함정이다: `limit_days=0`만 보고 "즉시 재학습"으로 읽으면 맞지만,
**계산이 깨진 것과 구분이 안 된다.** 음수를 그대로 흘려보내는 대신 `0`으로 clamp하되
사유를 남기는 이유가 이것이다 — 조용히 뭉개지 않는다(§4.1의 "관측되지 않은 것을
'안전'으로 바꾸지 않는다"와 같은 결). 이 케이스는 spec 초안에 없던 분기이며
구현(#485 Task 2) 중에 추가했고, `test_derive_hard_retrain_limit_clamps_negative_to_zero`로
고정했다.

### 4.3 baseline 재정의 — `forward_baseline_roc_auc` (선행 과제)

`#485`의 나머지 항목은 전부 "열화 곡선을 믿을 수 있는가"에 의존한다. 선행 spec §10이
기록한 baseline 오프셋(랜덤 val이 forward held-out보다 약 4%p 높음,
`experiments/2026-07-31_training-window-length/notes.md`)을 먼저 해소하지 않으면
hard retrain limit도 `#425` 신호도 왜곡된 곡선 위에서 계산된다.

`RollingOriginResult`에 필드를 **추가**하고 기존 필드는 유지한다(하위호환).

```text
baseline_val_roc_auc      # 유지 — cutoff 학습의 랜덤 val 지표(참고용, 과거 실행과 비교 가능)
forward_baseline_roc_auc  # 신규 — per_day 중 첫 valid 관측치의 roc_auc
forward_baseline_source   # 신규 — 그 값을 준 elapsed_days(없으면 None)
```

`detect_degradation_point`의 `baseline` 인자에는 `forward_baseline_roc_auc`를 넘긴다 —
같은 산출 경로(forward held-out)끼리 비교해 오프셋을 **정의상 상쇄**한다.

- **왜 `per_day[0]`인가**: cutoff 직후 첫 유효일은 "학습 직후 시점의 forward 성능"이라
  열화의 출발점 정의에 가장 가깝다. 랜덤 val은 학습 데이터와 같은 날들에서 뽑히므로
  애초에 다른 문제를 잰다(선행 spec §10 실측).
- **버리는 대안**: (a) 랜덤 val 유지 — 오프셋이 남아 `elapsed_days` 0~1 오탐이 계속된다.
  (b) 첫 K일 평균을 기준선으로 — 열화가 K일 안에 시작되면 기준선 자체가 오염된다.
  (c) cutoff 이전 마지막 날로 별도 forward 평가 — 조립·평가 1회가 더 들고, 그 날은
  학습에 포함돼 held-out이 아니다.
- **유효 관측치가 0개면** `forward_baseline_roc_auc=None`이고, `degradation_point`는
  기존대로 `insufficient_valid_points`로 끝난다.
- **새 계산 경로가 없다** — `run_rolling_origin`이 이미 산출하는 `per_day` 값을 읽기만
  한다. 오프셋 상쇄가 실측 검증 없이 주장되는 게 아니라, 같은 함수가 만든 같은 종류의
  값끼리 비교하므로 정의상 보장된다.

**선행 spec §2.4는 이 개정으로 부분 supersede된다** — 그 문서 §2.4에
`> [!IMPORTANT] **[부분 supersede — #485, 2026-08-04]**` 블록을 함께 남긴다.
`run_rolling_origin` 실행 기록이 0건인 시점의 개정이라 소급 영향이 없다.

#### 남은 트레이드오프 — 기준선이 단일 관측치다 (PR #520 리뷰 Low#6)

기준선이 다수 행의 val 분할에서 **평가일 관측치 1개**로 바뀌었다. 4%p 계통 오프셋을
없앤 대신, **그날의 표본 변동이 기준선에 그대로 실린다**. `min_auc_drop`은
`compute_min_auc_drop`이 시드 간 변동폭(`k × seed_std`)으로 calibration한 값인데, 그
전제(기준선이 안정적)가 약해진 셈이다.

`#425` 배경(round_002)에서 "격차가 임계값의 2.6배였는데도 신뢰할 수 없었던 원인이 표본
민감도"였던 것과 같은 종류의 위험이다. 이 spec은 그래서:

- `forward_baseline_source`를 결과에 남겨 **어느 날이 기준선이 됐는지** 사후 추적을
  가능하게 한다. 결손이 앞에 끼면 0이 아닐 수 있고, 그 날이 이미 떨어진 지점이면
  threshold 전체가 낮게 잡힌다.
- `degradation_point.elapsed_days <= 1`이면 `confidence=low`로 낮춘다(§5.1) — 표본
  1~2개짜리 판정을 높은 신뢰도로 내보내지 않는다.

**후속 과제**: "첫 K개 유효일 평균"을 기준선으로 쓰는 안은 열화가 K일 안에 시작되면
기준선 자체가 오염되는 문제가 있어 이번 범위에서 채택하지 않았다. 실측 데이터가 쌓인
뒤 `forward_baseline_source` 분포를 보고 재검토한다.

## 5. `#425` 다중 신호 판정 연결

`#425` 완료 조건이 요구하는 필드를 temporal signal에 맞춰 채운다.

```text
confidence: high | medium | low
robustness_note: str | None
direction_vs_offline_metric: agree | disagree | not_applicable
```

### 5.1 confidence 산출 규칙(결정론적)

**§5는 §6의 `hold` 조건을 통과한 경우에만 실행된다.** 따라서 `hold`로 걸러지는 조건
(유효 평가일 < 2, 동일 조건 검증 실패 등)은 여기서 다시 검사하지 않는다 — 실행 흐름상
도달할 수 없는 분기를 코드로 옮기지 않기 위해서다.

```text
low     ⟸ hard_retrain_limit_days is not None AND degradation_point.elapsed_days <= 1
          OR  recent_roc_auc_mean is None
medium  ⟸ 유효 평가일 < recent_window_days + 2  OR  degradation_point 미탐지
high    ⟸ 그 외
```

- **`degradation_point.elapsed_days <= 1`을 `low`로 두는 이유**: 열화가 첫 1~2일에
  잡혔다는 건 곡선을 이루는 관측 표본이 사실상 1~2개라는 뜻이라 **통계적으로
  불안정**하다. §4.3의 baseline 재정의로 4%p 오프셋은 이미 상쇄됐으므로 "오프셋과
  구분 안 됨"은 더 이상 사유가 아니다 — 표본 크기 자체가 사유다.
- `#425` 배경(round_002)의 교훈을 그대로 적용한다 — "격차가 임계값의 2.6배였음에도
  결과가 신뢰할 수 없는 수준"이었던 이유는 표본(시드) 민감도였다. temporal signal의
  대응물은 **유효 평가일 수**다. 그래서 delta 크기가 아니라 관측 밀도로 confidence를
  정한다.

### 5.2 direction_vs_offline_metric

```text
agree     ⟸ (offline 주지표 delta > 0) == (temporal recent_roc_auc_mean delta > 0)
disagree  ⟸ 부호가 반대
not_applicable ⟸ 어느 한쪽이 None
```

두 조건 비교(`#514`) 전까지는 `not_applicable`이 기본이다 — 단일 조건 실행에는
비교 대상 delta가 없다. `#514` 착지 후 baseline/challenger의
`recent_roc_auc_mean` 차이로 채운다.

`disagree`는 **실패가 아니다**(`#425` 완료 조건: "'기각'·'낮은 신뢰도'가 실패가 아닌
정상 종료 경로"). `robustness_note`에 두 신호가 갈린 사실을 남기고 `confidence`를
`medium` 이하로 낮춘 뒤 정상 종료한다.

### 5.3 스키마 부착 지점 — **미해결, §7.1 참고**

`confidence`/`robustness_note`/`direction_vs_*` 세 필드는 현재
`ExperimentEvaluation`·`PromotionDecision`(`experiment_evaluation.py`)에 **없다**
(코드 확인: `verdict`, `reason_codes`, CI 상·하한, 평균값만 존재). 두 배치안이 있고
**이 spec은 어느 쪽도 확정하지 않는다**:

- **안 A — `ExperimentEvaluation`에 필드 추가**: `#425` 완료 조건 문구("판정 산출물
  스키마")에 가장 충실하다. 단 `#493` 플랜 D2가 같은 함수 시그니처를 건드리므로 충돌
  위험이 있다.
- **안 B — temporal 결과에만 담고 판정 엔진은 소비만**: 충돌이 없고 `#485` 범위 안에서
  닫힌다. 단 `#425`가 요구한 "판정 산출물"에 직접 들어가지 않아 완료 조건 해석이
  느슨해진다.

선택은 §7.1이 풀린 뒤에 한다.

## 6. fail-closed `hold` 종료 조건

`#485` 작업 범위: "데이터 부족, 미래 구간 누락, feature snapshot 불일치, 시간 순서
위반은 통계 추정 없이 `hold`로 종료한다."

| 조건 | 탐지 방법 | reason code(가칭) | 담당 |
| --- | --- | --- | --- |
| 데이터 부족 | 유효 평가일 < 2 | `temporal_insufficient_valid_points` | 이 spec |
| 미래 구간 누락 | `per_day` 길이 < `horizon_days` **또는 꼬리가 `missing_date`** | `temporal_horizon_incomplete` | 이 spec |
| 시간 순서 위반 | `training_window` 종료일 >= `cutoff_date` | `temporal_ordering_violated` | 이 spec |
| temporal evidence 부재 | `RollingOriginResult` 자체가 없음 | `temporal_evidence_missing` | 이 spec |
| feature snapshot 불일치 | 두 조건의 cutoff·window·horizon·snapshot·split·seed 중 상이 | `temporal_condition_mismatch` | **`#514`**(두 조건 비교가 전제) |

**"미래 구간 누락"이 길이 검사만으로는 안 잡히는 이유** (PR #520 리뷰 Medium#3):
`run_rolling_origin`은 모든 평가일에 대해 `PerDayResult`를 **반드시 하나씩** append하므로
(실패해도 `best_effort=True`면 `evaluation_failed`, `False`면 예외로 결과 자체가 안 생김),
정상 산출물에서 `len(per_day) < horizon_days`는 성립하지 않는다 — 그 검사는 hand-built
결과에 대한 심층 방어일 뿐이다. 실제 운영 케이스는 **꼬리가 `missing_date`로 채워지는 것**이다:
`cutoff+H`가 아직 지나지 않았거나 데이터 레이크가 뒤처진 시점에 실행하면 길이 검사를
통과해버리고, 앞쪽 유효일이 2개 이상이면 hold도 안 걸린 채 **잘린 구간 위에서 계산된**
평균·열화 시점이 그대로 나간다. 그래서 꼬리 결손도 같은 사유로 잡는다.

중간 결손은 여기 해당하지 않는다 — 관측이 horizon 끝까지 도달했기 때문이다.

**"시간 순서 위반"의 실제 의미**: 선행 spec §2.1이 학습 구간을 `[cutoff-W, cutoff)`로
고정하고 `events_end_date = cutoff - 1일`로 환산하므로, 정상 경로에서는 이 위반이 나올
수 없다. 이 검사는 호출부가 `RollingOriginResult`를 손으로 만들어 넣는 경우를 막는
심층 방어다 — `#478`의 write-once evidence가 "producer가 보낸 숫자를 믿지 않는다"는
원칙과 같다.

## 7. 미해결 항목 (구현 착수 전 확정 필요)

### 7.1 `#493` 판정 엔진 단일화와의 충돌 — **가장 중요**

`#493`은 2026-08-04에 CLOSED(COMPLETED)됐지만, **`src/pipeline/experiment_evaluation.py`
코드는 바뀌지 않았다**(마지막 변경은 `f8bf611`/#478). 머지된 것은 계획 문서뿐이다
(`#500`, `docs/plans/2026-08-03-experiment-evaluation-unification.md`).

그 계획은 이 파일을 실제로 재구조화한다고 명시한다 — `evaluate_experiment()`에
`declared_minimum` 키워드 인자 추가, reason code
`PRIMARY_DELTA_BELOW_DECLARED_MINIMUM` 신설, 미사용 reason code 6개 정리 등.

**따라서 §5.3(스키마 부착 지점)을 지금 확정하면 그 재구조화와 충돌할 수 있다.**
구현 착수 전에 확인해야 한다:

- `#493`의 코드 구현이 별도 이슈로 남아 있는가, 아니면 계획만 남기고 종료된 것인가?
- 남아 있다면 이 spec의 §5는 그 작업 이후로 미뤄야 한다.
- 확인 대상: hyochangsung(#493 assignee).

**현재 상태(2026-08-04 기준): 미해결.** `#493`에 질문 코멘트를 남겼으나
(`issues/493#issuecomment-5174828099`) **답변이 아직 없다** — 그 코멘트가 이슈의
마지막 코멘트다. 구두로 확인받았더라도 이 spec에 근거로 적지 않는다: 검증 불가능한
근거가 GitHub 문서에 사실처럼 고정되면, 나중에 이 문서만 읽는 사람이 추적할 방법이
없다("GitHub이 source of truth"). 유효한 확인이 있다면 `#493`에 코멘트로 남겨달라고
요청한 뒤, 그 링크를 근거로 여기에 기록한다.

이 항목이 풀리기 전까지 **§5(스키마 연결)는 구현하지 않는다.** §3·§4·§6은
`experiment_evaluation.py`를 건드리지 않으므로 선행 가능하다.

### 7.2 video staleness 활성화 — `PASSTHROUGH_COLUMNS` 확장

선행 spec §10에 기록된 대로, `_resolve_staleness_summary`는 평가 CSV에
`video_id`/`event_timestamp`가 없어 항상 `UNAVAILABLE`로 떨어진다.

`#506`이 도입한 `PASSTHROUGH_COLUMNS`(`model_contract.py:48`, 현재 `("user_id",)`)가
정확히 이 문제를 푸는 기제다 — 여기에 `video_id`/`event_timestamp`를 더하면 조립이
엔티티 키를 보존하고 staleness가 켜진다.

다만 이건 **feature contract SSOT 변경**이고 `#505`/`#506`(bbungjun)의 최근 작업
영역이다. 또한 스냅샷 해시가 바뀌어 진행 중인 짝지은 비교의 baseline 재학습이 필요해진다
(`#506` spec이 이미 그 영향을 명시했다). 그래서 이 spec은 **제안만 하고 직접 바꾸지
않는다** — bbungjun님께 확장 가부를 요청해야 한다.

staleness 없이도 §3~§6은 성립한다(staleness는 진단 보조 지표이지 판정 입력이 아니다).

### 7.3 실측이 필요한 값

| 값 | 결정 절차 | 현재 상태 |
| --- | --- | --- |
| `safety_margin_days` | §4.1, 다중 origin 관측 후 `#472`에서 확정 | 미확정 |
| `recent_window_days` | §3, 기본 3. `measure-degradation --recent-window-days`로 노출(PR #520 리뷰 Medium#5) | 실측 후 재조정 |
| `min_auc_drop` | 선행 spec plan Task 7-A | 미확정(GCP 자격증명 필요) |

셋 다 GCP 접근이 가능한 환경에서 선행 spec plan Task 1·7-A를 먼저 실행해야 한다.

## 8. 구현 순서

```text
1단계 — §4.3 baseline 재정의 (선행, 다른 항목이 전부 이 곡선에 의존)
  forward_baseline_roc_auc / forward_baseline_source 필드 추가
  detect_degradation_point의 baseline 인자 교체
  선행 spec §2.4에 부분 supersede 블록
  → degradation_eval.py 안에서 닫힘, 다른 담당자 영역 미접촉

2단계 — §3 구간 분리 + §4.1·§4.2 hard retrain limit
  overall_roc_auc_mean / recent_roc_auc_mean
  derive_hard_retrain_limit (별도 함수, 결과에 안 얹음)

3단계 — §6 fail-closed hold 조건 4개 (두 조건 전제인 condition_mismatch 제외)

4단계 — §5 #425 스키마 연결  ⚠️ §7.1(#493 답변) 확인 후에만 착수

별도 이슈 — #514: 두 조건 확장(§2), condition_mismatch hold, Plotly 2곡선
별도 요청 — §7.2: video staleness 활성화(bbungjun님 PASSTHROUGH_COLUMNS 확장)
```

**공통 블로커**: 선행 spec plan Task 1(BigQuery 실측 — `A`/`D` 확정)과 Task 7-A
(`min_auc_drop` calibration)는 여전히 미완이다. GCP 자격증명이 있는 환경에서 선행돼야
1~3단계 결과를 실측으로 확인할 수 있다 — 코드 착지 자체는 막지 않는다.

## 범위 제외

- `#472`의 `#461` 게이트 배선(값 산출 절차·기준만 이 spec, 배선은 `#472`)
- baseline/challenger 두 조건 확장·시간축 paired 계약 → **`#514`**
- production Feature Service·alias 변경(`#485` 명시 제약)
- 다중 origin(여러 cutoff) 확장 — 선행 spec §10에 후속으로 남아 있음
- 주 지표 교체·일반화(`#493` 소유)
