# 자연어 가설 → Auto Research 이슈 발행 계약 (#490)

- **상태**: Proposed
- **날짜**: 2026-08-03
- **이슈**: #490
- **선행 계약**: `.github/ISSUE_TEMPLATE/auto_research.yml`(필드 정본),
  `tools/auto_research_issue_branch.py`(파싱 정본),
  `docs/archive/specs/2026-08-01-auto-research-dev-issue-branch.md`(브랜치 봉인)

## 목적

자율 실험 흐름의 첫 단계인 "가설 한 줄 → 구조화된 Auto Research 이슈"가 전적으로
사람 손에 묶여 있다. 뒤따르는 단계는 자동화되어 있으나 시작점이 수동이라 폐루프가
스스로 돌지 못한다.

이 문서는 **에이전트가 따를 작성 템플릿**과 **그 결과를 기존 파서로 자가 검증한 뒤
발행하는 경로**를 정의한다. 파서를 새로 만들지 않는다 — 이미 있는
`parse_issue_input()`을 발행 **전** 게이트로 재사용한다.

## 결정 1 — 렌더 정본은 기존 fixture를 실제 렌더에 맞춰 고친다

`tests/fixtures/auto_research_issue_form_rendered.md`의 `허용 범위` 절에는 체크박스가
**한 줄뿐**이다. 반면 GitHub Issue Form의 `type: checkboxes`는 옵션 3개를 모두
렌더한다(`.github/ISSUE_TEMPLATE/auto_research.yml`의 `options:` 3개). 즉 현재
fixture는 이름과 달리 "GitHub이 실제로 렌더한 본문"이 아니라 **파서 최소 통과
케이스**다.

**결정: fixture를 3줄로 고친다. 별도 fixture를 만들지 않는다.**

근거를 실측으로 확인했다.

```text
1줄 fixture      criteria_id=1ae256dd8c58…  reproducibility_id=315f6fc3abe7…
3줄(실제 렌더)   criteria_id=1ae256dd8c58…  reproducibility_id=315f6fc3abe7…
criteria_id 동일: True     reproducibility_id 동일: True
```

- `허용 범위`는 두 식별자의 해시 입력이 **아니다**. `criteria_id`는 주 지표 6필드,
  `reproducibility_id`는 dataset/seed/split/config 6필드만 묶는다. 따라서 fixture를
  고쳐도 봉인된 ID가 달라지지 않고, marker 재검증이 깨지지 않는다.
- 파서는 3줄도 정상 통과한다(모두 미체크 → `allowed_scope == ()`).
- fixture를 두 벌 두면 동기화 대상이 늘어난다. Issue Form 파서가 두 벌이어서 생긴
  드리프트가 #495였다. 같은 종류의 위험을 새로 만들지 않는다.
- 이름이 `..._rendered.md`인 파일이 실제 렌더와 다른 것 자체가 결함이다.
- **이 fixture의 두 번째 소비자도 안전하다.** `autoresearch/experiments/promotion_gate.py`의
  `parse_criteria()`가 같은 fixture를 읽지만(`tests/test_experiment_promotion_gate.py`),
  `_LABELS`의 지표 필드만 뽑아 쓰므로 `허용 범위` 줄 수에 영향받지 않는다. 실행으로
  확인했다 — fixture 3줄 변경 후에도 해당 테스트 파일이 전부 통과한다.

## 결정 2 — 발행 경로는 `tools/` 아래 독립 CLI

세 후보를 검토했다.

| 후보 | 장점 | 기각 사유 |
| --- | --- | --- |
| `agent_orchestration` 신규 endpoint | `/chat`과 같은 `X-Orch-Token` 인증 재사용 | 상시 기동 중인 서비스 프로세스가 GitHub **쓰기** 권한을 상시 보유하게 된다 |
| **`tools/` 독립 CLI (채택)** | 실행 시점에만 토큰을 읽고 종료. dry-run 기본값. 폭주 위험 최소 | — |
| `workflow_dispatch` 워크플로 | Actions 토큰 사용 | 자연어 입력을 사람이 넣어야 해 자율성이 떨어진다 |

