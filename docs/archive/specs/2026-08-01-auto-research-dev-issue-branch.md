# Auto Research dev 기준 단일 이슈 브랜치

> 상태: 구현 중
> 관련 이슈: #449

## 목적

Auto Research Issue Form 이슈마다 현재 `dev` HEAD를 한 번만 `base_dev_sha`로
고정하고, 그 커밋에서 파생한 하나의 작업 브랜치를 만든다. 이 브랜치는 가설
구현의 유일한 작업 공간이며 이후 커밋이 쌓여도 재실행이 ref를 이동하거나
덮어쓰지 않는다.

## 범위와 경계

- Issue Form의 성공 기준과 재현 조건을 기계 판독 가능한 값으로 수집·검증한다.
- `issues` 이벤트 workflow가 `exp/<issue>-<slug>` 이슈 브랜치를 생성·재사용하고
  `base_dev_sha`와 입력 식별자를 이슈 comment와 workflow output에 기록한다.
- 공용 `prod` 배포와 champion 변경은 수행하지 않는다. `main`은 workflow가 생성·갱신·병합하지
  않으며 기존 PR·승인·필수 CI 보호 규칙을 우회하지 않는다.
- 이슈·`experiment_id`의 모든 후보가 끝났다는 완료 event가 검증된 결과 집합을 전달하면,
  기준을 통과한 최선 후보 하나만 `dev`에 자동 병합한다. 실행 Job 자체는 이 변경의 책임이
  아니며, 완료 event producer가 후보 결과를 전달한다.

## Issue Form 계약

기존 자유 문장 `success_criteria`를 아래 필드로 대체한다.

| 필드 | 형식 | 검증 |
| --- | --- | --- |
| `primary_metric_name` | 영문자 시작, 영문자·숫자·`.`, `_`, `-` | 1–64자 |
| `primary_metric_direction` | dropdown | `higher_is_better` 또는 `lower_is_better` |
| `minimum_primary_delta` | decimal | 유한한 0 이상 `Decimal`; 입력 128자 이하, 계수 64자리 이하, 지수 절댓값 1,000 이하 |
| `guardrail_metric_name` | input | `없음` 또는 주 지표와 같은 이름 규칙 |
| `guardrail_metric_direction` | dropdown | guardrail 없음이면 `not_applicable`, 있으면 비교 방향 |
| `maximum_guardrail_regression` | input | guardrail이 있으면 유한한 0 이상 `Decimal`(동일 한도), 없으면 `없음` |
| `dataset_snapshot` | 비어 있지 않은 식별자 | 1–256자 |
| `random_seeds` | 쉼표 구분 정수 | 하나 이상, 중복·음수·비정수 없음 |
| `split_seed` | 정수 | 0 이상 |
| `test_size`, `validation_size` | decimal | 각각 0 초과 1 미만, 합계 1 미만 |
| `training_config_ref` | 비어 있지 않은 식별자 | 1–256자 |

Issue Form 자체의 `required`는 빈 입력만 막으므로, workflow가 위 모든 규칙을 다시
검사한다. 하나라도 맞지 않으면 branch·comment를 만들지 않고 실패한다.

## Branch와 재실행 계약

1. `auto-research`와 `experiment` label을 모두 가진 이슈의 `opened` 또는 `labeled`
   이벤트만 처리한다.
2. workflow는 검증된 이슈 제목에서 ASCII slug를 만들고 `exp/<issue-number>-<slug>`를
   결정한다. `experiment_id`는 branch 이름에 사용하지 않는다.
3. marker comment가 없으면 현재 `heads/dev` SHA에서 ref를 생성하고, `base_dev_sha`와
   criteria·reproducibility 식별자를 bot comment와 output에 남긴다.
4. marker가 있으면 기록된 branch와 `base_dev_sha`를 읽고, branch tip이 그 baseline의
   descendant 또는 동일 커밋인지 GitHub compare API로 검사한다. 이 경우 현재 `dev`
   tip과 달라도 정상 재사용한다.
5. marker가 중복·손상됐거나 branch가 없거나 baseline에서 파생되지 않았으면
   fail-closed한다. 어떤 경우에도 `updateRef`를 호출하지 않는다.

## 완료 event와 dev 병합 계약

1. `repository_dispatch`의 `auto-research-experiment-completed` 또는 동일 입력의
   `workflow_dispatch`가 이슈 번호·`experiment_id`·후보 결과 배열을 전달한다. mutation 없는
   선행 job이 이슈 번호를 선행 0 없는 양의 10진 문자열로, `experiment_id`를 소문자 safe
   identifier로 엄격히 검증해 canonical output으로 전달한다.
