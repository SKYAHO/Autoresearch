# 시간축 paired 평가 구현 계획 (#514)

정본 계약: `docs/specs/2026-08-05-temporal-paired-evaluation.md`
선행 계약: `docs/specs/2026-08-04-temporal-signal-promotion-integration.md`(#485, §2가 이관),
`docs/specs/2026-08-03-model-degradation-rolling-origin-evaluation.md`(#471/#510, 측정 하네스)

## 이 plan의 범위

spec §9의 6단계를 구현한다. Task 1~5는 코드, Task 6은 실측이다.

| 범위 | 상태 |
| --- | --- |
| Task 1 — `training_run_id`·seed 기록 (§2.4) | 착수 가능 |
| Task 2 — `condition_match` 검증과 `condition_mismatch` hold (§3) | Task 1 이후 |
| Task 3 — `TemporalPairedResult` 계약과 hold 합산 (§4) | Task 2 이후 |
| Task 4 — `TemporalDelta` 산출 (§5·§6) | Task 3 이후 |
| Task 5 — Plotly 두 조건 확장 (§7) | Task 3 이후(Task 4와 병행 가능) |
| Task 6 — 2조건 실측 1회 (§8.1 실험대) | Task 1~5 전부 이후 |

**Task 2가 §8.3 확인에 걸린다** — `verify_training_comparison`을 등록되지 않은 측정
run에 적용할 수 있는지에 따라 검증 재료가 달라진다. Task 1에서 그 확인을 먼저 끝낸다
(아래 Task 1의 마지막 항목). 검증 **항목**은 어느 쪽이든 같으므로 Task 2의 테스트는
재료와 무관하게 먼저 쓸 수 있다.

## 범위 제외 (다루지 않는 것과 사유)

- **프로덕션 배선** — `ExperimentEvaluation.temporal_signal`을 채우고 발행 payload에
  싣는 것은 `#472` 소유다(`#485` spec §5.3, PR #527 리뷰 Medium#2). 이 plan은 그 배선의
  **입력**(`temporal_delta`)을 만드는 데까지다.
- **`evaluation_id` 해시 결정 변경** — spec §6.1. 이 plan은 `temporal_delta`를 만들 뿐
  판정 입력으로 승격시키지 않으므로 `#485` §5.3의 결정이 유지된다.
- **`#478` 30 시드 경로** — spec §1.2. `experiment_evaluation.py`·`paired_experiment.py`·
  `promotion_gate.py`를 수정하지 않는다.
- **다중 origin(여러 cutoff)** — 선행 spec §10.
- **`recent_window_days` 노이즈 보정** — spec §6 "알려진 한계". 판정 뒤집힘이 실측되면
  후속 이슈.

## 파일별 책임

| 파일 | 변경 |
| --- | --- |
| `src/pipeline/degradation_eval.py` | Task 1~4 — 필드 추가, 검증·계약·delta 함수 신설 |
| `scripts/bench/degradation_curve_plot.py` | Task 5 — 두 조건 확장 |
| `src/cli.py` | Task 3 — 두 조건 실행 진입점 노출 여부는 Task 3에서 판단 |
| `tests/test_degradation_eval_paired.py` | Task 2~4 — 신규 |
| `tests/test_degradation_eval.py` | Task 1 — 기존 결과 하위호환 |
| `tests/test_degradation_curve_plot.py` | Task 5 |

**건드리지 않는 파일**: `experiment_evaluation.py`, `paired_experiment.py`,
`promotion_gate.py`, `model_contract.py`, `build_training_dataset.py`.

## 회귀 판정 기준 (baseline)

이 plan 착수 시점에 clean `origin/main`에서 전체 스위트를 1회 돌려 기록한다. **코드 변경
0건 상태**여야 하므로 신규 테스트를 추가하기 전에 측정한다(`#485` plan에서 배운 것 —
백그라운드 pytest가 새로 추가한 RED 테스트를 수집해 숫자가 오염됐다).

직전 참고값(`#485` plan, clean main `b4553f1`): **64 failed / 1754 passed / 17 skipped**.
`#520`·`#527` 착지 후 `59ef767` 기준 실측: **64 failed / 1813 passed / 17 skipped**
(파일별 분포 동일). **새로 실패하는 테스트가 그 목록 밖에 있을 때만 회귀다.**

---

## Task 1 — `training_run_id`·seed 기록 (spec §2.4)

`run_rolling_origin`이 `train.main`의 `TrainingOutcome.run_id`를 버리고 있어
(`degradation_eval.py`에 `run_id` 0건) `verify_training_comparison`을 부를 수 없다.

- [ ] `RollingOriginResult`에 `training_run_id: str | None = None`을 더한다.
      **기본값 `None`이 필수다** — `#510`/`#520`이 이미 만든 결과 JSON을 읽는 경로
      (`degradation_curve_plot.py`, `#472`가 쓸 소비 경로)가 깨지면 안 된다.
- [ ] `seed: int | None = None`을 더한다. spec §3.1의 "동일 시드 고정"을 검증하려면
      결과에 시드가 남아야 하는데 현재 없다.
- [ ] `run_rolling_origin`에서 `outcome.run_id`와 인자로 받은 `seed`를 결과에 채운다.
- [ ] **§8.3 확인**: `verify_training_comparison`을 `defer_registration=True`로 학습한
      run에 적용할 수 있는지 코드로 확인한다. 확인 결과를 spec §8.3에 기록하고,
      불가하면 Task 2의 검증 재료를 `training_snapshot_manifest` 대조로 확정한다.

**RED**: 기존 결과 JSON(필드 없음)을 `model_validate`로 읽어 성공하는지, 새 실행 결과에
`training_run_id`/`seed`가 채워지는지. 전자는 하위호환 가드다.

검증: `uv run python -m pytest tests/test_degradation_eval.py -v`

## Task 2 — `condition_match` 검증과 `condition_mismatch` hold (spec §3)

- [ ] `TemporalHoldReason`에 `CONDITION_MISMATCH = "condition_mismatch"`를 더한다.
- [ ] spec §3.1의 8개 항목을 비교하는 `ConditionMatch` 결과 모델과 판정 함수를 만든다.
      **항목별로 무엇이 어긋났는지 남긴다** — "다르다"만 남기면 원인 추적이 끊긴다
      (`#485` spec §4.2의 "사유를 남겨 조용히 뭉개지 않는다"와 같은 결).
- [ ] 평가일 집합 비교는 `per_day[].date`의 **집합**으로 한다. 순서는
      `_ordered_by_elapsed`가 이미 정규화한다.

**RED**: 8개 항목 각각을 하나씩 어긋뜨린 결과 쌍이 그 항목을 사유로 내는지. 항목마다
**독립적으로** 실패해야 한다 — 한 항목만 검사하고 나머지를 통과시키는 구현을 잡는다.

검증: `uv run python -m pytest tests/test_degradation_eval_paired.py -v`(신규)

## Task 3 — `TemporalPairedResult` 계약과 hold 합산 (spec §4)

- [ ] `contract_version: Literal["temporal-paired-evaluation-v1"]`을 가진 결과 모델.
      `baseline`/`challenger`를 **`RollingOriginResult` 그대로** 담는다(요약 금지).
- [ ] hold 합산: 두 조건 각각의 `evaluate_temporal_hold` + Task 2의
      `condition_mismatch`. **하나라도 hold면 전체 hold**이며, 사유에 **어느 조건인지**를
      함께 남긴다.
- [ ] `hold_reason`이 있으면 `delta`는 `None`이다(spec §3.2 fail-closed).
- [ ] CLI 노출 여부를 판단한다. `measure-degradation`은 단일 조건 진입점이므로 별도
      명령이 필요한지, 아니면 이 단계에서는 라이브러리 함수로 두고 Task 6에서 스크립트로
      부를지 결정하고 사유를 남긴다.

**RED**: 한쪽만 hold인 쌍이 전체 hold가 되는지, 그때 `delta`가 `None`인지, 사유에 조건
이름이 들어가는지.

## Task 4 — `TemporalDelta` 산출 (spec §5·§6)

- [ ] `forward_baseline_delta` / `per_day_delta` / `overall_delta` / `recent_delta` /
      `degradation_point_delta`.
- [ ] **baseline은 조건별로 유지한다**(spec §5) — 공유 baseline으로 재계산하지 않는다.
- [ ] `recent_delta`는 **양쪽 다** 유효일이 `recent_window_days` 이상일 때만 낸다.
      한쪽이라도 부족하면 `None`.
- [ ] `degradation_point_delta`는 **양쪽 다** 탐지됐을 때만 낸다. 한쪽이 미탐지면
      `None`이며 "차이 없음(0)"으로 바꾸지 않는다 — 관측되지 않은 것을 값으로 만들지
      않는다(`#485` spec §4.1과 같은 결).
- [ ] `#425`에 넘기는 값은 `recent_delta`임을 docstring에 고정한다(spec §6).

**RED**: 각 delta의 부호와 `None` 조건. 특히 한쪽 미탐지 시 `degradation_point_delta`가
`0`이 아니라 `None`인지 — 이 실수는 조용히 "동등"으로 읽힌다.

## Task 5 — Plotly 두 조건 확장 (spec §7)

- [ ] 두 곡선을 같은 `elapsed_days` 축에 겹쳐 그린다.
- [ ] delta는 **하단 subplot**으로 분리한다. 같은 축이면 ROC-AUC(0.5~0.7)와
      delta(±0.05)의 스케일 차이로 delta가 평평해진다.
- [ ] 조건별 threshold선과 `degradation_point`를 조건 색으로 구분한다.
- [ ] `hold`가 있으면 **곡선을 그리되 경고 배너**를 넣는다.
- [ ] **기존 단일 조건 결과 JSON도 계속 그려져야 한다.** `#510`/`#520` 산출물이 이미
      있고, 이번 실측 결과(`experiments/.../rolling_origin_result.json`)로 확인한다.

**RED**: 단일 조건 JSON과 두 조건 JSON 둘 다 입력했을 때 각각 기대한 trace 수가 나오는지.

## Task 6 — 2조건 실측 1회 (spec §8.1)

실험대는 `#485` 실측과 같은 구간을 쓴다 — 결손일 분포상 07-27~08-03이 유일한 연속
8일이고(`experiments/2026-08-03_model-degradation-rolling-origin/notes.md`), 두 조건이
같은 평가일 집합을 써야 spec §3의 검증이 성립한다.

```text
cutoff 2026-07-27, W=15, H=8, seed 42 (양 조건 동일)
min_auc_drop 0.009330 (#485 Task 7-A calibration 산출)
```

- [ ] baseline/challenger 두 조건을 정한다. **무엇을 challenger로 둘지는 이 plan이
      정하지 않는다** — 실행 시점에 유효한 실험 가설(`--extra-features` 등)을 골라
      기록한다.
- [ ] 2조건 실행 1회. 학습 2번 + 평가일 조립 16번이므로 `#485` 실측의 약 2배 시간을
      예상한다.
- [ ] `experiments/2026-08-05_temporal-paired-evaluation/notes.md`에 5필드 포맷으로
      기록한다. Before는 `#485` 실측(단일 조건)을 인용한다.

**실행 환경**: 로컬 Windows에서는 Docker가 필수다. ADC 경로 가드와 Feast registry
경로 파싱이 Windows에서 실패한다 — 레시피는 `#485` 실측 notes.md의 "재현 방법"에 있다.
`--experiment`를 빠뜨리면 학습 데이터셋 조립을 마친 뒤 `MLFLOW_TRACKING_URI` 가드로
죽으므로 먼저 넣는다.

## 전체 검증

```bash
uv run python -m pytest -v
uv run --no-sync ruff check agent_orchestration autoresearch tests tools
```

각 Task 종료 시 **degradation 스위트 전체 회귀** + baseline 목록 밖 새 실패 여부를
확인한다. 있으면 멈추고 그 실패만 보고한다.

## 미해결 (착수 전/중 확정)

| 항목 | 언제 | 막는 것 |
| --- | --- | --- |
| §8.2 — `#478` 30 시드 경로 미변경 해석 확인 | spec Accepted 전 | spec 상태(코드는 막지 않음) |
| §8.3 — `verify_training_comparison` 적용 가능성 | Task 1 중 | Task 2의 검증 **재료**(항목은 불변) |
| Task 6의 challenger 조건 선정 | Task 6 착수 시 | Task 6만 |
