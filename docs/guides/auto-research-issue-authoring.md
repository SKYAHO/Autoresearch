# 가이드 — 자연어 가설로 Auto Research 이슈 작성·발행하기

> **이 문서는 정본이 아니라 파생물입니다.** 필드 정본은
> `.github/ISSUE_TEMPLATE/auto_research.yml`이고, 파싱·검증 계약 정본은
> `tools/auto_research_issue_branch.py`의 `_HEADING_NAMES`, `_REQUIRED_SECTIONS`,
> `_COMPARISONS`, `_SNAPSHOT_REUSE`, `_SCOPE_LABELS`입니다. 이 문서와 정본이
> 어긋나면 **정본이 이깁니다.** 드리프트는
> `tests/test_auto_research_issue_authoring.py`가 잡습니다.
>
> 설계 근거는 `docs/specs/2026-08-03-auto-research-issue-authoring.md`입니다.

에이전트가 가설 한 줄에서 시작해 Auto Research 이슈를 직접 발행하는 절차입니다.
**전용 도구를 쓰지 않습니다** — 본문을 작성해 `gh issue create`로 올립니다.

## 흐름

1. 가설을 아래 20개 heading의 값으로 옮겨 본문 파일을 만든다.
2. `gh issue create`로 발행한다. label 두 개를 **반드시 함께** 붙인다.
3. 이슈가 열리는 순간 `.github/workflows/auto-research-issue-branch.yml`이
   본문을 검증하고, 통과하면 `dev` tip을 봉인해 `exp/<이슈번호>-<slug>` 브랜치를
   만든 뒤 marker 코멘트를 남긴다.

```bash
gh issue create \
  --title "[AR] views_per_day 비율 피처" \
  --label auto-research \
  --label experiment \
  --assignee @me \
  --body-file body.md
```

**label 두 개가 게이트입니다.** Issue Form을 쓰면 자동으로 붙지만 `gh issue create`는
Form을 우회하므로 직접 지정해야 합니다. 하나라도 빠지면 job의 `if:` 조건이
미충족되어 **skip**되는데, skip은 실패가 아니라 체크도 알림도 남지 않습니다. 즉
브랜치 생성이 **아무 흔적 없이 그냥 일어나지 않습니다.**

제목의 `[AR] ` prefix는 강제되지 않습니다(아래 「제목」 참조).

## 본문 형식

본문은 `### ` heading 20개로 이루어집니다. 순서는 아래와 같으며 Issue Form과
같습니다.

| heading | 필수 | 값 규칙 |
| --- | --- | --- |
| `연구 가설` | 필수 | 무엇이 왜 개선되는지. 공백만 있으면 거부 |
| `변경할 피처 · 모델` | 필수 | 코드로 옮길 수 있을 만큼 구체적으로 |
| `주 지표 이름` | 필수 | `^[A-Za-z][A-Za-z0-9._-]{0,63}$` |
| `주 지표 방향` | 필수 | `higher_is_better` 또는 `lower_is_better` |
| `최소 주 지표 개선폭` | 필수 | 유한한 0 이상 십진수 |
| `Guardrail 지표 이름` | 필수 | 지표 이름 규칙 또는 `없음` |
| `Guardrail 지표 방향` | 필수 | 위 두 방향 + `not_applicable` |
| `최대 Guardrail 악화폭` | 필수 | 유한한 0 이상 십진수 또는 `없음` |
| `보조 관측 지표` | 선택 | 채우거나 heading 자체를 생략 |
| `비교 대상` | 필수 | 정해진 3개 문자열 중 하나 |
| `데이터셋 스냅샷` | 필수 | 1~256자 |
| `랜덤 시드 목록` | 필수 | 쉼표로 구분한 중복 없는 0 이상 정수 |
| `Split 시드` | 필수 | 0 이상 정수 |
| `Test 비율` | 필수 | 0 초과 1 미만 |
| `Validation 비율` | 필수 | 0 초과 1 미만, `Test 비율`과 합이 1 미만 |
| `학습 설정 참조` | 필수 | 1~256자 |
| `대상 데이터 · 기간` | 필수 | 데이터셋 경로와 KST 기간 |
| `스냅샷 재사용` | 필수 | 정해진 2개 문자열 중 하나 |
| `허용 범위` | 필수 | 체크박스 3줄 (아래 참조) |
| `결과 (에이전트가 채웁니다)` | 선택 | 실험 종료 후 에이전트가 채움 |

