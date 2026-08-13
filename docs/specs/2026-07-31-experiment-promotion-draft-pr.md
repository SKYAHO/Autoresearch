# 실험 결과 기반 Draft main PR 승격 게이트

## 목적

판정 엔진이 이미 내린 결과를 받아 신뢰 경계를 검증하고, 통과한 dev 후보 SHA를
변경 불가능한 `promote/<issue>-<experiment>-<sha>` ref로 고정해 Draft main PR을
만든다. PR은 자동 병합하지 않는다.

**이 게이트는 판정하지 않는다 (#493).** 판정은
`autoresearch/model_evaluation/experiment_evaluation.py` 한 곳에서만 계산하며, 여기는 소비자다.
자세한 계약은 `docs/specs/2026-08-03-paired-offline-experiment-comparison.md`
§4 "판정 소재지 — 게이트는 판정하지 않는다"가 정본이다.

## 입력 계약

**입력은 판정 결과 payload다 (#493).** 게이트는 metric을 받아 스스로 판정하지 않고,
이미 끝난 판정 결과를 받아 신뢰 경계만 검증한다. 정본은
`docs/specs/2026-08-03-paired-offline-experiment-comparison.md`의 결과 계약
`paired-offline-experiment-result-v1`과 그 "완료 이벤트 투영"이다.

`experiment_result` repository_dispatch 이벤트는 **폐지한다.** 완료 이벤트를
`auto-research-experiment-completed` 하나로 합치고, dev 병합 워크플로가 후보를 고르고
병합한 뒤 선택된 결과 하나로 이 게이트를 `workflow_call`로 호출한다. 두 이벤트를
유지하면 producer가 같은 사실을 서로 다른 두 스키마로 두 번 말해야 하며, 실제로 두
스키마는 `experiment_id` 정규식에서 이미 갈라졌던 전력이 있다(아래 참조).

`workflow_dispatch` 수동 입력 경로는 **유지한다.** 게이트를 dev 병합 없이 단독
재실행할 수 있는 유일한 운영 회복 경로다.

### 두 진입점 모두 단일 JSON 문자열 입력을 쓴다 (#493)

GitHub Actions의 `inputs`는 `string`/`number`/`boolean`/`choice`만 지원하고 object·
array 타입이 없다. 그런데 새 입력 계약의 정본인 `PairedExperimentResult`에는 중첩
object(`baseline`/`candidate` = `ConditionLineage`)와 배열(`reason_codes`,
`extra_features`, `seeds`)이 들어 있다. 따라서 결과 payload를 필드별 입력으로 표현할
수 없다.

**결정: `workflow_call`과 `workflow_dispatch` 모두 `result_payload`(`type: string`,
`required: true`) 하나로 받는다.** 워크플로는 그 문자열을 파싱한 뒤 정확 일치
fail-closed 검사를 수행한다. 검사가 입력 층이 아니라 파싱 이후로 내려가는 것을
감수하는 대신, 두 진입점이 **같은 계약**을 받는다.

이 결정을 명시하지 않으면 "수동 경로는 평면 필드, 자동 경로는 결과 payload"로 두
진입점이 다시 갈린다 — 이 작업이 없애려는 바로 그 드리프트다. 현재
`auto-research-promotion.yml`의 `workflow_dispatch` 입력은 평면 문자열 **13개**
(`issue_number`~`outcome`)이며, 7단계에서 이를 `result_payload` 하나로 교체한다.
GitHub이 문서화한 `workflow_dispatch` 입력 개수 상한(10개)과 현재 13개의 관계는
교체 시점에 함께 확인한다 — 단일 입력으로 가면 이 문제 자체가 사라진다.

```json
{
  "contract_version": "paired-offline-experiment-result-v1",
  "outcome": "comparison_passed",
  "decision_reason": "criteria_met",
  "reason_codes": ["primary_roc_auc_improved_with_95pct_confidence"],
  "issue_number": 449,
  "issue_branch": "exp/449-...",
  "experiment_id": "primary",
  "base_dev_sha": "40자리 소문자 SHA",
  "candidate_sha": "40자리 소문자 SHA",
  "registry_root": "gs://bucket/experiments/449/primary",
  "plan_id": "...",
  "evidence_id": "...",
  "evaluation_id": "...",
  "decision_id": "...",
  "policy_version": "promotion-policy-v2",
  "metric_name": "roc_auc",
  "primary_baseline": 0.778,
  "primary_candidate": 0.7812,
  "paired_delta_mean": 0.0032,
  "confidence_interval_lower": 0.0011,
  "confidence_interval_upper": 0.0053,
  "seeds": [42, "...", 71],
  "model_uri": "...",
  "evaluated_at": "2026-08-03T00:00:00+00:00"
}
```

위는 발췌다. **정확한 필드 집합은 결과 모델(`PairedExperimentResult`)에서 파생된
투영 상수 한 곳이 소유하며, 이 문서는 그 파생물이다.** 조건별 `ConditionLineage`
(`baseline`/`candidate`), `feature_service`, `extra_features`, dataset/split
fingerprint 등 나머지 필드도 함께 실린다. `runs`만 payload 크기 때문에 제외한다.

`guardrail_candidate`/`guardrail_baseline`은 **입력에서 사라진다.** 판정 엔진이
guardrail을 계산하지 않아 결과 계약에 실리지 않으므로, 게이트가 요구하면 영원히
통과할 수 없다. guardrail을 선언한 실험은 이슈 발행 시점에 자동 승격 대상이
아님을 고지받는다.

### metric 필드 표기 (#495)

> #493 이후로 이 규칙은 결과 payload의 수치 필드(`primary_baseline`,
> `primary_candidate`, `paired_delta_mean`, `confidence_interval_lower`/`upper`)에
> 적용된다. guardrail 두 필드는 입력에서 사라졌다.

수치 필드는 유한한 십진 실수여야 하고, 표기는 다음을 모두 허용한다.

- 일반 표기: `0.7812`, `-0.0004`, `0`
- 지수 표기: `1e-05`, `2E+3` — producer가 작은 delta를 JSON 숫자로 실으면 흔히
  이렇게 직렬화된다.

거부: 빈 값, 비숫자, `NaN`, `Infinity`, 선행 0(`01.5`), 소수점만 있고 자릿수가 없는
형태(`1.`). 이 검증은 lineage 단계에서 수행하며 위반은 `input_invalid:`로 분류한다.
gate 단계의 `float()`이 받아들이던 범위를 좁히지 않는 것이 계약이다 — 좁히면 외부
producer의 정상 payload가 거부된다.

이슈 본문의 구조화 기준과 지표 이름·방향·delta를 검증한다. candidate SHA는 현재
`dev`의 조상이어야 하며, Registry URI는 issue·experiment·candidate SHA로 결정되는
경로와 일치해야 한다. 실패·기각·기준 미달은 ref/PR을 만들지 않고 원래 실험 이슈에
결과 comment를 남긴다.

### Issue Form 파서는 게이트에서 제거한다 (#493)

승격 게이트는 Issue Form 본문을 **파싱하지 않는다.** `parse_criteria()`와 `_LABELS`를
제거하고, `rg -n "### " autoresearch/experiments/`가 비어 있는 상태를 유지한다.

근거는 #495가 남긴 교훈이다. 게이트가 자체 Issue Form 파서를 갖고 있었기 때문에,
게이트를 도입한 커밋이 폼 변경분을 squash 과정에서 잃자 `_LABELS` 6개 중 3개가 실제
폼에 존재하지 않는 heading을 가리키게 되었고, `parse_criteria()`가 실제 이슈 본문에서
100% 실패했다. 자체 테스트가 실제 폼이 아니라 그 잘못된 label로 본문을 **합성**했기
때문에 파서와 테스트가 같이 틀린 채 통과했다. `7b26a50`이 여섯 문자열을 폼에 맞춰
복구했지만, **두 번째 파서가 존재하는 한 같은 드리프트가 다시 생길 수 있다.**

Issue Form을 읽는 파서는 `tools/auto_research_issue_branch.py`의 `parse_issue_input`
하나로 남긴다. 게이트가 필요로 하던 사용자 선언 임계는 `criteria_id`로 봉인된 채
판정 엔진까지 전달되므로, 게이트가 본문을 다시 읽을 이유가 없다.

Issue Form을 수정하는 PR은 같은 PR에서 `_HEADING_NAMES`와 정본 fixture
(`tests/fixtures/auto_research_issue_form_rendered.md`)를 함께 갱신한다.

### `experiment_id` 형식 (#495)

`experiment_id`는 `^[a-z0-9][a-z0-9-]{0,31}$`이며, 이 값이 저장소 전체의 단일
계약이다. 정의는 다섯 개 파일에 걸쳐 여섯 군데 있으며 모두 같아야 한다 —
테스트가 동일성을 단언한다.

| 파일 | 위치 수 |
| --- | --- |
| `.github/workflows/auto-research-promotion.yml` | 1 |
| `.github/workflows/auto-research-dev-promotion.yml` | 2 |
| `tools/auto_research_issue_branch.py` | 1 |
| `autoresearch/model_evaluation/paired_experiment.py` | 1 |
| `autoresearch/experiments/context.py` | 1 |

이전에 dev 승격 워크플로와 선택기가 허용하던 `^[a-z0-9][a-z0-9._:-]{0,127}$`는
런타임 계약보다 넓어, dev 병합을 통과한 값이 main 승격에서 거부될 수 있었다.
좁은 쪽으로 통일했다. 넓은 형식이 허용하던 `:`는 git ref 이름 불허 문자이므로
`promote/*` 브랜치 이름으로도 쓸 수 없다.

**롤아웃 영향(fail-closed 축소).** dev 승격 워크플로는 이전 실행이 남긴 결과 marker
코멘트를 다시 파싱해 거기서 뽑은 `experiment_id`를 재검증한다. 따라서 넓은 형식의
좌표가 이미 이슈에 존재하면 그 좌표의 재개 경로는 이 변경 이후 fail-closed된다.
축소 시점에 두 워크플로의 실행 이력이 0건이었고 넓은 형식의 marker도 존재하지
않았으므로 실무 영향은 없었다. 앞으로 이 패턴을 다시 좁힐 때는 기존 marker를 먼저
조사하고, 필요하면 marker 버전을 올려 구 계약을 분리한다. 되돌릴 때는 넓히는 방향이
안전하므로 정규식만 원복하면 된다.

**`_CANDIDATE_ID_PATTERN`은 의도적으로 제외한다.** `tools/auto_research_issue_branch.py`의
`candidate_id`는 완료 이벤트 안에서만 쓰이는 변형 식별자이며 git ref나 GCS 경로
구성 요소가 아니다. `experiment_id`와 달리 좁혀야 할 런타임 제약이 없으므로 기존
형식을 유지한다.

## 동작 계약

1. 게이트가 결과 payload의 신뢰 경계를 검증한다 (#493) — `contract_version` /
   `policy_version`이 아는 값인지, `outcome == "comparison_passed"`인지,
   `criteria_id` / `reproducibility_id`가 issue branch marker와 일치하는지, SHA
   lineage와 `registry_uri` 좌표가 맞는지. **metric을 임계와 비교하지 않는다.**
2. 통과하면 GitHub Script가 candidate SHA에서 promotion ref를 생성한다.
3. 같은 promotion ref/PR은 idempotent하게 재사용하며 ref를 이동하지 않는다.
4. main 대상 Draft PR에는 지표·기준·원본 이슈 링크와 Registry·image·run lineage를 기록한다.
5. 실패·기각 comment에는 experiment_id·candidate SHA·run_id·metric·reason·artifact/log
   URI를 기록한다. 동일 결과 재전송은 marker로 중복 생성하지 않는다.
   **gate step이 예외로 실패한 경우에도 comment를 남긴다**(#495) — 판정 코드가 죽으면
   원인과 무관하게 흔적이 사라져 결함이 장기간 드러나지 않는다.
   거부 사유는 세 갈래로 구분한다: `input_invalid:`(입력 형식이 계약을 벗어남),
   `lineage_invalid:`(좌표·계보 불일치), `comparison_rejected:`(비교는 성립했으나
   통과하지 못한 정상 결과). comment 제목도 이 구분을 반영한다.
6. 이 게이트는 더 이상 `repository_dispatch`를 직접 받지 않는다 (#493). 진입점은
   dev 병합 워크플로의 `workflow_call`과 `workflow_dispatch` 두 개다. 완료 이벤트
   `auto-research-experiment-completed`의 producer는 Airflow 등 외부 소유이며, 이
   저장소는 공개 event payload만 소비한다는 원칙은 그대로다.

## 제한

여러 exp 변경을 dev에 동시에 merge하면 candidate SHA에는 선행 dev 변경도 포함될
수 있다. 실험별 Registry 격리와 offline 실행은 별도 실행 이슈에서 담당하며, 이
게이트는 결과의 Registry URI·SHA lineage를 검증한다. 실패 결과는 main 병합 후보로
표시하지 않고 원래 이슈 comment로만 기록한다.
