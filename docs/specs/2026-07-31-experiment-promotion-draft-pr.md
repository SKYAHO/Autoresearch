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

## 동작 계약

1. Python gate가 이슈 본문 기준을 parse하고 기준 충족 여부를 계산한다.
2. 통과하면 GitHub Script가 candidate SHA에서 promotion ref를 생성한다.
3. 같은 promotion ref/PR은 idempotent하게 재사용하며 ref를 이동하지 않는다.
4. main 대상 Draft PR에는 지표·기준·원본 이슈 링크와 Registry·image·run lineage를 기록한다.
5. 실패·기각 comment에는 experiment_id·candidate SHA·run_id·metric·reason·artifact/log
   URI를 기록한다. 동일 결과 재전송은 marker로 중복 생성하지 않는다.
6. `repository_dispatch` producer는 Airflow 등 외부 소유이며, 이 저장소는 공개
   event payload만 소비한다.

## 제한

여러 exp 변경을 dev에 동시에 merge하면 candidate SHA에는 선행 dev 변경도 포함될
수 있다. 실험별 Registry 격리와 offline 실행은 별도 실행 이슈에서 담당하며, 이
게이트는 결과의 Registry URI·SHA lineage를 검증한다. 실패 결과는 main 병합 후보로
표시하지 않고 원래 이슈 comment로만 기록한다.