작성 예시는 `tests/fixtures/auto_research_issue_form_rendered.md`가 정본입니다 —
이 파일이 GitHub이 실제로 렌더하는 본문과 같습니다. 새 본문을 쓸 때 이 파일을
복사해 값만 바꾸는 편이 가장 안전합니다.

## 지켜야 할 제약

### heading 문자열과 순서

- heading은 `### ` 뒤에 `_HEADING_NAMES` 키와 **완전히 동일한 문자열**이어야
  합니다. 알 수 없는 heading과 중복 heading은 즉시 `ValueError`입니다.
- `변경할 피처 · 모델`과 `대상 데이터 · 기간`의 가운뎃점은 **U+00B7**입니다.
  재타이핑하지 말고 정본에서 복사하세요. 렌더러를 쓰면 이 문제가 없습니다.
- 필드 **값** 안에 `### `로 시작하는 줄을 넣으면 heading이 하나 더 생겨
  본문 구조가 깨집니다. 렌더러가 조립 시점에 거부합니다.

```text
# 나쁜 예 — 값 안에 heading 줄
hypothesis = "가설 요약\n### 연구 가설\n(설명)"   → 중복 heading

# 나쁜 예 — 재타이핑한 가운뎃점
"### 변경할 피처 ㆍ 모델"                          → 알 수 없는 heading
```

### 필수 섹션

- 필수 섹션은 20개 heading에서 `보조 관측 지표`와
  `결과 (에이전트가 채웁니다)` 둘을 뺀 **18개**이며, 값이 공백이면 실패합니다.

### 지표

- `주 지표 이름`과 `Guardrail 지표 이름`은 `^[A-Za-z][A-Za-z0-9._-]{0,63}$`입니다.
  숫자로 시작하거나 공백·한글이 들어가면 거부됩니다.
- `주 지표 방향`은 `higher_is_better` 또는 `lower_is_better`입니다.
  `Guardrail 지표 방향`은 추가로 `not_applicable`을 허용합니다.
- Guardrail을 **쓰지 않으면 세 필드가 함께** `없음` / `not_applicable` / `없음`이어야
  합니다. 하나라도 어긋나면 거부됩니다.

```text
# 좋은 예 — guardrail 미사용
guardrail_metric_name        = "없음"
guardrail_metric_direction   = "not_applicable"
maximum_guardrail_regression = "없음"

# 좋은 예 — guardrail 사용
guardrail_metric_name        = "logloss"
guardrail_metric_direction   = "lower_is_better"
maximum_guardrail_regression = "0.001"

# 나쁜 예 — 세 필드 불일치
guardrail_metric_name        = "logloss"
guardrail_metric_direction   = "not_applicable"   → 거부
```

### 정확한 문자열 옵션

`비교 대상`은 다음 셋 중 하나여야 합니다 (괄호·공백까지 동일).

```text
동일 조건 baseline 재학습 (권장)
champion (ctr-model@champion)
둘 다
```

`스냅샷 재사용`은 다음 둘 중 하나입니다.

```text
허용 (진행하되 실제로 쓴 데이터를 결과에 명시)
불허 (정규 조립 경로 실패 시 중단)
```

### 시드와 split

- `랜덤 시드 목록`은 쉼표로 구분한 **중복 없는** 0 이상 정수이며 ASCII 숫자만
  허용합니다. `Split 시드`도 0 이상 정수입니다.
- `Test 비율`·`Validation 비율`은 각각 0 초과 1 미만이고 **합이 1 미만**이어야
  하며, Decimal과 float 두 경로 모두에서 이 조건을 만족해야 합니다.
- `데이터셋 스냅샷`과 `학습 설정 참조`는 1~256자입니다.

```text
# 나쁜 예
random_seeds    = "42, 42, 43"      → 중복 시드
random_seeds    = "４２"             → 전각 숫자
test_size       = "0.6"
validation_size = "0.5"             → 합이 1 이상, 학습 데이터가 남지 않음
```

### 허용 범위

- `허용 범위` 섹션은 **체크박스 줄만** 포함해야 합니다. 체크박스 3줄 **사이에**
  빈 줄이나 설명 줄을 넣지 마십시오 — 빈 줄 하나라도 거부됩니다. 섹션 앞뒤 개행은
  무관합니다(파서가 섹션 본문을 `strip()`한 뒤 검사하므로 선행·후행 빈 줄은 이미
  제거된 상태입니다).
