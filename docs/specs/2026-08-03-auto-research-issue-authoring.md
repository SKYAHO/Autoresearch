# 자연어 가설 → Auto Research 이슈 발행 계약 (#490)

- **상태**: Proposed
- **날짜**: 2026-08-03
- **이슈**: #490
- **선행 계약**: `.github/ISSUE_TEMPLATE/auto_research.yml`(필드 정본),
  `tools/auto_research_issue_branch.py`(파싱 정본),
  `docs/archive/specs/2026-08-01-auto-research-dev-issue-branch.md`(브랜치 봉인)

## 목적

자율 실험 흐름의 첫 단계인 "가설 한 줄 → 구조화된 Auto Research 이슈"가 사람 손에
묶여 있다. 뒤따르는 단계는 자동화되어 있으나 시작점이 수동이라 폐루프가 스스로
돌지 못한다.

이 문서는 **에이전트가 이슈를 직접 발행하기 위해 알아야 할 계약**을 정의한다.
산출물은 작성 가이드와 drift 테스트이며, **새 도구를 만들지 않는다.**

## 결정 1 — 전용 발행 도구를 만들지 않는다

에이전트는 본문을 작성해 `gh issue create`로 발행한다. 렌더러도 발행 CLI도 두지
않는다.

초안에서는 `tools/` 아래에 본문 렌더러(순수 함수)와 발행 CLI(dry-run 기본, 발행
상한, 재발행 차단 키, 전용 토큰)를 두기로 했으나 **기각한다.** 근거는 다음과 같다.

- **발행 전 검증에 새 코드가 필요 없다.** `tools/auto_research_issue_branch.py`는
  이미 독립 CLI이며 placeholder 이슈 번호로 실행하면 본문을 그대로 검증한다.
  `criteria_id`/`reproducibility_id`는 이슈 번호에 의존하지 않으므로 발행 전에
  확정된 값을 그대로 신뢰할 수 있다.

  ```console
  $ python tools/auto_research_issue_branch.py \
      --issue-number 1 --issue-title "[AR] test" --issue-body-file body.md
  issue_branch=exp/1-test
  criteria_id=1ae256dd8c58...
  reproducibility_id=315f6fc3abe7...
  ```

- **새 토큰이 순손해다.** `gh`는 이미 인증되어 있는데 전용 토큰을 도입하면 시크릿
  표면만 넓어진다. 초안은 이를 안전 장치로 서술했으나 실제로는 반대 방향이었다.
- **폭주 방지를 걸 대상이 없다.** dry-run 기본값과 발행 상한은 호출 주체가 있을 때
  의미가 있는데, 저장소 안에 그 도구를 호출할 주체가 하나도 없었다. 사람이 손으로
  이슈를 채우던 것이 사람이 손으로 CLI를 돌리는 것으로 바뀔 뿐, 이 이슈가 풀려던
  "시작점이 수동이다"가 그대로 남는다.
- **잘못된 본문의 피해가 작다.** 워크플로의 검증 step이 브랜치 생성 step보다 먼저
  실행되므로, 계약을 어긴 본문은 **브랜치를 만들지 못하고** 실패한 run만 남긴다.
  이슈 본문을 고치고 label을 다시 붙이면 `labeled` 이벤트로 재시도된다.

**남는 위험과 그 처리.** 전용 도구가 없으므로 발행 상한과 중복 차단이 사라진다.
지금은 호출 주체가 없어 폭주가 발생할 수 없고, 자율 실행 주체(`agent_orchestration`
endpoint 등)가 생기는 시점에 **그 주체에** 상한을 붙이는 것이 옳다. 도구 계층에
미리 두면 실제 호출 경로와 어긋난 곳에 방어가 놓인다.

## 결정 2 — label 두 개가 유일한 게이트다

브랜치 생성 job은 `auto-research`와 `experiment`를 **동시에** 가질 때만 실행된다
(`.github/workflows/auto-research-issue-branch.yml`의 `if:`). Issue Form을 쓰면 두
label이 자동 부여되지만 `gh issue create`는 Form을 우회하므로 반드시 직접 지정한다.

```bash
gh issue create --title "[AR] ..." \
  --label auto-research --label experiment --assignee @me --body-file body.md
```

하나라도 빠지면 job이 실패가 아니라 **skip**된다. skip은 체크도 알림도 남기지
않으므로 브랜치 생성이 아무 흔적 없이 일어나지 않는다. 가이드가 이 명령을 그대로
싣고, drift 테스트가 두 `--label`의 실재를 고정한다.

## 결정 3 — 제목 `[AR] ` prefix는 강제하지 않는다

