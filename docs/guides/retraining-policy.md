# 재학습·강제 교체(하드 리밋) 정책

> 관련 이슈: #472 · 값 산출: #485 · 게이트: #461
> 계약 정본: `docs/specs/2026-08-05-hard-retrain-limit-gate.md`

## 목적

승격 게이트는 원래 **지표만** 본다. 그래서 모델이 조용히 늙어도 지표가 기준을 넘지
못하면 영원히 교체되지 않는다. 이 문서는 **"성능과 무관하게 일정 기간이 지나면
교체한다"**는 하드 리밋 정책의 운영 규칙을 적는다.

값 자체를 여기 박지 않는다 — 값은 열화 측정으로 바뀌므로, 문서에 숫자를 쓰면 실측
갱신 때마다 코드와 문서가 어긋난다. **값이 어디 있고, 누가 언제 갱신하는가**만 적는다.

## 값은 어디 있는가

```text
.github/workflows/auto-research-promotion.yml
  jobs.create-promotion-pr.env.HARD_RETRAIN_LIMIT_DAYS
```

**비어 있으면 하드 리밋 조건을 평가하지 않는다.** 이때 게이트는 지표만 보므로 정책
도입 전과 완전히 동일하게 동작한다. 값을 넣는 순간 정책이 켜진다.

2026-08-05 현재 **비어 있다.** `#485` 실측이 단일 origin 관측 하나뿐이고
`safety_margin_days`가 미확정이라 리밋도 잠정이기 때문이다. 확정 전에 숫자를 박으면
근거 없는 상수가 승격을 만들어낸다.

## 누가, 언제 갱신하는가

| | |
| --- | --- |
| **주체** | 열화 측정(`#485` 계열)을 소유한 사람 |
| **시점** | 다중 origin 관측이 새로 쌓일 때마다 |
| **절차** | 아래 3단계 |

1. `measure-degradation`으로 rolling-origin 곡선을 얻는다
   (`docs/specs/2026-08-03-model-degradation-rolling-origin-evaluation.md`).
2. `evaluate_temporal_hold`를 **먼저** 확인한다. hold 사유가 있으면 그 곡선에서 나온
   값을 쓰지 않는다 — `derive_hard_retrain_limit`은 hold를 참조하지 않으므로 오염된
   곡선에서도 숫자 모양이 정상인 값을 낸다(`#485` spec §4.2 "소비 순서").
3. `derive_hard_retrain_limit(result, safety_margin_days=...)`의 `limit_days`를
   위 env 값으로 반영한다. `limit_days`가 `None`이면 **값을 만들지 않는다** —
   "열화가 관측되지 않았다"는 "안전하다"가 아니다.

값을 갱신하면 이 문서의 "마지막 갱신" 줄도 함께 고친다.

**마지막 갱신**: 없음(정책 미활성 — env 비어 있음).

## 판정 규칙 (요약)

정본은 spec §4다. 운영자가 알아야 할 것만 옮긴다.

```text
passed = (지표 기준 통과) OR (하드 리밋 도달)
```

- **지표로 통과하면 사유는 `criteria_met`이다** — 기한 도달 여부와 무관하다. 기한
  때문에 통과한 것으로 기록하면 나중에 승격 이력을 읽을 때 모델 품질을 과소평가한다.
- **하드 리밋으로 통과하면 사유는 `hard_retrain_limit_reached`다.** 그 후보는
  **지표상 개선이 없다.** Draft PR 제목이 "하드 리밋 강제 교체 후보"로 바뀌고 본문
  첫머리에 경고가 붙는다.
- **guardrail은 하드 리밋으로 우회되지 않는다.** 취지는 "성능이 정체돼도 교체한다"이지
  "망가진 모델도 올린다"가 아니다. guardrail 위반이면 기한 도달 여부와 무관하게
  거부된다.

## 경과일은 근사치다 — 알고 써야 한다

`days_since_last_promotion`은 승격 workflow의 **입력**이다. 이 workflow는 MLflow에
접근하지 않고 모든 값을 호출자에게서 받는다.

현재 산출 근거는 **champion alias가 가리키는 버전의 `creation_timestamp`**이지
**alias를 실제로 부여한 시각이 아니다.** `set_model_alias`(`src/tracking/registry.py`)가
부여 시각을 남기지 않아 그것이 유일하게 얻을 수 있는 값이다.

```text
버전 생성 ≤ alias 부여   →   계산된 경과일 ≥ 실제 경과일
                          →   하드 리밋이 실제보다 **일찍** 발동
```

재학습을 덜 하는 게 아니라 더 하는 쪽이라 모델 신선도 관점에서는 안전한 방향이지만,
**정확한 값은 아니다.**

**이 근사가 깨지는 조건**: 버전을 여러 개 미리 등록해두고 나중에 골라 alias만 옮기는
운영이 자리잡으면 시차가 며칠씩 벌어져, 하드 리밋이 "며칠 일찍"이 아니라 **거의 항상
도달 상태**가 된다. 그러면 지표 조건이 사실상 무력해진다.

그 상태가 관측되면 alias 부여 시각을 기록하는 쪽으로 전환한다 — 전환 설계는 spec
§8.4에 있다. **`src/tracking/`을 건드리므로 소유자 확인이 선행돼야 한다.**

## 관련 문서

- `docs/specs/2026-08-05-hard-retrain-limit-gate.md` — 이 정책의 계약 정본
  (§4 판정 규칙, §6 경과일 근사, §8.3 값 미확정, §8.4 전환 설계)
- `docs/specs/2026-08-04-temporal-signal-promotion-integration.md` §4.1·§4.2 —
  `hard_retrain_limit` 값 산출 절차와 소비 순서
- `docs/guides/training-experiment-provenance.md` — 승격 evidence 계약
