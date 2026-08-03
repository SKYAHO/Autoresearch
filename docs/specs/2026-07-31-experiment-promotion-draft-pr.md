# 실험 결과 기반 Draft main PR 승격 게이트

## 목적

실험의 구조화된 baseline/candidate metric을 이슈 성공 기준과 비교하고, 통과한
dev 후보 SHA를 변경 불가능한 `promote/<issue>-<experiment>-<sha>` ref로 고정해
Draft main PR을 만든다. PR은 자동 병합하지 않는다.

## 입력 계약

workflow_dispatch 또는 repository_dispatch(`experiment_result`)의 payload는 다음을
포함한다.

```json
{
  "issue_number": 449,
  "experiment_id": "primary",
  "candidate_sha": "40자리 소문자 SHA",
  "registry_uri": "gs://bucket/experiments/449/primary/<candidate_sha>/registry.db",
  "run_id": "run-001",
  "primary_candidate": 0.7812,
  "primary_baseline": 0.778,
  "guardrail_candidate": null,
  "guardrail_baseline": null,
  "image_digest": "sha256:...",
  "artifact_uri": "gs://bucket/artifacts/449/primary/<candidate_sha>/run-001/",
  "log_uri": "gs://bucket/logs/449/primary/<candidate_sha>/run-001/"
}
```

### metric 필드 표기 (#495)

`primary_candidate` / `primary_baseline`은 **필수**이며, `guardrail_candidate` /
`guardrail_baseline`은 선언된 guardrail이 있을 때만 채운다. 네 값 모두 유한한 십진
실수여야 하고, 표기는 다음을 모두 허용한다.

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

### Issue Form label 정본 (#495)

`autoresearch/experiments/promotion_gate.py`의 `_LABELS`는 아래 여섯 문자열을
`.github/ISSUE_TEMPLATE/auto_research.yml`의 `label:`과 **문자 그대로 동일하게**
유지해야 한다. 한쪽만 바뀌거나 한쪽만 머지되면 `parse_criteria()`가 실제 이슈
본문에서 항상 실패한다.

| `_LABELS` 키 | Issue Form label |
| --- | --- |
| `primary_name` | `주 지표 이름` |
| `primary_direction` | `주 지표 방향` |
| `minimum_delta` | `최소 주 지표 개선폭` |
| `guardrail_name` | `Guardrail 지표 이름` |
| `guardrail_direction` | `Guardrail 지표 방향` |
| `maximum_regression` | `최대 Guardrail 악화폭` |

이 정합성은 `tests/test_experiment_promotion_gate.py`가 두 방향으로 고정한다 —
정본 fixture(`tests/fixtures/auto_research_issue_form_rendered.md`)를 직접 파싱하는
테스트와, `_LABELS`의 모든 값이 Issue Form에 실재하는지 확인하는 테스트다.
Issue Form을 수정하는 PR은 같은 PR에서 `_LABELS`와 fixture를 함께 갱신한다.

### `experiment_id` 형식 (#495)

`experiment_id`는 `^[a-z0-9][a-z0-9-]{0,31}$`이며, 이 값이 저장소 전체의 단일
계약이다. 정의는 다섯 개 파일에 걸쳐 여섯 군데 있으며 모두 같아야 한다 —
테스트가 동일성을 단언한다.

| 파일 | 위치 수 |
| --- | --- |
| `.github/workflows/auto-research-promotion.yml` | 1 |
| `.github/workflows/auto-research-dev-promotion.yml` | 2 |
| `tools/auto_research_issue_branch.py` | 1 |
| `src/pipeline/paired_experiment.py` | 1 |
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

1. Python gate가 이슈 본문 기준을 parse하고 기준 충족 여부를 계산한다.
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
6. `repository_dispatch` producer는 Airflow 등 외부 소유이며, 이 저장소는 공개
   event payload만 소비한다.

## 제한

여러 exp 변경을 dev에 동시에 merge하면 candidate SHA에는 선행 dev 변경도 포함될
수 있다. 실험별 Registry 격리와 offline 실행은 별도 실행 이슈에서 담당하며, 이
게이트는 결과의 Registry URI·SHA lineage를 검증한다. 실패 결과는 main 병합 후보로
표시하지 않고 원래 이슈 comment로만 기록한다.