저장소 계약 어디에도 요구가 없다. 워크플로는 제목을 검사하지 않고,
`branch_name_for()`는 prefix가 있으면 slug 생성 전에 제거할 뿐 없다고 실패하지
않는다. Issue Form이 미리 채워 주는 관례이므로 따르기를 권하되, 발행 게이트로
승격시키지 않는다. **계약에 없는 제약을 도구가 만들면 그것이 곧 새로운 계약이 된다.**

제목에 ASCII 영소문자·숫자가 전혀 없으면 slug가 비어 `issue-<sha256 앞 12자>`로
대체된다. 이는 검증 실패가 아니라 **가독성 문제**다 — 그런 이름도 워크플로의 브랜치
정규식을 정상 통과한다. 사람이 브랜치 목록에서 실험을 식별할 수 있도록 제목에
ASCII slug 조각을 남기라는 것이 규정의 목적이다.

## 결정 4 — 렌더 정본은 기존 fixture를 실제 렌더에 맞춰 고친다

`tests/fixtures/auto_research_issue_form_rendered.md`의 `허용 범위` 절에는 체크박스가
**한 줄뿐**이었다. 반면 GitHub Issue Form의 `type: checkboxes`는 옵션 3개를 모두
렌더한다. 즉 이 fixture는 이름과 달리 "GitHub이 실제로 렌더한 본문"이 아니라
**파서 최소 통과 케이스**였다.

**결정: fixture를 3줄로 고친다. 별도 fixture를 만들지 않는다.**

```text
1줄 fixture      criteria_id=1ae256dd8c58…  reproducibility_id=315f6fc3abe7…
3줄(실제 렌더)   criteria_id=1ae256dd8c58…  reproducibility_id=315f6fc3abe7…
criteria_id 동일: True     reproducibility_id 동일: True
```

- `허용 범위`는 두 식별자의 해시 입력이 **아니다**. `criteria_id`는 주 지표 6필드,
  `reproducibility_id`는 dataset/seed/split/config 6필드만 묶는다. 따라서 fixture를
  고쳐도 봉인된 ID가 달라지지 않고 marker 재검증이 깨지지 않는다.
- 파서는 3줄도 정상 통과한다(모두 미체크 → `allowed_scope == ()`).
- fixture를 두 벌 두면 동기화 대상이 늘어난다. Issue Form 파서가 두 벌이어서 생긴
  드리프트가 #495였다. 같은 종류의 위험을 새로 만들지 않는다.
- 이름이 `..._rendered.md`인 파일이 실제 렌더와 다른 것 자체가 결함이다.
- **이 fixture의 두 번째 소비자도 안전하다.** `autoresearch/experiments/promotion_gate.py`의
  `parse_criteria()`가 같은 fixture를 읽지만 `_LABELS`의 지표 필드만 뽑아 쓰므로
  `허용 범위` 줄 수에 영향받지 않는다. 실행으로 확인했다.

이 fixture는 에이전트의 **작성 예시 정본**이기도 하다. 새 본문을 쓸 때 복사해 값만
바꾸는 것이 가장 안전하다.

## 결정 5 — 검수용 발행물의 정리 계약

marker 코멘트가 남는 순간 그 exp 브랜치를 함부로 지울 수 없다. 다만 흔한 오해와 달리
**영구 차단은 아니다** — fail-closed는 marker가 남아 있을 때만 걸린다.

| 지운 것 | 결과 |
| --- | --- |
| 브랜치만 | marker가 남아 `recorded issue branch ref is missing`으로 fail-closed |
| 브랜치 + marker 코멘트 | marker 없는 분기로 들어가 `dev`의 **현재 HEAD**로 브랜치를 다시 만들고 marker를 새로 쓴다 |

