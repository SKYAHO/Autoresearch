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
계약이다. 정의 지점은 다섯 곳(`auto-research-promotion.yml`,
`auto-research-dev-promotion.yml` 2곳, `tools/auto_research_issue_branch.py`,
`src/pipeline/paired_experiment.py`, `autoresearch/experiments/context.py`)이며
모두 같아야 한다 — 테스트가 동일성을 단언한다.

이전에 dev 승격 워크플로와 선택기가 허용하던 `^[a-z0-9][a-z0-9._:-]{0,127}$`는
런타임 계약보다 넓어, dev 병합을 통과한 값이 main 승격에서 거부될 수 있었다.
좁은 쪽으로 통일했다. 넓은 형식이 허용하던 `:`는 git ref 이름 불허 문자이므로
`promote/*` 브랜치 이름으로도 쓸 수 없다.

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
