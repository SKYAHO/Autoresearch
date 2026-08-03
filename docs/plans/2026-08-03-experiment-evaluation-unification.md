# 실험 판정 엔진 단일화와 완료 이벤트 통합 (#493)

> 관련 spec: `docs/specs/2026-08-03-paired-offline-experiment-comparison.md`,
> `docs/specs/2026-07-31-experiment-promotion-draft-pr.md`

## 목적

같은 실험이 어느 경로로 흘러가느냐에 따라 다른 판정을 받는 상태를 끝낸다.
판정은 `src/pipeline/experiment_evaluation.py` 한 곳에서만 계산하고, 나머지
두 경로는 신뢰 경계 검증만 수행하는 소비자로 강등한다. 완료 이벤트는 하나로
합쳐 producer가 같은 사실을 두 스키마로 두 번 말하지 않게 한다.

## 착수 시점의 사실 (2026-08-03 실측)

계획의 전제이므로 구현 전에 다시 확인한다.

- **#495의 승격 워크플로 버그 3건은 `7b26a50`에서 모두 해소되었다.**
  `promotion_gate._LABELS` 6개가 Issue Form `label:`과 문자 그대로 일치하고,
  `].join('\n')` 2곳이 정정되었으며, `experiment_id` 정규식이 6군데 모두
  `^[a-z0-9][a-z0-9-]{0,31}$`로 통일되었다. 즉 이슈 #493 본문의 "시나리오 4"는
  **이미 해결된 과거 기록**이며, 이 계획은 그 위에서 시작한다. 다만 단일화의
  근거는 그대로 유효하다 — 파서가 두 벌이라는 사실이 그 드리프트를 낳았고,
  이 작업이 두 번째 파서를 제거한다.
