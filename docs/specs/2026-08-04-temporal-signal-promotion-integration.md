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
- **`#472`의 게이트 배선** — hard retrain limit **값 산정**은 이 spec이, 그 값을 `#461`
  승격 게이트에 배선하는 것은 `#472`가 소유한다(`#472` 본문 2026-08-04 갱신 기준).
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
| **신규** | baseline/challenger의 as-of cutoff 동일성 검증 (§2) | 이 spec |
| **신규** | 최근 구간 vs 전체 구간 지표 분리 (§3) | 이 spec |
| **신규** | 유효 기간·hard retrain limit 산출 (§4) | 이 spec |
| **신규** | temporal signal → `#425` 스키마 연결 (§5) | 이 spec |
| **신규** | fail-closed `hold` 종료 조건 (§6) | 이 spec |

## 2. baseline/challenger 동일 조건 검증

`#485` 작업 범위: "baseline과 challenger가 동일한 as-of cutoff, feature snapshot,
split 규칙, paired seed를 사용하도록 검증한다."

기존 `verify_training_comparison`(#454)이 이미 **snapshot·split·seed 동일성**을
재검증한다 — 그 계약을 그대로 쓰고, temporal 고유의 항목만 더한다.

```text
기존(재사용): dataset_sha256 / split_manifest_sha256 / seed triplet 동일
신규(이 spec): cutoff_date 동일, window_days 동일, horizon_days 동일
```

`TemporalComparisonRequest`(가칭)는 두 `RollingOriginResult`(baseline/challenger)와
그 각각의 `training_snapshot_manifest`를 받아, 위 6개가 모두 같은지 확인한다.
하나라도 다르면 통계 추정 없이 `hold`(§6).

**핵심 근거**: cutoff가 다르면 두 곡선의 `elapsed_days=k`가 서로 다른 달력 날짜를
가리킨다 — 같은 x축 위에 놓을 수 없다. 이건 snapshot 해시 동일성으로는 안 잡힌다
(각자 다른 구간을 조립하면 해시도 당연히 다르므로, "다르다"는 사실만 알고 "왜 다른가"는
모른다). 그래서 별도 필드로 명시 비교한다.

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

## 4. 유효 기간과 hard retrain limit

`#485` 작업 범위: "성능과 무관하게 일정 기간이 지나면 재학습하도록 hard retrain limit과
다음 재학습 시각을 정의한다."

### 4.1 유효 기간 (valid_days)

```text
valid_days = degradation_point.elapsed_days        (탐지된 경우)
           = 관측된 마지막 유효일의 elapsed_days + 1  (미탐지 — "적어도 이만큼은 유효")
```

미탐지일 때 `None`이 아니라 하한값을 쓰는 이유: `#472`가 이 값을 게이트에 배선할 때
`None`이면 "유효 기간을 모른다"와 "아직 안 꺾였다"를 구분할 수 없다. 대신
`valid_days_is_lower_bound: bool`을 함께 실어 두 경우를 구분한다.

### 4.2 hard retrain limit

```text
hard_retrain_limit_days = min(valid_days, hard_retrain_ceiling_days)
next_retrain_at         = last_trained_at + hard_retrain_limit_days
```

- `hard_retrain_ceiling_days`는 **성능과 무관한 상한**이다 — 곡선이 안 꺾여도 이 날짜가
  오면 재학습한다(`#472` 본문의 "성능이 좋든 안 좋든 그냥 나가야 함").
- 값은 이 spec이 확정하지 않는다. `#485` 참고 사항이 "재학습 리밋 값은 구현 전 spec에서
  확정한다"고 요구하지만, **데이터 가용성 실측 없이 정하면 근거 없는 상수가 된다**
  (선행 spec §3의 `A-D` 공식, `A`는 실행 시점마다 달라짐). 이 spec은 대신 **결정 절차**를
  확정한다:
  ```text
  hard_retrain_ceiling_days = min(
      관측 가능한 최대 horizon (= A - D - W, 선행 spec §3),
      운영이 감당 가능한 재학습 주기(팀 결정),
  )
  ```
  구체적 수치는 plan 단계에서 실측(선행 spec plan Task 1) 후 기록한다.

### 4.3 열화 시점 해석의 한계 (반드시 함께 읽어야 함)

선행 spec §10에 기록된 대로, `baseline_val_roc_auc`(랜덤 val split)와 `per_day`(forward
held-out)는 산출 경로가 다르고 이 저장소 실측에서 **약 4%p 오프셋**이 있었다
(`experiments/2026-07-31_training-window-length/notes.md`).

따라서 `valid_days`가 0~1로 나오면 그것이 실제 열화인지 이 오프셋인지 구분되지 않는다.
이 spec은 그 구분을 자동화하지 않고, 대신:

- `valid_days <= 1`이면 `robustness_note`에 이 오프셋 가능성을 명시한다(§5).
- `confidence`를 `low`로 낮춘다(§5).

`per_day[0]`을 baseline으로 쓰는 대안은 선행 spec §2.4 계약 변경이라 **이 spec에서도
채택하지 않는다** — 바꾸려면 선행 spec을 개정해야 하고, 그러면 이미 착지한 판정 로직과
과거 측정 결과의 비교 가능성이 깨진다.

## 5. `#425` 다중 신호 판정 연결

`#425` 완료 조건이 요구하는 필드를 temporal signal에 맞춰 채운다.

```text
confidence: high | medium | low
robustness_note: str | None
direction_vs_offline_metric: agree | disagree | not_applicable
```

### 5.1 confidence 산출 규칙(결정론적)

```text
low     ⟸ 유효 평가일 < 2  OR  valid_days <= 1  OR  recent_roc_auc_mean is None
          OR  §2 동일 조건 검증 실패(이 경우 애초에 hold, §6)
medium  ⟸ 유효 평가일 < recent_window_days + 2  OR  degradation_point 미탐지
high    ⟸ 그 외
```

`#425` 배경(round_002)의 교훈을 그대로 적용한다 — "격차가 임계값의 2.6배였음에도 결과가
신뢰할 수 없는 수준"이었던 이유는 표본(시드) 민감도였다. temporal signal의 대응물은
**유효 평가일 수**다. 그래서 delta 크기가 아니라 관측 밀도로 confidence를 정한다.

### 5.2 direction_vs_offline_metric

```text
agree     ⟸ (offline 주지표 delta > 0) == (challenger.recent_roc_auc_mean
             > baseline.recent_roc_auc_mean)
disagree  ⟸ 부호가 반대
not_applicable ⟸ 어느 한쪽이 None
```

`disagree`는 **실패가 아니다**(`#425` 완료 조건: "'기각'·'낮은 신뢰도'가 실패가 아닌
정상 종료 경로"). `robustness_note`에 두 신호가 갈린 사실을 남기고 `confidence`를
`medium` 이하로 낮춘 뒤 정상 종료한다.

### 5.3 스키마 부착 지점 — **미해결, §7 참고**

`ExperimentEvaluation`(`experiment_evaluation.py:137-155`)에 필드를 더할지, temporal
전용 별도 레코드로 둘지는 `#493` 상태 확인 후 결정한다.

## 6. fail-closed `hold` 종료 조건

`#485` 작업 범위: "데이터 부족, 미래 구간 누락, feature snapshot 불일치, 시간 순서
위반은 통계 추정 없이 `hold`로 종료한다."

| 조건 | 탐지 방법 | reason code(가칭) |
| --- | --- | --- |
| 데이터 부족 | 유효 평가일 < 2 | `temporal_insufficient_valid_points` |
| 미래 구간 누락 | `per_day` 길이 < `horizon_days` | `temporal_horizon_incomplete` |
| feature snapshot 불일치 | §2의 6개 필드 중 하나라도 상이 | `temporal_condition_mismatch` |
| 시간 순서 위반 | `training_window` 종료일 >= `cutoff_date` | `temporal_ordering_violated` |
| temporal evidence 부재 | `RollingOriginResult` 자체가 없음 | `temporal_evidence_missing` |

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

이 항목이 풀리기 전까지 **§5(스키마 연결)는 구현하지 않는다.** §2·§3·§4·§6은
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

staleness 없이도 §2~§6은 성립한다(staleness는 진단 보조 지표이지 판정 입력이 아니다).

### 7.3 실측이 필요한 값

| 값 | 결정 절차 | 현재 상태 |
| --- | --- | --- |
| `hard_retrain_ceiling_days` | §4.2 공식 + 팀 결정 | 미확정(데이터 가용성 실측 선행) |
| `recent_window_days` | §3, 기본 3 | 실측 후 재조정 |
| `min_auc_drop` | 선행 spec plan Task 7-A | 미확정(GCP 자격증명 필요) |

셋 다 GCP 접근이 가능한 환경에서 선행 spec plan Task 1·7-A를 먼저 실행해야 한다.

## 8. 구현 순서 제안

```text
1단계 (선행 가능, experiment_evaluation.py 미접촉)
  §2 동일 조건 검증 → §3 구간 분리 → §4 유효 기간·hard retrain limit → §6 hold 조건
  산출물: TemporalEvaluation 레코드 + Plotly 확장(baseline/challenger 2개 곡선)

2단계 (§7.1 확인 후)
  §5 #425 스키마 연결

별도 (§7.2 요청 후)
  video staleness 활성화
```

## 범위 제외

- `#472`의 `#461` 게이트 배선(값 산정만 이 spec, 배선은 `#472`)
- production Feature Service·alias 변경(`#485` 명시 제약)
- 다중 origin(여러 cutoff) 확장 — 선행 spec §10에 후속으로 남아 있음
- 주 지표 교체·일반화(`#493` 소유)