`tools/__init__.py`가 이미 있어 `from tools....` import가 가능하다. 렌더러는 순수
함수로 `tools/` 아래 두고, CLI가 그 출력을 `parse_issue_input()`에 넣어 자가 검증한
뒤 발행한다.

## 결정 3 — 토큰과 권한

- 필요한 권한은 **`issues: write` 하나뿐**이다. 차단 키 확인을 위해 열린 이슈 목록과
  본문을 **읽어야** 하지만, fine-grained PAT의 Issues 권한은 read/write가 한 쌍이라
  별도 read 권한이 필요 없다. 브랜치 생성은
  `.github/workflows/auto-research-issue-branch.yml`이 자신의 `contents: write`
  권한으로 수행하므로(`:10-12`), 발행 주체에 `contents` 권한을 주지 않는다.
- 새 환경 변수는 `.env.example`에 **빈 값 + 용도 주석**으로 등록하고, Agent
  Orchestration 절(`ORCH_API_TOKEN`·`ORCH_RUNNER_TOKEN` 블록)의 서술 관례를 따른다.
- 토큰 값을 로그·에러 메시지·PR 본문·테스트 fixture 어디에도 남기지 않는다. 실패
  보고에는 작업 이름과 정제된 엔드포인트만 포함한다.
- 필수 환경 변수를 새로 도입하므로 **같은 PR에서** `README.md`와
  `.claude/docs/agent-project-reference.md`를 갱신한다.

## 결정 4 — 폭주 방지

자동 발행은 `issues: [opened, labeled]` 워크플로를 즉시 트리거한다
(`.github/workflows/auto-research-issue-branch.yml:7-8`). 되돌릴 수 없는 부작용이
생기므로 세 겹으로 막는다.

1. **dry-run이 기본값이다.** 발행하려면 명시적 플래그가 필요하다.
2. **1회 실행당 발행 상한**을 둔다.
3. **동일 가설 재발행 차단 키**를 둔다. 키는 `연구 가설`(`hypothesis`)과
   `변경할 피처 · 모델`(`change`) **두 필드만** canonical JSON + SHA-256으로 묶은
   별도 해시다(`_identifier()` 재사용). 이미 그 키로 발행된 열린 이슈가 있으면 거부한다.

   > **`criteria_id`+`reproducibility_id` 조합을 쓰면 안 된다(리뷰 반영).** 두 해시의
   > 입력은 판정기준 6필드와 재현조건 6필드뿐이고 `연구 가설`·`변경할 피처 · 모델`은
   > **어느 쪽에도 들어가지 않는다.** 그 조합은 "동일 가설"이 아니라 "동일 판정기준 +
   > 동일 재현조건"을 식별하므로, 같은 스냅샷·시드·`roc_auc` 기준 위에서 피처만 바꿔
   > 반복하는 **이 폐루프의 정상 사용 패턴을 전부 중복으로 거부**하고, 반대로 가설을
   > 그대로 두고 시드만 하나 바꾸면 차단을 우회한다. 막아야 할 것을 통과시키고
   > 통과시켜야 할 것을 막는 방향이다.
   >
   > 한계도 기록한다 — 내용 해시이므로 같은 가설을 문구만 바꿔 쓰면 다른 키가 된다.
   > 정규화로 동일 판정을 넓히면 서로 다른 가설을 합쳐 버릴 위험이 커지므로 넓히지
   > 않는다.

라벨은 반드시 함께 부여한다. Issue Form을 우회해 API로 발행하면 label이 자동
적용되지 않는데, 워크플로 job은 `auto-research`와 `experiment`를 **동시에** 가질 때만
실행된다(`.github/workflows/auto-research-issue-branch.yml:16-18`).

## 결정 5 — 사전 검증은 placeholder 이슈 번호로 수행한다

`parse_issue_input()`은 내부에서 `branch_name_for()`를 호출하고, 이 함수는
`issue_number <= 0`이면 예외를 던진다. 발행 전에는 이슈 번호가 없으므로 사전 검증은
placeholder 번호로 한다.

