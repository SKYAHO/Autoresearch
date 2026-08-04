# temporal signal 승격 판정 연결 구현 계획 (#485 잔여 범위)

정본 계약: `docs/specs/2026-08-04-temporal-signal-promotion-integration.md`
선행 계약: `docs/specs/2026-08-03-model-degradation-rolling-origin-evaluation.md`(측정 하네스, #510 착지)

## 이 plan의 범위

spec §8의 **1~4단계**를 구현한다. 4단계는 작성 시점에 `#493` 블로커로 착수 금지였으나
2026-08-04에 해제됐다(Task 4 참고).

| 범위 | 상태 |
| --- | --- |
| Task 1 — §4.3 baseline 재정의 | 착수 가능 |
| Task 2 — §3 구간 분리 + §4.1·§4.2 hard retrain limit | 착수 가능 |
| Task 3 — §6 fail-closed `hold` 조건 4개 | 착수 가능 |
| Task 4 — §5 `#425` 스키마 연결 | **블로커 해제됨(2026-08-04)** — 착수 완료 |

## 범위 제외 (이 plan이 다루지 않는 것과 사유)

- **`#514` — baseline/challenger 두 조건 확장, 시간축 paired 계약**(spec §2):
  `RollingOriginResult`에 조건 식별자도 두 실행을 묶는 기록도 없어 하네스 구조 변경이
  필요하다. 검증 난이도가 이 plan의 Task들과 달라 별도 이슈·plan으로 분리했다.
- **video staleness 활성화**(spec §7.2): `PASSTHROUGH_COLUMNS`(`model_contract.py`,
  `#506`) 확장이 필요하고 feature contract SSOT 변경이라 bbungjun님 확인 대기 중이다.
  staleness는 진단 보조 지표이지 판정 입력이 아니므로 이 plan의 Task들은 이것 없이
  성립한다.
- **`safety_margin_days`·`recent_window_days`·`min_auc_drop` 값 확정**(spec §7.3):
  GCP 자격증명이 있는 환경에서 선행 spec plan Task 1·7-A를 먼저 실행해야 한다. 코드
  착지는 막지 않는다 — 세 값 모두 호출부가 넘기는 인자이지 하드코딩 상수가 아니다.

## 파일별 책임

| 파일 | 변경 |
| --- | --- |
| `src/pipeline/degradation_eval.py` | Task 1~3 전부 — 필드 추가, 함수 신설, hold 조건. Task 4 — `temporal_signal_inputs` |
| `src/pipeline/experiment_evaluation.py` | Task 4 — `TemporalSignal` 및 판정 산출물 필드(§7.1 해제 후) |
| `tests/test_experiment_evaluation_temporal_signal.py` | Task 4 — 신규 |
| `docs/specs/2026-08-03-model-degradation-rolling-origin-evaluation.md` | §2.4 부분 supersede 블록(Task 1에서 이미 커밋됨, 확인만) |
| `tests/test_degradation_eval_detection.py` | Task 1 — baseline 재정의 |
| `tests/test_degradation_eval.py` | Task 1~3 — 오케스트레이션 통합 |
| `tests/test_degradation_eval_hold.py` | Task 3 — 신규 |
| `src/cli.py` | Task 2 — `derive_hard_retrain_limit` 노출 여부는 Task 2에서 판단 |

**건드리지 않는 파일**: `promotion_gate.py`,
`paired_experiment.py`. `model_contract.py`, `build_training_dataset.py`
(spec §7.2, bbungjun님 영역).

## Task 1 — §4.3 baseline 재정의

다른 Task가 전부 이 곡선 위에서 계산되므로 **선행**이다.

- [ ] `RollingOriginResult`에 필드 2개 추가(기존 필드 유지, 하위호환):
      `forward_baseline_roc_auc: float | None`, `forward_baseline_source: int | None`
- [ ] `run_rolling_origin`이 `per_day` 중 **첫 `valid` 관측치**의 `roc_auc`를
      `forward_baseline_roc_auc`로, 그 `elapsed_days`를 `forward_baseline_source`로 채운다.
      유효 관측치가 0개면 둘 다 `None`.
- [ ] `detect_degradation_point`의 `baseline` 인자에 `forward_baseline_roc_auc`를 넘긴다
      (현재 `baseline_val_roc_auc`를 넘기는 `degradation_eval.py:642` 호출부 교체).
- [ ] `forward_baseline_roc_auc`가 `None`이면 `detect_degradation_point`를 부르지 않고
      `insufficient_valid_points`로 끝낸다 — 유효 관측치 0개면 애초에 판정 불가다.
- [ ] `baseline_val_roc_auc`는 결과 필드로 유지하되 판정 로직에는 쓰지 않는다.
      기존 fail-fast(`math.isfinite` 검증)는 그대로 둔다 — 학습이 실제로 수행됐는지의
      신호로는 여전히 유효하다.
      **`forward_baseline_roc_auc`에는 같은 검증을 넣지 않는다**(이유 두 가지):
      (a) 시점 — 이 값은 `per_day`에서 파생돼 루프가 끝난 뒤에만 존재하므로, 비싼 평가
      **전에** 멈추는 현재 fail-fast 자리에 놓을 수 없다.
      (b) 스키마 — `PerDayResult`가 `_ResultModel`(`allow_inf_nan=False`)이라 `roc_auc`에
      NaN/inf가 들어가면 `PerDayResult` 생성 시점에 이미 실패한다. 따라서
      `forward_baseline_roc_auc`는 **유한값이거나 `None`**임이 스키마로 보장되고,
      `None` 케이스는 아래 `insufficient_valid_points` 항목이 처리한다.
- [ ] 선행 spec §2.4의 부분 supersede 블록이 실제로 들어갔는지 확인한다(커밋 `0f1c3db`).

**테스트(TDD, RED 먼저 확인)**
- `forward_baseline_roc_auc`가 첫 valid 관측치 값과 같다(중간에 invalid가 끼어도).
- `forward_baseline_source`가 그 관측치의 `elapsed_days`다.
- 유효 관측치 0개면 둘 다 `None`이고 `degradation_point.reason == "insufficient_valid_points"`.
- `detect_degradation_point`가 `baseline_val_roc_auc`가 아니라
  `forward_baseline_roc_auc` 기준으로 판정한다 — 두 값이 크게 다른 시나리오로 고정한다
  (예: `baseline_val_roc_auc=0.80`, `per_day[0]=0.76`이면 0.76 기준으로 threshold 계산).
- 기존 테스트 48개가 그대로 통과한다(하위호환).

검증: `uv run python -m pytest tests/test_degradation_eval.py tests/test_degradation_eval_detection.py -v`

## Task 2 — §3 구간 분리 + §4.1·§4.2 hard retrain limit

- [ ] `RollingOriginResult`에 필드 추가: `overall_roc_auc_mean: float | None`,
      `recent_roc_auc_mean: float | None`, `recent_window_days: int`
- [ ] `overall_roc_auc_mean` = `status == valid`인 날의 `roc_auc` **날 동등 가중** 평균.
      `recent_roc_auc_mean` = 그중 최근 `recent_window_days`개 유효일의 평균.
- [ ] 유효일이 `recent_window_days` 미만이면 `recent_roc_auc_mean = None`이고 사유를
      남긴다 — 적은 표본으로 평균을 만들어 "최근 성능"이라고 부르지 않는다.
- [ ] `recent_window_days`는 `run_rolling_origin`의 키워드 인자(기본 3)로 받는다.
      하드코딩하지 않는다(spec §7.3, 실측 후 재조정 대상).
- [ ] `derive_hard_retrain_limit(result, *, safety_margin_days) -> HardRetrainLimit`을
      **별도 함수**로 만든다. `RollingOriginResult`에 얹지 않는다(측정/정책 분리).
- [ ] `HardRetrainLimit`(pydantic, `_ResultModel`): `limit_days: int | None`,
      `reason: str | None`. `degradation_point`가 없으면 `limit_days=None` +
      `no_degradation_observed_within_horizon` 또는 `insufficient_valid_points`를 그대로 전달.
      **관측되지 않은 것을 "안전"으로 바꾸지 않는다.**
- [ ] `next_retrain_at` 계산은 이 함수가 하지 않는다 — `last_trained_at`은 이 모듈이
      모르는 값이고, `#472`가 게이트에서 조합한다.

**테스트**
- 유효일 5개 중 최근 3개 평균이 `recent_roc_auc_mean`과 같다.
- 유효일이 `recent_window_days` 미만이면 `recent_roc_auc_mean is None`.
- `overall`과 `recent`가 다른 값이 나오는 시나리오(뒤로 갈수록 하락)로 분리를 고정한다.
- `derive_hard_retrain_limit`: 탐지됨 → `elapsed_days - safety_margin_days`.
- `derive_hard_retrain_limit`: `no_degradation_detected` → `limit_days is None`,
  `reason == "no_degradation_observed_within_horizon"`.
- `derive_hard_retrain_limit`: `insufficient_valid_points` → `limit_days is None`,
  사유가 그대로 전달된다.

검증: `uv run python -m pytest tests/test_degradation_eval.py -v`

## Task 3 — §6 fail-closed `hold` 조건 4개

spec §6 표에서 **`condition_mismatch`를 제외한 4개**만 구현한다(그건 두 조건 비교가
전제라 `#514` 소관).

- [ ] `TemporalHoldReason`(str Enum): `temporal_insufficient_valid_points`,
      `temporal_horizon_incomplete`, `temporal_ordering_violated`,
      `temporal_evidence_missing`
- [ ] `evaluate_temporal_hold(result: RollingOriginResult | None) -> TemporalHoldReason | None`
      — `None` 반환이 "hold 아님"이다.
- [ ] 데이터 부족: 유효 평가일 < 2 → `temporal_insufficient_valid_points`
- [ ] 미래 구간 누락: `len(per_day) < horizon_days` → `temporal_horizon_incomplete`
- [ ] 시간 순서 위반: `training_snapshot_manifest.events_end_date >= cutoff_date` →
      `temporal_ordering_violated`. 선행 spec §2.1이 `events_end_date = cutoff - 1일`로
      고정하므로 정상 경로에서는 발생하지 않는다 — 호출부가 결과를 손으로 만들어 넣는
      경우를 막는 **심층 방어**다(`#478`의 "producer가 보낸 숫자를 믿지 않는다"와 같은 결).
- [ ] evidence 부재: `result is None` → `temporal_evidence_missing`
- [ ] 판정 순서를 고정한다: evidence 부재 → 시간 순서 위반 → 미래 구간 누락 → 데이터 부족.
      더 근본적인 결손을 먼저 보고한다(선행 spec §2.3의 `classify_evaluation_day` 우선순위와
      같은 원칙).

**테스트** — 신규 파일 `tests/test_degradation_eval_hold.py`
- 4개 조건 각각이 해당 reason을 낸다.
- 정상 결과는 `None`(hold 아님)을 낸다.
- 두 조건이 동시 성립하면 위 순서대로 더 근본적인 것이 나온다.

검증: `uv run python -m pytest tests/test_degradation_eval_hold.py -v`

## Task 4 — §5 `#425` 스키마 연결 ✅ 블로커 해제됨(2026-08-04)

- 블로커였던 것: `#493`이 CLOSED(COMPLETED)인데 `experiment_evaluation.py` 코드는
  `f8bf611`(#478) 이후 변경이 없었다. `#500`으로 머지된 계획이 그 파일의 재구조화를
  선언했으므로, 필드를 더하면 충돌할 수 있었다.
- 해제 근거: `#493` assignee의 이슈 코멘트
  (`issues/493#issuecomment-5175803521`) — "판정 엔진 재구조화는 계획만 남기고
  종료, 후속은 `#485`/`#514`에서 진행. `experiment_evaluation.py` 직접 확장 승인".
- **구두 확인은 근거로 쓰지 않았다** — 확인 자체는 Slack에서 먼저 이뤄졌으나,
  검증 불가능한 근거를 문서·코드에 고정하지 않는다는 spec §7.1의 결정에 따라
  assignee에게 이슈 코멘트를 요청한 뒤 그 링크를 근거로 삼았다. 이슈 상태(CLOSED)나
  assignee 필드 같은 **결정과 무관한 메타데이터는 근거로 쓰지 않는다** — 그것들은
  "누가 맡았나"만 말할 뿐 "재구조화가 어떻게 됐나"에 답하지 않는다.
- 한 일: spec §5.3을 **안 A로 확정** → `EvaluationConfidence`/`SignalDirection`/
  `TemporalSignal` + `summarize_temporal_signal`(§5.1·§5.2 산출 규칙),
  `ExperimentEvaluation.temporal_signal`(기본 `None`),
  `degradation_eval.temporal_signal_inputs`.

이 Task로 이 plan의 Task 1~4는 모두 끝난다. **`#485`에 남는 것**은 이 plan의 범위
제외 항목뿐이다 — `#514`(두 조건 확장), spec §7.2(video staleness, bbungjun님 확인
대기), spec §7.3(`safety_margin_days`/`recent_window_days`/`min_auc_drop` 값 확정,
GCP 실측 선행).

## 전체 검증

```bash
uv run python -m pytest -v
uv run --no-sync ruff check agent_orchestration autoresearch tests tools
docker build -f Dockerfile.app -t autoresearch:ci .
```

### 기존 실패 baseline (회귀 판정 기준)

GCP·Docker·K8s 툴체인이 없는 이 개발 환경에서는 **이 plan과 무관하게 실패하는 테스트**가
있다. **새로 실패하는 테스트가 아래 목록 밖에 있을 때만 회귀다.**

> **확인 시점: 2026-08-04, clean main `b4553f1`** (이 브랜치의 코드 변경 0건 상태에서
> `uv run python -m pytest -q --tb=no -rf` 실행). **64 failed / 1754 passed / 17 skipped.**

| 파일 | 건수 |
| --- | --- |
| `tests/test_action_logs_daily.py` | 18 |
| `tests/test_pr_report_archive_rail.py` | 13 |
| `tests/test_rerank_loadtest_fixture.py` | 11 |
| `tests/test_pr_report_archive_card_isolation.py` | 6 |
| `tests/test_agent_orchestration_bootstrap.py` | 6 |
| `tests/test_pr_report_archive_merge.py` | 3 |
| `tests/test_pr_report_archive_search.py` | 2 |
| `tests/test_pr_report_archive_category.py` | 2 |
| `tests/test_agent_orchestration.py` | 2 |
| `tests/test_cli.py` (`test_promote_model_structured_unexpected_error_emits_safe_stack`) | 1 |

**이 목록은 main이 움직이면 낡는다.** 다음 Task 착수 전에 다시 찍고 시점·SHA를 갱신한다.

> 측정 시 주의: 이 값을 잴 때는 작업 중인 변경을 stash해야 한다. 실제로 이 기준을
> 처음 잴 때 백그라운드 실행 중에 RED 테스트를 추가해 68건으로 오염된 적이 있다
> (64 baseline + RED 4건). 총계(failed+passed+skipped)가 예상과 다르면 오염을 의심한다.
- feast 계열 변경이 없으므로 `pytest (feast group)` 별도 실행은 필요 없다.

## 체크포인트

각 Task 종료 시 멈추고 보고한다. Task 1은 다른 Task의 전제이므로 특히 리뷰 후
다음으로 넘어간다.

## 미완 선행 작업 (코드 착지를 막지는 않음)

선행 spec plan(`docs/plans/2026-08-03-model-degradation-rolling-origin-evaluation.md`)의
Task 1(BigQuery 실측 — `A`/`D` 확정)과 Task 7-A(`min_auc_drop` calibration)는 여전히
미완이다. GCP 자격증명이 있는 환경에서 선행돼야 이 plan의 결과를 **실측으로** 확인할 수
있다. 다만 세 값(`safety_margin_days`/`recent_window_days`/`min_auc_drop`) 모두 호출부가
넘기는 인자이므로 코드 착지 자체는 막히지 않는다.