2. 후보 배열은 schema version 1의 정확한 key 집합이고 최대 50개다. 후보는
   `candidate_sha`, candidate ID, primary candidate/baseline metric, 선택 guardrail metric,
   `criteria_id`, `reproducibility_id`, artifact·log 식별자를 모두 가진다. 수치는
   128자 이하·계수 64자리 이하·지수 절댓값 1,000 이하의 유한 `Decimal` 문자열이며,
   artifact·log는 제어문자·공백 전용·2,048자 초과를 허용하지 않는 단일행 식별자다.
   형식·기록 marker의 두 식별자·baseline lineage·이슈 branch ancestor 검사를 하나라도
   통과하지 못하면 전체 event를 fail-closed한다. workflow도 JSON parse 직후, 후보 object·lineage
   loop 또는 `compareCommits` 호출 전에 비어 있지 않음과 최대 50개를 함께 검사한다.
3. gate는 primary direction에 맞춰 최소 delta를, guardrail direction에 맞춰 최대 비열화를
   판단한다. 두 subtraction은 최대 64자리 입력의 반올림 경계가 판정에 영향을 주지 않도록
   최소 136자리이고 현재 지수 한도까지 고려한 2,072자리 `Decimal` local context에서 수행한다.
   적격 후보 중 primary metric이 가장 좋은 하나를 직접 `Decimal` 비교로 고르고 동률은
   `candidate_sha` 오름차순으로 결정한다.
4. 이슈+`experiment_id`별 결과 marker는 `pending`, `merged`, `no_qualified`,
   `merge_conflict`, `merge_api_failed` 중 하나의 strict state와 result-set ID·선택 SHA를
   기록한다. 새 적격 결과는 marker를 먼저 `pending`으로 생성한 뒤에만 merge한다. 같은
   result-set의 final state는 no-op이고, pending 재실행은 선택 SHA가 이미 `dev` ancestor면
   marker를 `merged`로 복구하며 아니면 merge를 재개한다. 다른 result-set은 fail-closed하고
   같은 이슈의 다른 experiment marker는 허용한다. `merge_conflict`와 `merge_api_failed`는
   terminal failure이므로 marker update가 성공한 뒤 job도 실패하고, 같은 result-set의 재실행도
   재조정·재병합 없이 실패한다.
5. 적격 후보가 하나면 GitHub merge API의 base를 `dev`, head를 선택 `candidate_sha`로
   고정한다. 201 merge와 204 already-contained는 성공으로, 409은 `merge_conflict`, 그 밖의
   API 오류는 `merge_api_failed`로 marker를 갱신한다. marker update 실패는 pending을 남겨
   재실행 reconciliation으로 복구한다. 201 response의 merge SHA가 형식 계약을 어기면 marker를
   갱신하지 않고 pending을 남긴 채 fail-closed한다.

## 보안과 운영 제약

- 이슈 제목·본문은 환경 변수로 Python validator에 전달할 뿐 shell source로 보간하지
  않는다.
- mutation 없는 좌표 검증 job은 `permissions: {}`를 사용하고, 이후 promotion job만
  `contents: write`, `issues: write`를 명시한다.
- 권한 있는 workflow는 `github.workflow_sha`를 checkout하고 checkout credential을 남기지
  않는다. selector child process에는 `PATH`, `LANG`, `PYTHONUTF8`, `GITHUB_OUTPUT`만 전달한다.
- selector가 반환한 선택 SHA는 merge 직전에 lineage 검증을 통과한 후보 SHA 집합에 다시
  포함되는지 확인한다.
- 이슈별 concurrency group을 직렬화해 같은 이벤트의 create-ref 경합을 막는다.
- 완료 event는 검증된 좌표 output만 사용해 전역 `auto-research-dev-promotion` concurrency
  group에 들어가며 `queue: max`, `cancel-in-progress: false`로 dev merge를 직렬화한다. GitHub
  Actions의 최대 100개 pending event를 보존하며 raw 이슈 번호·실험 ID로 concurrency group을
  만들지 않아 context 사전 우회를 막는다.
- 생성 branch는 공용 `prod` 배포 branch가 아니며 PR·prod 배포·champion 변경을 만들지 않는다.
- GitHub의 `issues`·`repository_dispatch` 이벤트는 기본 브랜치 `main`에 있는 workflow
  정의를 실행한다. 따라서 이 변경의 workflow 파일은 `main`에도 존재해야 하지만, `main`은
  정의 위치일 뿐 자동 merge·ref 생성/갱신·PR 생성 대상이 아니다.
- 완료 event workflow는 `dev` 외 ref를 merge base로 사용하지 않는다.
- `GITHUB_TOKEN`으로 수행한 `dev` merge는 `dev` CI를 새로 trigger하지 않는다. 현재 candidate
  lifecycle은 candidate SHA의 CI success check나 그 결과 뒤 producer 재dispatch 계약을 요구하지
  않으므로, metric 통과 자동 병합은 CI 통과 보증이 아니다. check-run gate와 producer 재dispatch는
  별도 후속 계약 범위다.

## 검증

- Python validator와 selection gate는 유효 입력, 숫자/guardrail/seed/split 오류, slug,
  marker, baseline ancestry, 후보 전체 fail-closed, direction별 최선 후보·동률의 단위
  테스트를 가진다.
- Issue Form과 workflow는 YAML parse 및 actionlint로 문법을 검증한다.
- focused pytest, 전체 pytest, Ruff, `git diff --check`를 실행한다.