**발행 후 재계산이 필요한 값은 `issue_branch` 하나뿐이다.** `criteria_id`와
`reproducibility_id`는 이슈 번호에 의존하지 않으므로 발행 전에 확정된다. 사전 검증
결과를 그대로 신뢰하고 **재계산 계약을 만들지 않는다** — 두 식별자에 이슈 번호를
섞는 구현은 이슈 본문이 바뀌지 않았는데도 ID가 달라지게 만들어 marker 봉인
재검증을 깨뜨린다.

제목 생성 규칙도 템플릿에 포함한다. **`[AR] ` prefix는 강제하지 않는다** — 저장소
계약 어디에도 요구가 없다. 워크플로는 제목을 검사하지 않고(job의 게이트는 label
두 개뿐이다), `branch_name_for()`는 prefix가 있으면 제거할 뿐 없다고 실패하지
않는다. Issue Form이 미리 채워 주는 관례이므로 따르기를 권하되, 발행 게이트로
승격시키지 않는다. 계약에 없는 제약을 도구가 만들면 그것이 곧 새로운 계약이 된다.

`branch_name_for()`가 `[AR] ` prefix를 제거한 뒤 slug를 만들므로, 제목에 ASCII
영소문자·숫자가 전혀 없으면 slug가 비어
`issue-<sha256 앞 12자>`로 대체된다. **이는 검증 실패가 아니라 가독성 문제다** —
그런 이름도 워크플로의 브랜치 정규식을 정상 통과한다. 사람이 브랜치 목록에서 실험을
식별할 수 있도록 제목에 ASCII slug 조각을 남기라는 것이 규정의 목적이다.

## 결정 6 — 검수용 발행물의 정리 계약

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
- 검수 이슈에는 `auto-research`·`experiment` 외에 구분용 label을 함께 붙이고, 제목과
  본문 첫 줄에 검수용임을 명시한다.
- 검수가 끝나면 이슈는 **close**하되 **exp 브랜치는 남긴다.** 지우면 위 fail-closed에
  걸리고, 남겨도 닫힌 이슈에 딸린 브랜치일 뿐 부작용이 없다.
- 이 절차를 지키지 않으면 저장소에 회수 불가능한 잔여물이 쌓인다.

## 결정 7 — `Experiment` lineage는 1급 컬럼으로 추가한다

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
- `models.py`의 모듈 docstring이 "Alembic 초기 migration과 동일한 table, server
  default, FK, index와 unique constraint를 제공한다"고 단언하므로, 모델과 migration을
  같은 커밋에서 갱신하고 docstring도 맞춘다.
- 이슈 1건이 실험 N건을 가질 수 있으므로 `issue_number`에 **unique 제약을 두지
  않는다.** 조회 성능을 위한 index는 둔다 — 대시보드(#338)가 이슈 기준으로 실험을
  묶어 조회하는 것이 주 사용처다.

## 작성 템플릿이 지켜야 할 제약

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
  `_required_content()`가 `strip()`한 뒤이므로 선행·후행 빈 줄은 이미 제거돼 있다.
  미체크는 불허를 뜻하므로 에이전트는 세 줄을 모두 명시적으로 출력한다.
- `보조 관측 지표`는 `required: false`이고 기본값이 없어, 사람이 비워 두면 GitHub이
  `_No response_`를 넣는다. 파서는 이 섹션을 검증 없이 담으므로 실패하지는 않지만,
  에이전트가 만드는 본문에서는 이 섹션을 **채우거나 heading 자체를 생략**한다.

## 범위 밖

- exp 브랜치를 checkout해 가설을 코드로 구현하고 candidate SHA를 만드는 실험
  실행기와 변형 큐·상태 머신 — #492
- 실험 종료 후 `### 결과 (에이전트가 채웁니다)` 섹션을 채우는 결과 보고 양식 — #494
- 지표 판정 엔진의 통합 — #493
- `repository_dispatch` 이벤트 발신자 부재
