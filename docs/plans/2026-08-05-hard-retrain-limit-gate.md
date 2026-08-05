# 하드 리밋 승격 게이트 배선 구현 계획 (#472)

정본 계약: `docs/specs/2026-08-05-hard-retrain-limit-gate.md`
선행 계약: `docs/specs/2026-08-04-temporal-signal-promotion-integration.md`(#485) §4.1·§4.2

## 이 plan의 범위

spec §9의 6단계를 구현한다. **Task 1~3은 순수 함수 변경이고, Task 4~6은 호출부·문서다.**

| 범위 | 상태 |
| --- | --- |
| Task 1 — `evaluate()`에 하드 리밋 조건과 사유 코드 (§3.1·§4.1~§4.3) | 착수 가능 |
| Task 2 — guardrail 우회 금지 (§4.5) | Task 1 이후 |
| Task 3 — `GateDecision.policy_version` (§5) | Task 1 이후 |
| Task 4 — workflow 배선 (§8.2) | §6 확정(안 A) — §8.2만 남음 |
| Task 5 — Draft PR 본문에 승격 사유 표시 (§4.3) | Task 4 이후 |
| Task 6 — 스킬 문서에 하드 리밋 정책 명시 | Task 4 이후 |

### 왜 Task 1~3을 먼저 하는가

`evaluate()`는 **I/O가 없는 순수 함수**이고, 신규 인자 2개는 기본값 `None`이라 기존
호출부가 그대로 동작한다(spec §3.1). 즉 **값을 어디서 구할지 정해지기 전에 판정 규칙만
먼저 고정**할 수 있고, 그 규칙은 안 A/안 B 어느 쪽이든 같다.

반대로 Task 4를 먼저 하면 §6이 뒤집힐 때 배선을 다시 짜야 한다.

## 미해결 — 착수 전/중 확정

| 항목 | 언제 | 막는 것 |
| --- | --- | --- |
| §8.2 — 측정 결과 JSON을 CI가 어떻게 얻는지 | Task 4 착수 전 | Task 4 |
| §8.3 — `safety_margin_days` 확정 | 이 plan 범위 밖 | 실운영 값(코드는 안 막힘) |

**`#461` 소유자 승인은 기록됐다**(`pull/461#issuecomment-5186975872`). spec §6은 그
코멘트가 아니라 **§6에 적힌 사유**를 근거로 안 A를 확정했다 — 안 A의 오차를 키우는 운영
관행이 쌓일 시간 자체가 없었다는 것. 안 B는 spec §8.4로 백로그화했다.

## 범위 제외 (다루지 않는 것과 사유)

- **`hard_retrain_limit_days` 값 산출** — `#485` §4.1 소유. 이 plan은 값을 받기만 한다.
- **`evaluate_temporal_hold` 호출** — spec §3.2대로 hold 확인은 **호출부 책임**이다.
  게이트는 `degradation_eval`을 import하지 않는다(패키지 경계 + lightgbm).
- **Issue Form 필드 추가** — spec §3.3. 하드 리밋은 가설별 기준이 아니라 운영 정책이다.
- **모델 alias 이동·prod 배포** — 게이트 모듈의 비책임(모듈 docstring).

## 파일별 책임

| 파일 | 변경 |
| --- | --- |
| `autoresearch/experiments/promotion_gate.py` | Task 1~3 — 인자·판정·사유·정책 버전 |
| `tests/test_experiment_promotion_gate.py` | Task 1~3 |
| `.github/workflows/auto-research-promotion.yml` | Task 4~5 |
| `docs/guides/retraining-policy.md`(신규) | Task 6 |

**건드리지 않는 파일**: `src/pipeline/degradation_eval.py`,
`src/pipeline/experiment_evaluation.py`, `src/pipeline/promotion_evidence.py`,
그리고 **`src/tracking/registry.py`** — 안 A는 그 모듈을 **읽기만** 하므로 수정하지
않는다(안 B로 전환할 때만 손댄다, spec §8.4).

## 회귀 판정 기준 (baseline)

착수 시점에 clean `origin/main`에서 **코드 변경 0건 상태**로 전체 스위트를 1회 돌려
기록한다. 신규 테스트를 추가하기 전에 측정한다 — 순서를 지키지 않으면 숫자가 오염된다.

이 개발 환경은 Windows라 리눅스였으면 통과했을 실패가 상시로 있다. **절대 개수가 아니라
baseline 대비 증감으로 판정한다**: `failed`가 늘지 않았는지, `passed` 증가분이 신규
테스트 수와 맞는지.

참고값(`#514` 워크트리, `59ef767` 기준): **64 failed / 1876 passed / 23 skipped**.
워크트리가 다르므로 이 plan 착수 시 다시 잰다.

---

## Task 1 — 하드 리밋 조건과 사유 코드 (spec §3.1·§4.1~§4.3)

- [ ] `evaluate()`에 `hard_retrain_limit_days: int | None = None`,
      `days_since_last_promotion: int | None = None` 추가.
      **기본값 `None`이 필수다** — 현재 호출부(workflow 182-193행)는 이 인자를 넘기지
      않으므로, 기본값이 없으면 기존 워크플로우가 즉시 깨진다.
- [ ] 지표 조건을 **먼저** 평가한다. 통과하면 `criteria_met`(기한 도달 여부와 무관).
- [ ] 지표 미달일 때만 하드 리밋 조건을 본다:
      `둘 다 not None AND days_since_last_promotion >= hard_retrain_limit_days`
      → `GateDecision(True, "hard_retrain_limit_reached")`
- [ ] 둘 중 하나라도 `None`이면 **조건을 평가하지 않는다.** 관측되지 않은 것을
      "기한이 안 지났다"로도 "지났다"로도 바꾸지 않는다(`#485` §4.1과 같은 결).

**RED** — 항목별로 독립 실패해야 한다:
- 지표 통과 + 기한 도달 → `criteria_met`(**`hard_retrain_limit_reached`가 아니다**).
  이걸 뒤집으면 승격 이력에서 모델 품질이 과소평가된다.
- 지표 미달 + 기한 도달 → `passed=True`, `hard_retrain_limit_reached`
- 지표 미달 + 기한 미도달 → `passed=False`, `primary_metric_below_delta`
- 지표 미달 + `hard_retrain_limit_days=None` → `primary_metric_below_delta`
- 지표 미달 + `days_since_last_promotion=None` → `primary_metric_below_delta`
- 경계: `days_since_last_promotion == hard_retrain_limit_days` → **도달**(`>=`)
- `hard_retrain_limit_days=0` → 항상 도달(spec §7, `#485` 표의 빈칸 케이스)
- **인자를 안 넘기는 기존 호출 형태가 그대로 동작** (하위호환 가드)

검증: `uv run python -m pytest tests/test_experiment_promotion_gate.py -v`

## Task 2 — guardrail은 하드 리밋으로 우회되지 않는다 (spec §4.5)

- [ ] guardrail 위반이면 하드 리밋 도달 여부와 **무관하게** `passed=False`.
- [ ] 순서를 명시한다: 지표 통과 판정 → guardrail 검사 → 하드 리밋 판정.
      하드 리밋을 guardrail보다 먼저 보면 망가진 모델이 기한만으로 통과한다.

**RED**:
- 지표 미달 + 기한 도달 + guardrail 악화 → `passed=False`, `guardrail_regressed`
- 지표 미달 + 기한 도달 + guardrail 없음(`guardrail_name is None`) → 통과
- 지표 미달 + 기한 도달 + guardrail 값 누락 → `guardrail_metric_missing`

> 이 Task가 없으면 "하드 리밋의 취지는 성능이 정체돼도 교체하는 것이지 망가진 모델도
> 올리는 게 아니다"라는 규칙이 코드에 없다. 게이트가 사실상 무력해지는 경로다.

## Task 3 — `GateDecision.policy_version` (spec §5)

- [ ] `GateDecision`에 `policy_version: str` 추가. 값은 `gate-policy-v1`.
- [ ] `promotion_evidence.PROMOTION_POLICY_VERSION`("promotion-policy-v1")과 **다른
      축**임을 docstring에 남긴다 — 그쪽은 통계 판정 정책, 이쪽은 게이트 정책이다.
      이름이 비슷해 나중에 같은 것으로 오독될 수 있다.
- [ ] 호출부 workflow는 이 필드를 읽지 않으므로 하위호환이다. 다만 `GateDecision`은
      frozen dataclass이므로 **필드 순서/기본값**이 기존 위치 인자 생성을 깨지 않는지
      확인한다(현재 `GateDecision(False, "reason")` 형태로 생성됨).

**RED**: 모든 판정 경로가 `policy_version`을 싣는지, 기존 2-인자 생성이 깨지지 않는지.

## Task 4 — workflow 배선 (spec §8.2)

§6은 **안 A(버전 `creation_timestamp` 근사)로 확정**됐다. 남은 미해결은 §8.2(측정 결과
JSON 조달 경로)뿐이다.

- [ ] `days_since_last_promotion` 산출 — champion alias가 가리키는 버전의
      `creation_timestamp`(`src/tracking/registry.py:90`)에서 계산한다. **읽기만 하므로
      그 파일을 수정하지 않는다.**
- [ ] **근사라는 사실을 호출부에 남긴다**(spec §6.1). `policy_version`에는 넣지 않는다 —
      게이트는 값이 어떻게 구해졌는지 모르므로 알 수 없는 것을 단언하게 된다.
- [ ] `hard_retrain_limit_days` 조달 경로. 후보: 측정 결과 JSON(아티팩트/GCS) vs
      **정책 상수 고정 + 실측으로 주기 갱신**. 1차 형태는 후자가 현실적일 수 있다 —
      그 판단 근거를 문서에 남긴다.
- [ ] **hold 확인을 배선한다**(spec §3.2). hold가 있으면
      `hard_retrain_limit_days=None`으로 넘긴다. 이걸 빠뜨리면 오염된 곡선에서 나온
      숫자가 그대로 게이트에 들어간다 — **게이트는 스스로 구분할 수 없다.**

## Task 5 — Draft PR 본문에 승격 사유 표시 (spec §4.3)

- [ ] `hard_retrain_limit_reached`로 통과한 후보는 **지표상 개선이 없다.** PR 본문에
      그 사실이 드러나야 리뷰어가 "왜 이게 올라왔지"를 되묻지 않는다.
- [ ] workflow는 이미 `GATE_REASON`을 PR 생성 단계에 넘기고 있다(199-205행) — 그 값으로
      분기한다.

## Task 6 — 운영 정책 문서에 하드 리밋 정책 명시 → `docs/guides/retraining-policy.md`

- [ ] `#472` 완료 조건: "하드 리밋 값이 문서(스킬)와 코드(게이트 로직) 양쪽에 동일하게
      반영됩니다."
- [ ] 값 자체가 미확정이므로(§8.3), **값이 아니라 "어디서 오는 값인지"와 갱신 절차**를
      적는다. 값을 문서에 박으면 실측 갱신 때마다 두 곳이 어긋난다.

## 전체 검증

```bash
uv run python -m pytest -v
uv run --no-sync ruff check agent_orchestration autoresearch tests tools
```

각 Task 종료 시 **`tests/test_experiment_promotion_gate.py` 전체 회귀** + baseline
목록 밖 새 실패 여부를 확인한다. 있으면 멈추고 그 실패만 보고한다.

Task 4~6은 workflow YAML을 건드리므로 `git diff --check`와 `actionlint`(있으면)를
추가로 돌린다.