- **마이그레이션 대상이 0건이다.** 열린 `auto-research` 이슈 3건(#470, #427,
  #425) 어디에도 `<!-- auto-research-issue-branch:v1 -->` marker 코멘트가 없다.
  따라서 Issue Form의 `랜덤 시드 목록`을 바꿔 `reproducibility_id`가 달라져도
  봉인 재검증이 깨질 진행 중 실험이 없다. **배포 직전에 다시 확인하고 결과를
  PR 본문에 남긴다.**
- 저장소 안에 두 완료 이벤트의 producer가 하나도 없다. 스키마를 고치는 가장
  저렴한 시점이다.
- `promotion_gate.evaluate()`는 `criteria.primary_name`을 **사용하지 않는다**
  (`autoresearch/experiments/promotion_gate.py:110-126`). 호출자가 어떤 지표를
  넘겼는지 검증하지 않는다.
- `EvaluationReasonCode` 18개 중 6개(`PLAN_NOT_PREDECLARED`,
  `PLAN_EVIDENCE_MISMATCH`, `PLAN_ID_MISMATCH`, `COMPARISON_PLAN_MISMATCH`,
  `METRIC_SPLIT_MISMATCH`, `TIMESTAMP_TIMEZONE_MISSING`)는 선언만 되어 있고
  방출되는 곳이 없다. 이 작업에서 정리 대상으로 삼는다.

## 확정된 설계 결정

### D1. 정본 판정 엔진은 `src/pipeline/experiment_evaluation.py`다

근거(대안 검토 포함):

- 시드 노이즈를 판정하는 유일한 경로다. 경로 A·B를 정본으로 삼으면
  `docs/specs/2026-07-29-auto-research-minimum-loop-gaps.md` §⑤가 미해결로 남는다.
- 입력을 **재검증**하는 유일한 경로다. 경로 A·B는 서명 없는 채널로 들어온
  숫자를 그대로 믿으므로, producer가 임의의 값을 보내면 그대로 dev 병합과
  Draft PR이 만들어진다.
- 이식·감사 가능한 불변 판정 레코드(`evaluation_id`/`decision_id`, canonical
  SHA-256)를 남기는 유일한 경로다.
- 소비 계층(`src/pipeline/paired_experiment.py`)과 결과 계약
  (`paired-offline-experiment-result-v1`)이 #454로 이미 존재한다.
- 대안 "경로 A를 정본으로 두고 CI 계산을 추가"는 기각한다. GitHub Actions
  runner 안에서 MLflow·GCS 재검증을 수행해야 하므로 신뢰 경계가 무너진다.

### D2. 사용자 선언 임계를 엔진이 흡수한다

`최소 주 지표 개선폭`은 지금 경로 A·B가 각자 재판정하는 근거다. 이 값을
판정 호출 인자로 엔진에 넘기고, 게이트에서는 임계 비교를 제거한다.

```text
eligible  ⟺  confidence_interval_lower > 0  AND  normalized_delta_mean >= declared_minimum
```

- 선언 임계는 `PromotionPolicy` 상수가 **아니다.** 정책은 통계 기준(CI 하한 > 0)을
  소유하고, 실용적 유의성 임계는 이슈가 소유한다. 따라서 `evaluate_experiment()`의
  키워드 인자로 받는다. `promotion_policy_v1()`이 인자 없는 frozen 모델을
  반환하는 현재 구조는 유지한다.
- 값은 `criteria_id`로 봉인된 Issue Form 값을 그대로 쓴다. 게이트가 다시 파싱하지
  않는다.
- 새 reason code `PRIMARY_DELTA_BELOW_DECLARED_MINIMUM`
  (`primary_delta_below_declared_minimum`)를 추가한다.

**전달 경로와 검증 지점 (리뷰 반영).** marker에는 `criteria_id`가 sha256
**다이제스트**로만 실린다(`auto-research-issue-branch.yml`의 marker 정규식은 두 해시와
`base_dev_sha`만 담는다). 임계의 **수치**는 어디에도 없으므로 전달 경로를 못박는다.

1. 판정 엔진을 호출하는 주체(#492 실험 실행기)는 **이슈 본문을 `parse_issue_input()`에
   넣어** `IssueInput.minimum_primary_delta`를 얻는다. D9가 남기는 단 하나의 Issue Form
   파서가 그것이며, 새 파서를 만들지 않는다.
2. 같은 호출에서 나온 `criteria_id`가 marker에 봉인된 값과 일치하는지 **호출자가 먼저
   확인**한다. 일치하면 그 본문이 봉인 시점과 같으므로, 함께 나온 임계값도 봉인된
   본문의 값이다.
3. **판정 결과가 임계를 실제로 적용했음을 결과 계약이 증거로 남긴다.**
   `PairedExperimentResult`에 `declared_minimum_primary_delta`와 `criteria_id`를
   싣고, `evaluation_id` 해시 입력에 임계값을 포함한다. 그러면 게이트는 재판정 없이
   "이 판정이 이 임계로 수행됐다"를 확인할 수 있다.
4. 게이트 검증 항목에 다음을 추가한다 — 결과의 `criteria_id`가 marker와 일치하고,
   결과의 `declared_minimum_primary_delta`가 그 이슈 본문에서 파생된 값과 같을 것.

이 셋이 없으면 "게이트가 재판정할 이유를 없앤다"가 "아무도 검증하지 않는다"로
바뀐다. 임계를 0으로 넣거나 아예 적용하지 않은 판정이 통과하기 때문이다.
- **verdict는 `reject`다.** 통계적으로는 개선이 확인되었으나 사용자가 선언한
  실용적 유의성에 못 미치는 상태는 "판정 불가"가 아니라 명확한 기각이다.
  `hold`로 두면 `comparison_failed`가 되어 재시도 가치가 있는 것처럼 읽힌다.

### D3. 지표와 방향을 확장한다

현재 `roc_auc`는 판정 엔진뿐 아니라 **write-once 증거 계약에 고정**되어 있다.

| 고정 지점 | 위치 |
| --- | --- |
| `HeldOutMetricEvidence.metric_name: Literal["roc_auc"]` | `src/pipeline/promotion_evidence.py:156` |
| `HeldOutMetricEvidence.value: float = Field(ge=0, le=1)` | `src/pipeline/promotion_evidence.py:158` |
| `PromotionPolicy.primary_metric: Literal["roc_auc"]` | `src/pipeline/experiment_evaluation.py:112` |
| `MetricDirection`에 `MAXIMIZE`뿐 | `src/pipeline/experiment_evaluation.py:75-79` |
| `_verified_metric_values`의 `metric_name != PRIMARY_METRIC` 검사 | `src/pipeline/experiment_evaluation.py:354-355` |
| 유일한 receipt 생산자가 ROC-AUC만 계산 | `src/pipeline/train.py:801-806` |

따라서 확장은 증거 생산 경로까지 함께 바꿔야 한다.

**기존 증거는 마이그레이션하지 않는다.** `metric_name`을 `Literal`에서 allowlist
검증으로 넓히고 `value` 범위를 지표별로 바꾸는 것은 **기존 필드의 직렬화를 바꾸지
않는다.** GCS에 이미 저장된 receipt의 바이트가 그대로이므로 `GcsObjectReceipt`의
sha256도 그대로이고, 기존 `roc_auc` receipt는 새 스키마에서도 유효하다. 새 필드를
추가하지 않는 것이 이 성질의 조건이다 — **필드를 추가해야 한다면 그 순간
마이그레이션 문제가 생기므로 설계를 되돌아본다.**

- `MetricDirection`에 `MINIMIZE = "minimize"`를 추가한다.
- 지원 지표는 정책이 소유하는 allowlist로 정의한다. v1 후보는 학습 경로가 실제로
  산출할 수 있는 세 가지다 — `roc_auc`, `pr_auc`(`average_precision_score`),
  `log_loss`. 세 값 모두 `src/pipeline/evaluate.py:142-145`가 이미 계산한다.
- **방향은 정책이 소유하고 사용자가 뒤집을 수 없다.** `log_loss`를
  `higher_is_better`로 선언하는 것은 의미가 없다. Issue Form의 `주 지표 방향`
  필드는 사용자의 의도 선언으로 남기되, 정책이 정한 방향과 다르면 **브랜치 생성
  시점에** fail-closed로 거부한다. 실행이 끝난 뒤 알게 되는 비용을 없앤다.
- `value` 범위는 지표별로 검증한다. `roc_auc`/`pr_auc`는 `[0, 1]`,
  `log_loss`는 `[0, ∞)`이며 유한해야 한다. 현재의 `Field(ge=0, le=1)` 단일 제약을
  지표별 validator로 바꾼다.
- delta는 **개선량으로 정규화**한다. `MAXIMIZE`면 `candidate - baseline`,
  `MINIMIZE`면 `baseline - candidate`. seed별 paired delta도 같은 규칙으로
  정규화하고, CI는 정규화된 delta 위에서 계산한다. 그 결과 `eligible` 조건
  `confidence_interval_lower > 0`은 **방향과 무관하게 동일한 형태**로 유지된다.
- `_verified_metric_values`는 상수 `PRIMARY_METRIC`이 아니라 **그 실험이 선언한
  주 지표**와 대조한다. `dataset_split == "test"` 고정은 유지한다.
#### 정책 버전 인상은 그대로 하면 기존 증거를 못 읽는다 (리뷰 반영)

초안은 "`PROMOTION_POLICY_VERSION`을 `promotion-policy-v2`로 올리되 v1로 기록된
기존 plan·evidence는 그대로 읽힌다"고 적었다. **이 서술은 틀렸다.**

`_plan_identity_payload()`(`src/pipeline/promotion_evidence.py:99-113`)는
`policy_version` 자리에 `self.policy_version`이 아니라 **모듈 상수**
`PROMOTION_POLICY_VERSION`을 넣는다. 그리고
`ExperimentPlan._validate_content_addressed_plan_id`(`:132-144`)가 그 payload로
`plan_id`를 재계산해 저장된 값과 대조한다. 따라서 상수를 바꾸는 순간 v1 시절에
발행된 모든 plan은 재계산된 `plan_id`가 달라져 읽히지 않는다. 실측했다.

```text
v1에서 발행한 plan_id: experiment-plan-19e297ab32f639850eb45833...
상수를 v2로 교체 후 재검증 → ValueError:
  plan_id가 canonical experiment plan 내용과 다릅니다
```

`HeldOutMetricEvidence`가 `plan_receipt`로 plan을 통째로 품으므로
(`promotion_evidence.py:155` 부근), `verify_held_out_metric_receipt()`의
`model_validate_json()`도 함께 깨진다. **GCS object의 바이트와 sha256은 그대로지만
더 이상 역직렬화되지 않는다.** 2단계의 무마이그레이션 논거("필드를 추가하지 않으므로
직렬화가 안 바뀐다")는 **필드 추가**만 다루며 **상수 값 변경**은 다루지 않는다 —
지표 확장은 실제로 안전하지만 정책 버전 인상은 안전하지 않다.

추가로 `Literal["promotion-policy-v1"]`이 네 곳에 박혀 있어 상수만 바꾸면 v2 값이
검증에서 거부된다 — `promotion_evidence.py:123`, `experiment_evaluation.py:111`·
`:143`·`:170`. (`paired_experiment.py:199`는 `str`이라 영향이 없다.)

**대응 방침:** plan 식별 payload에서 `policy_version`을 **제외**한다.

- `plan_id`는 "무엇을 비교하기로 사전 선언했는가"(가설·control·candidate·시각)의
  content address여야 한다. 그 선언을 **어떤 정책으로 판정할지**는 plan의 정체성이
  아니라 판정 시점의 선택이므로, 식별 payload에 들어갈 이유가 없다.
- `policy_version`은 필드로는 그대로 남긴다(어떤 정책으로 발행됐는지 기록은 필요).
  `Literal`은 v1·v2 유니온으로 넓힌다.
- **단, 이 변경 자체가 `plan_id` 계산식을 바꾼다.** 기존 v1 plan의 `plan_id`는 여전히
  `policy_version`을 포함해 계산된 값이므로, 식별 payload에서 그냥 빼면 이번에는
  그 이유로 읽히지 않는다. 따라서 `_validate_content_addressed_plan_id`가 **신규
  계산식으로 먼저 대조하고, 실패하면 v1 legacy 계산식(정책 버전 포함)으로 한 번 더
  대조**하는 2단 검증을 둔다. 신규 발행은 항상 신규 계산식을 쓴다.
- legacy 경로는 읽기 전용이며 새 plan을 만들 때는 쓰지 않는다. 이 비대칭을 코드
  주석과 이 문서에 남긴다.

대안으로 "식별 payload가 상수 대신 `self.policy_version`을 쓰도록 바꾸는" 방법도
있으나, 이는 v1 plan의 `plan_id`를 그대로 재현하므로 legacy 분기 없이 호환된다.
**구현 시 이쪽을 먼저 검토한다** — v1 plan은 `policy_version`이 항상
`"promotion-policy-v1"`이므로 상수를 읽든 필드를 읽든 결과가 같고, v2 plan만 새 값을
갖게 되어 자연스럽게 갈린다. 이 경우 legacy 분기가 아예 필요 없다.

### D4. guardrail은 고지만 하고 엔진 확장은 별도 이슈로 넘긴다

판정 엔진에 guardrail 개념이 없다(`grep -rn "guardrail" src/pipeline/` → 0건).
이 작업에서 guardrail paired 판정을 구현하지 않는다. 대신 **조용한 무시를 만들지
않는다.**

- 브랜치 생성 시점(`.github/workflows/auto-research-issue-branch.yml`)에 guardrail을
  선언한 이슈에는 "이 실험은 자동 승격 대상이 아니다"를 코멘트로 고지한다.
- 경로 A는 guardrail을 선언한 실험의 후보를 **적격에서 제외**한다
  (`guardrail_declared_unsupported`). 승격이 조용히 일어나지 않고, 사용자가 선언한
  guardrail이 조용히 무시되지도 않는다.
- `_parse_completion_candidate`의 조건부 guardrail 필수 키
  (`guardrail_candidate_metric`, `guardrail_baseline_metric`)는 **제거한다.**
  엔진이 생산하지 않는 값을 producer에게 요구하면 영원히 통과할 수 없다.
- guardrail paired 판정 구현은 후속 `feature` 이슈로 발행하고 이 계획에 링크한다.

### D5. 완료 이벤트를 하나로 합친다

- `auto-research-experiment-completed`의 `client_payload.candidates[]`를
  `paired-offline-experiment-result-v1`의 **이벤트 투영**(D6) 배열로 정의한다.
- `experiment_result` repository_dispatch 이벤트는 **폐지**한다.
  `.github/workflows/auto-research-promotion.yml`을 `workflow_call`로 전환하고,
  dev 병합 워크플로가 후보를 고르고 병합한 뒤 선택된 결과 하나로 호출한다.
- `workflow_dispatch` 수동 입력 경로는 **유지한다.** 현재 유일하게 실제로 동작하는
  경로이며, 게이트를 단독 재실행할 수단이 사라지면 운영 회복 경로가 없어진다.
- 대안(이벤트 2개 유지 + 공유 스키마)은 기각한다. 재발행 주체와 멱등 marker가
  하나 더 필요하고, 두 스키마가 다시 갈라질 여지를 남긴다.

### D6. 이벤트 투영 — `runs[]`는 싣지 않는다

이슈는 "producer가 결과 payload를 변환 없이 싣게 한다"를 요구한다. 다만
`PairedExperimentResult`를 통째로 실으면 payload가 너무 커진다.

```text
후보 1건의 runs[] = seed 30개 × (seed, run_id, comparison_id, artifact_uri, log_uri)
                  ≈ 6 KB
후보 50건(_MAX_COMPLETION_CANDIDATES) 기준 ≈ 300 KB
```

repository_dispatch payload 한도를 넘긴다. 따라서 이벤트 투영을 **명시적으로
이름 붙여** 정의한다.

- 이벤트 후보 객체 = `PairedExperimentResult`의 필드에서 **`runs`를 제외한 전부**.
- `seeds`는 싣는다(판정 재현에 필요한 시드 집합).
- seed별 run 좌표는 결과 artifact 안에 남으며, 후보 수준 `artifact_uri`/`log_uri`가
  그 위치를 가리킨다.
- `_parse_completion_candidate`의 정확 일치(fail-closed) 성질은 **유지한다.**
  기대 키 집합만 이 투영에 맞춰 확장한다.
- 투영은 코드 한 곳의 상수로 정의하고 결과 모델에서 파생시킨다. 결과 계약에 필드가
  늘면 투영도 함께 늘도록 하고, 그 동등성을 테스트로 고정한다.

**선택 필드와 정확 일치의 양립 (리뷰 반영).** `_parse_completion_candidate`는
`missing_keys`가 하나라도 있으면 거부하는데, 투영 대상에는 `evidence_id`·
`evaluation_id`·`decision_id`·`metric_name`·`primary_baseline`·`primary_candidate`·
`paired_delta_mean`·`confidence_interval_lower`/`upper`·`model_uri` 등 `X | None = None`
필드가 많다. 다음을 계약으로 못박는다.

- **투영은 "값이 `None`이어도 키는 싣는다"를 요구한다.** producer는
  `model_dump_json(exclude_none=True)`를 쓰지 않는다. 정확 일치 fail-closed를 유지하는
  대가이며, 소비 측이 선택 키를 허용하도록 느슨해지면 오탈자 키가 조용히 무시된다.
- `hold`/`comparison_failed` 후보도 `candidates[]`에 **실어 보낸다.** 적격 판정은
  `outcome`으로 하므로 실려도 안전하고, producer가 미리 거르면 "왜 후보가 사라졌는지"가
  이벤트에 남지 않는다. 통계 필드가 `None`인 채로 실리며, 위 규칙 덕분에 키 집합은
  통과 후보와 동일하다.
- `schema_version`은 `contract_version`으로 **대체한다.** 두 버전 필드를 병존시키면
  어느 쪽이 정본인지가 다시 갈린다. 5단계 작업에 이 교체를 명시한다.

### D7. Issue Form을 엔진 정책에 정렬한다

- `랜덤 시드 목록` 기본값 `"42, 43, 44"`(3개)를 정책 시드 42..71(30개)로 정렬한다.
  현재 값은 판정에 아무 영향을 주지 않으면서 `reproducibility_id`만 오염시킨다.
- 정책 시드와 다른 시드 집합을 선언한 이슈는 **브랜치 생성 시점에** 거부한다.
- 마이그레이션은 "진행 중 이슈 0건인 시점에 배포" 방식을 택한다(위 "착수 시점의
  사실" 참조). marker 버전 인상은 하지 않는다 — 분리할 구 계약 자체가 없다.
- Issue Form을 바꾸는 PR은 같은 PR에서 `_HEADING_NAMES`와
  `tests/fixtures/auto_research_issue_form_rendered.md`를 함께 갱신한다.

### D8. 다중 candidate

엔진은 단일 candidate만 자동 승격 대상으로 인정한다
(`multiple_candidates_require_independent_holdout`). 이 제약과 경로 A의 후보 배열은
충돌하지 않는다 — **후보 N개는 각각 독립된 plan/evidence로 개별 판정된 결과 N개**이며,
경로 A는 이미 판정이 끝난 결과들 중에서 고르기만 한다. 한 번의 판정 호출에 candidate가
여럿 들어가는 경우에만 위 reason code가 나온다. 이 구분을 spec에 명시한다.

경로 A의 선택 기준을 바꾼다.

```text
적격  = outcome == "comparison_passed"
정렬  = paired_delta_mean 내림차순
      → confidence_interval_lower 내림차순
      → candidate_sha 사전순 오름차순   (결정론 보장)
```

`_is_qualified_candidate`의 임계 판정과 Decimal 산술
(`_SELECTION_DECIMAL_PRECISION = 2072`)은 제거한다. 게이트는 더 이상 숫자를 비교하지
않으므로 확장 정밀도가 필요 없다.

### D9. 게이트가 수행할 검증의 범위

두 게이트는 **신뢰 경계 검증만** 한다. 통계·임계는 재계산하지 않는다.

- `contract_version` / `policy_version` 확인 (알 수 없는 버전은 fail-closed)
- `outcome == "comparison_passed"`
- `criteria_id` / `reproducibility_id`가 issue branch marker와 일치
- SHA lineage (`base_dev_sha` 자손, issue branch 조상, dev 조상)
- `registry_uri` 좌표

`autoresearch/experiments/promotion_gate.py`의 `parse_criteria`는 제거한다.
Issue Form heading 문자열이 이 패키지에 남지 않게 한다.

## 작업 분해

| # | 작업 | 선행 | 산출물 | 단독 검증 |
| --- | --- | --- | --- | --- |
| 1 | spec 2건 갱신 + 이 계획 | — | 문서 | `git diff --check` |
| 2 | 증거 계약 지표 확장 (D3 전반부) | 1 | `promotion_evidence.py`, `train.py`, `evaluate.py` | 기존 `roc_auc` receipt 역직렬화 회귀 + 신규 지표 receipt 생산 테스트 |
| 3 | 판정 엔진 확장 (D2, D3 후반부) | 2 | `experiment_evaluation.py` | 방향별 정규화·선언 임계·새 reason code 단위 테스트 |
| 4 | 결과 계약·투영 (D6) | 3 | `paired_experiment.py` | 투영 ↔ 결과 모델 동등성 테스트 |
| 5 | 경로 A 강등 (D8, D9) + `schema_version`→`contract_version` 교체 | 4 | `tools/auto_research_issue_branch.py` | 시나리오 1 회귀 테스트 |
| 6 | 경로 B 강등 (D9) | 4 | `promotion_gate.py`, `tests/test_experiment_promotion_gate.py` | 시나리오 5 회귀 테스트 |
| 7 | 이벤트 통합 (D5) | 5, 6 | 워크플로 2건 | 두 워크플로 소비 필드 집합 동일성 테스트 |
| 8 | Issue Form 정렬 + 분기 시점 검증·고지 (D4, D7) | 3, 5 | Issue Form, fixture, `auto-research-issue-branch.yml` | 폼 ↔ `_HEADING_NAMES` ↔ fixture drift 테스트 |

2~8은 각각 별도 PR로 낸다. 하나의 PR에 증거 계약 변경과 워크플로 변경을 함께 담지
않는다.

## 검증 체크리스트

- [ ] `uv run python -m pytest`
- [ ] `uv run --no-sync ruff check agent_orchestration autoresearch tests tools`
- [ ] `rg -n "### " autoresearch/experiments/`가 비어 있다 (두 번째 파서 제거 확인)
- [ ] `tools/auto_research_issue_branch.py`에 `minimum_primary_delta`를 후보 metric과
      비교하는 코드가 없다
- [ ] `_parse_completion_candidate`가 `outcome`을 포함한 결과 payload를 거부하지 않는다
- [ ] `outcome != "comparison_passed"`인 후보가 적격에서 제외된다
- [ ] **시나리오 1 회귀**: CI가 0을 걸쳐 `hold`로 판정된 결과가 dev 병합 후보로
      선택되지 않는다
- [ ] **시나리오 5 회귀**: `eligible` 결과가 게이트에서 재판정으로 기각되지 않는다
- [ ] 승격 게이트 테스트가 합성 본문이 아니라
      `tests/fixtures/auto_research_issue_form_rendered.md`를 입력으로 쓴다
- [ ] 두 워크플로가 소비하는 payload 필드 집합이 동일하고, 그 사실을 검증하는
      테스트가 있다
- [ ] `None` 값을 가진 선택 필드가 키로 실린 payload가 거부되지 않고, `exclude_none`으로
      키가 빠진 payload는 `missing candidate keys`로 거부된다
- [ ] `workflow_call`/`workflow_dispatch` 진입점이 단일 JSON 문자열 입력을 쓰며, 평면
      필드 입력이 남아 있지 않다
- [ ] 결과의 `declared_minimum_primary_delta`가 이슈 본문에서 파생된 값과 일치하는지
      게이트가 확인한다
- [ ] 기존 `roc_auc` receipt가 새 스키마에서 그대로 역직렬화되고 sha256이 변하지
      않는다
- [ ] **v1 시절에 발행된 plan receipt가 v2 코드에서 재검증을 통과한다.** 위 항목은
      지표 확장만 검증하므로 정책 버전 인상 경로를 잡지 못한다. `plan_id` 재계산이
      깨지지 않음을 별도로 확인한다
- [ ] `Literal["promotion-policy-v1"]` 네 곳이 v1·v2를 모두 받아들이도록 갱신되었다
- [ ] `MINIMIZE` 지표의 delta 정규화가 `eligible` 조건을 방향 무관하게 유지한다
- [ ] Issue Form 기본값 그대로 발행한 이슈가 자동 승격 경로를 끝까지 통과하거나,
      통과할 수 없다면 브랜치 생성 시점에 이슈 코멘트로 고지된다
- [ ] 배포 직전 `gh issue list --label auto-research --state open`으로 marker를 가진
      진행 중 이슈가 여전히 0건임을 확인하고 결과를 PR 본문에 남긴다

## 범위 밖

- guardrail paired 판정 구현 — 별도 `feature` 이슈 (D4)
- 실험 실행기(exp 브랜치에서 candidate SHA를 만드는 주체) — #492
- 결과 보고 양식(Issue Form `### 결과` 섹션 채우기) — #494
- `ExperimentEvaluation`/`PromotionDecision`에 `confidence`·`robustness_note`·
  `direction_vs_*` 필드를 더하는 작업 — #425. **같은 pydantic 모델을 건드리므로
  착수 순서를 조율한다.**
- `environment`/`target_*`/`policy_version`을 `PromotionDecision`에 더하는 작업 —
  #470. 위와 같은 이유로 순서 조율이 필요하다.
- champion alias 이동과 prod 승격 — #470
- Airflow Job 오케스트레이션 — `SKYAHO/Autoresearch-airflow`