- 각 줄의 label은 다음 세 문자열과 정확히 일치해야 합니다.

```text
- [ ] prod 모델 계약(`src/features/model_contract.py`) 수정을 허용한다
- [ ] Feast 정의(`feature_repo/`) 수정을 허용한다
- [ ] 실험 결과를 champion으로 승격하는 것까지 검토한다
```

- GitHub Issue Form의 `type: checkboxes`는 옵션을 **모두** 렌더하고 **미체크는
  불허**를 뜻하므로, 에이전트가 만드는 본문도 세 줄을 항상 출력합니다.
  렌더러에는 체크할 항목만 scope 키로 넘깁니다:
  `prod_model_contract`, `feast_definition`, `promotion`.

### 선택 섹션

- `보조 관측 지표`는 `required: false`이고 기본값이 없어, 사람이 비워 두면 GitHub이
  `_No response_`를 넣습니다. 파서는 이 섹션을 검증 없이 담으므로 실패하지는
  않지만, 에이전트가 만드는 본문에서는 **채우거나 heading 자체를 생략**합니다.
  렌더러는 값이 없으면 heading을 생략합니다.

### 제목

- 제목의 `[AR] ` prefix는 **강제되지 않습니다.** 워크플로는 제목을 검사하지 않고,
  `branch_name_for()`는 prefix가 있으면 제거할 뿐 없다고 실패하지 않습니다. Issue
  Form이 미리 채워 주는 관례이므로 따르기를 권하지만, 없어도 동작은 같습니다.
- `branch_name_for()`는 `[AR] ` prefix를 제거한 뒤 slug를 만들므로, 제목에 ASCII
  영소문자·숫자가 전혀 없으면 slug가 비어 `issue-<sha256 앞 12자>`로 대체됩니다.
  **이는 검증 실패가 아니라 가독성 문제입니다** — 그런 이름도 워크플로의 브랜치
  정규식을 통과합니다. 사람이 브랜치 목록에서 실험을 식별할 수 있도록 제목에
  ASCII slug 조각을 남기세요.

```text
[AR] views_per_day 비율 피처   → exp/501-views-per-day
[AR] 비율 피처 실험            → exp/501-issue-3f2a1c9d8b7e (읽기 어려움)
```

## 발행 전에 확인하고 싶다면

필수는 아니지만, 파서를 직접 돌려 본문이 통과하는지 미리 볼 수 있습니다. 이슈
번호는 아직 없으므로 아무 양수나 placeholder로 넣습니다 — `criteria_id`와
`reproducibility_id`는 이슈 번호에 의존하지 않아 이 값이 발행 후에도 그대로입니다.

```console
$ python tools/auto_research_issue_branch.py \
    --issue-number 1 --issue-title "[AR] test" --issue-body-file body.md
issue_branch=exp/1-test
criteria_id=1ae256dd8c58...
reproducibility_id=315f6fc3abe7...
```

`exit 0`이면 그 본문은 워크플로에서도 통과합니다. 실패하면 사유가 stderr에
나옵니다.

## 잘못 발행했을 때

본문이 계약을 어기면 워크플로의 **검증 step이 브랜치 생성 step보다 먼저** 돌아
fail-closed됩니다. 즉 **브랜치는 만들어지지 않고** 실패한 run만 남습니다.

복구는 이렇게 합니다.

1. 이슈 본문을 고친다.
2. label을 뗐다 다시 붙인다 — `labeled` 이벤트가 워크플로를 다시 트리거한다.
3. marker가 아직 없으므로 워크플로가 `dev`의 현재 tip을 새로 봉인하고 브랜치를
   만든다.

**marker 코멘트가 이미 남았다면 이야기가 다릅니다.** 그때는 exp 브랜치를 지우지
마십시오 — 브랜치만 지우면 `recorded issue branch ref is missing`으로 fail-closed되고,
marker까지 함께 지우면 **다른 기준선으로 조용히 재생성**되어 이미 원래
`base_dev_sha`를 인용한 곳과 아무 실패 없이 어긋납니다.

검수용으로 발행한 이슈는 **close하되 exp 브랜치는 남깁니다.**