**진짜 위험은 두 번째다.** 재생성된 `base_dev_sha`는 원래 값과 다른데 marker는 새 값으로
정상적으로 다시 쓰이므로, 이미 원래 값을 인용해 둔 곳(#454 paired 비교, 결과 코멘트)과
어긋나도 **아무 데서도 실패하지 않는다.** fail-closed보다 이쪽이 위험한 실패 양식이다.

- 검수 발행은 **1건만** 한다.
- 검수가 끝나면 이슈는 **close**하되 **exp 브랜치는 남긴다.**

## 결정 6 — `Experiment` lineage는 1급 컬럼으로 추가한다

현재는 자유형 key/value에 의존해야 한다. `ExperimentMetadata`는 `key` `String(64)` /
`value` `Text`이고 API 계약도 `dict[MetadataKey, MetadataValue]`로 열려 있어,
`issue_number`가 숫자인지 `issue_branch`가 `exp/...` 형식인지 보장되지 않는다.

더 큰 문제는 **쓰기 시점**이다. metadata는 `create_experiment()`가 실험 생성 시 한 번만
기록하고 router에 metadata 갱신 endpoint가 없다. 이슈 번호와 exp 브랜치는 **발행
이후**에 확정되므로 현재 구조로는 기록할 방법이 없다.

- `Experiment`에 `issue_number` / `issue_branch`를 1급 컬럼으로 추가하고
  `ExperimentResponse`에 노출한다. 값 형식은 pydantic에서 검증한다.
- Alembic revision을 추가한다. 기존 revision은 `0001_experiment_tables` 하나뿐이므로
  `down_revision = "0001_experiment_tables"`로 연결하고 `upgrade()`/`downgrade()`를
  대칭으로 작성한다. **기존 행이 있으므로 새 컬럼은 nullable로 추가한다.**
- `models.py`의 모듈 docstring이 migration과의 동일성을 단언하므로 같은 커밋에서
  갱신한다.
- 이슈 1건이 실험 N건을 가질 수 있으므로 `issue_number`에 **unique 제약을 두지
  않는다.** 조회 성능을 위한 index는 둔다 — 대시보드(#338)가 주 사용처다.

## 작성 가이드가 담아야 할 제약

사람이 읽는 정본은 `docs/guides/auto-research-issue-authoring.md`에 둔다. 가이드는
**정본이 아니라 파생물**임을 명시한다 — 필드 정본은 Issue Form yml, 파싱 계약 정본은
`tools/auto_research_issue_branch.py`의 `_HEADING_NAMES`, `_REQUIRED_SECTIONS`,
`_COMPARISONS`, `_SNAPSHOT_REUSE`, `_SCOPE_LABELS`다.

- heading은 `### ` 뒤에 `_HEADING_NAMES` 키와 **완전히 동일한 문자열**이어야 한다.
  알 수 없는 heading과 중복 heading은 즉시 `ValueError`다. 특히
  `변경할 피처 · 모델`과 `대상 데이터 · 기간`은 가운뎃점 `·`(U+00B7)을 포함하므로
  재타이핑하지 말고 정본에서 복사한다.
- 필수 섹션은 20개 heading에서 `보조 관측 지표`, `결과 (에이전트가 채웁니다)` 두 개를
  뺀 18개이며, 값이 공백이면 실패한다.
- `주 지표 이름` / `Guardrail 지표 이름`은 `^[A-Za-z][A-Za-z0-9._-]{0,63}$`.
- `주 지표 방향`은 `higher_is_better` 또는 `lower_is_better`. `Guardrail 지표 방향`은
  추가로 `not_applicable`을 허용한다.
- Guardrail 미사용은 **세 필드가 함께** `없음` / `not_applicable` / `없음`이어야 하고,
  사용하면 세 필드가 모두 채워져야 한다.
- `비교 대상`과 `스냅샷 재사용`은 정해진 **정확한 문자열** 중 하나다.
- `랜덤 시드 목록`은 쉼표로 구분한 **중복 없는** 0 이상 정수이며 ASCII 숫자만
  허용한다. `Split 시드`도 0 이상 정수다.
- `Test 비율`·`Validation 비율`은 각각 0 초과 1 미만이고 합이 1 미만이어야 하며,
  Decimal과 float 두 경로 모두에서 이 조건을 만족해야 한다.
- `데이터셋 스냅샷`·`학습 설정 참조`는 1~256자다.
- `허용 범위` 섹션은 **체크박스 줄만** 포함해야 하며 각 줄의 label은 `_SCOPE_LABELS`의
  세 문자열 중 하나와 정확히 일치해야 한다. 거부되는 것은 체크박스 줄 **사이에 낀**
  비체크박스 줄이다 — 섹션 앞뒤 개행은 무관하다. `_parse_allowed_scope()`가 받는 값은
  `_required_content()`가 `strip()`한 뒤이기 때문이다. 미체크는 불허를 뜻하므로
  에이전트는 세 줄을 모두 명시적으로 출력한다.
- `보조 관측 지표`는 `required: false`이고 기본값이 없어, 사람이 비워 두면 GitHub이
  `_No response_`를 넣는다. 파서는 이 섹션을 검증 없이 담으므로 실패하지는 않지만,
  에이전트가 만드는 본문에서는 이 섹션을 **채우거나 heading 자체를 생략**한다.

## 범위 밖

- exp 브랜치를 checkout해 가설을 코드로 구현하고 candidate SHA를 만드는 실험
  실행기와 변형 큐·상태 머신 — #492
- 실험 종료 후 `### 결과 (에이전트가 채웁니다)` 섹션을 채우는 결과 보고 양식 — #494
- 지표 판정 엔진의 통합 — #493
- 발행 상한·중복 차단 등 폭주 방지 — 자율 실행 주체가 생기는 시점에 그 주체에 둔다
