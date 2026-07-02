# GitHub 운영 가이드

이 문서는 AutoResearch 저장소에서 issue, branch, pull request, GitHub Projects를 어떻게 함께 사용할지 정리한 팀 운영 가이드입니다.

- 기준 저장소: `SKYAHO/Autoresearch`
- 기준 Project: `SKYAHO / Autoresearch`
- 확인일: 2026-06-29

## 핵심 원칙

```text
Issue 생성 -> Branch 생성 -> 작업 -> PR 생성 -> Review -> Merge -> Project Done
```

- **Issue는 작업의 시작점**입니다. 기능, 버그, 실험, 문서, 리팩터링처럼 추적해야 하는 작업은 먼저 issue로 만듭니다.
- **Branch는 issue 번호를 포함**합니다. 어떤 작업을 위한 브랜치인지 추적하기 쉽게 만듭니다.
- **PR은 issue를 닫는 단위**입니다. PR 본문에 `Closes #이슈번호`를 넣습니다.
- **Projects는 상태판**입니다. issue와 PR이 지금 `Todo`, `In Progress`, `Done` 중 어디에 있는지 보여줍니다.

## 현재 GitHub 설정

### Issue

현재 `.github/ISSUE_TEMPLATE`에는 3개 Issue Forms가 있습니다.

| Form 파일 | 제목 prefix | 자동 label | 사용 상황 |
|---|---|---|---|
| `bug.yml` | `[BUG]` | `bug` | 오류, 장애, 기대와 다른 동작 |
| `feature.yml` | `[FEAT]` | `feature` | 새 기능, 기능 개선 |
| `experiment.yml` | `[EXP]` | `experiment` | 모델, 데이터, 지표, 방법론 실험 |

`blank_issues_enabled: false`이므로 빈 issue는 만들 수 없습니다. 팀원은 반드시 `New issue` 화면에서 Bug / Feature / Experiment form 중 하나를 선택해야 합니다.

GitHub의 기본 흐름은 `label 선택 -> template 변경`이 아니라 `form 선택 -> label 자동 적용`입니다. Project의 `Add item`에서 제목만 바로 추가하면 form을 우회할 수 있으므로, 새 작업은 repository의 Issues 화면에서 생성하는 것을 권장합니다.

### Pull Request

현재 `.github/PULL_REQUEST_TEMPLATE.md`에는 다음 항목이 있습니다.

- 작업 내용
- 변경 사항
- 관련 이슈: `Closes #`
- 체크리스트
- 리뷰어 참고사항

PR을 만들 때 관련 issue를 닫으려면 본문에 아래처럼 작성합니다.

```md
Closes #12
```

### GitHub Actions CI

`.github/workflows/ci.yml`은 일반 코드 검증용 workflow입니다.

실행 조건:

- `pull_request`
- `main` 브랜치 `push`
- 수동 실행(`workflow_dispatch`)

검증 항목:

- Python 3.11에서 `python -m pytest`
- Python 3.12에서 `python -m pytest`
- `Dockerfile` 기반 이미지 빌드
- 빌드된 Docker 이미지의 import smoke check

이번 CI는 실제 GKE 배포를 수행하지 않습니다. GKE 배포 단계는 GCP project, Artifact Registry repository, GKE cluster, workload identity 또는 service account 전략이 정해진 뒤 별도 issue/PR에서 추가합니다.

### Claude Code PR Review

`.github/workflows/claude.yml`은 Claude 기반 PR 리뷰 workflow입니다.

자동 실행 조건:

- PR이 처음 열림(`opened`)
- Draft PR을 Ready for review로 전환(`ready_for_review`)

수동 재리뷰 요청:

- PR 피드백을 반영한 뒤 같은 PR의 최신 diff를 다시 리뷰받고 싶으면 PR conversation에 `/claude-review` 댓글을 작성합니다.
- `/claude-review` 댓글은 PR에서만 동작하며, 일반 issue 댓글에서는 Claude review job을 실행하지 않습니다.
- push마다 자동 재리뷰되는 `synchronize` 이벤트는 비용과 노이즈를 줄이기 위해 사용하지 않습니다.

주의 사항:

- Actions 탭의 `workflow_dispatch`는 workflow를 수동 실행할 수 있게 열어두지만, PR 재리뷰 요청은 `/claude-review` 댓글을 기본 방식으로 사용합니다.
- Claude review가 남긴 inline thread를 반영한 뒤에는 `/claude-review`로 최신 변경분에 대한 추가 검토를 요청합니다.

### GitHub Project

현재 Project에는 다음 view가 있습니다.

| View | 용도 |
|---|---|
| `Assignee` | 담당자별 table view |
| `Board` | `Todo`, `In Progress`, `Done` 칸반 보드 |
| `View 4` | Roadmap view. 날짜 필드 설정이 필요할 수 있음 |

현재 켜진 workflow는 다음과 같습니다.

| Workflow | 동작 |
|---|---|
| `Auto-add to project` | `Autoresearch` repo의 open issue/PR을 Project에 자동 추가 |
| `Item added to project` | Project에 추가된 issue/PR의 `Status`를 `Todo`로 설정 |
| `Item closed` | issue/PR이 닫히면 `Status`를 `Done`으로 설정 |
| `Pull request merged` | PR이 merge되면 `Status`를 `Done`으로 설정 |
| `Auto-close issue` | Project에서 issue의 `Status`를 `Done`으로 바꾸면 issue를 close |

현재 `Auto-add to project` 필터는 다음과 같습니다.

```text
is:issue,pr is:open
```

## 언제 Issue를 만드는가

다음 상황에서는 issue를 먼저 만듭니다.

- 새 기능을 추가할 때
- 버그를 발견했을 때
- 실험을 계획하거나 결과를 기록해야 할 때
- 문서, 설정, 리팩터링처럼 추적 가능한 작업이 생겼을 때
- PR 리뷰 중 후속 작업이 생겼지만 현재 PR 범위를 벗어날 때
- 작업 범위가 커져서 여러 PR로 나누어야 할 때

아주 작은 오타 수정처럼 독립 추적이 필요 없는 작업은 바로 PR로 처리할 수 있습니다. 다만 팀 협업 연습과 기록을 우선한다면 작은 작업도 issue로 남기는 것을 권장합니다.

## Issue 작성 규칙

### Feature issue

기능 추가나 개선은 `Feature` form을 사용합니다.

제목:

```text
[FEAT] 덧셈 함수 추가
```

form에서 최소한 다음을 작성합니다.

- 목적: 왜 필요한가
- 작업 범위: 무엇을 할 것인가
- 영향받는 컴포넌트
- 완료 조건

### Bug issue

버그는 `Bug` form을 사용합니다.

제목:

```text
[BUG] 로그인 실패 시 에러 메시지 누락
```

form에서 최소한 다음을 작성합니다.

- 현상
- 재현 방법
- 기대 동작
- 실행 환경
- 로그 또는 에러 메시지

### Experiment issue

실험은 `Experiment` form을 사용합니다.

제목:

```text
[EXP] LightGBM baseline 성능 비교
```

form에서 최소한 다음을 작성합니다.

- 가설
- 데이터셋
- 모델
- 피처
- 평가지표
- Champion 대비 결과
- 결론

## Branch 컨벤션

브랜치는 항상 `main` 최신 상태에서 생성합니다.

```bash
git switch main
git pull origin main
git switch -c feat/12-add-math-utils
```

브랜치 이름은 아래 형식을 따릅니다.

| 작업 유형 | 형식 | 예시 |
|---|---|---|
| 기능 | `feat/이슈번호-간략한-설명` | `feat/12-add-math-utils` |
| 버그 | `fix/이슈번호-간략한-설명` | `fix/18-handle-empty-input` |
| 실험 | `exp/이슈번호-간략한-설명` | `exp/21-lgbm-baseline` |
| 문서 | `docs/이슈번호-간략한-설명` | `docs/7-github-workflow-guide` |
| 리팩터링 | `refactor/이슈번호-간략한-설명` | `refactor/15-split-training-module` |
| 기타 | `chore/이슈번호-간략한-설명` | `chore/3-setup-ci` |

규칙:

- 영어 소문자, 숫자, 하이픈만 사용합니다.
- issue 번호를 반드시 포함합니다.
- 한 브랜치에는 하나의 주요 목적만 담습니다.

## Commit 컨벤션

커밋 메시지는 다음 형식을 권장합니다.

```text
<type>: <한국어 설명>
```

사용 type:

| type | 의미 |
|---|---|
| `feat` | 새 기능 |
| `fix` | 버그 수정 |
| `exp` | 실험 코드 또는 실험 설정 |
| `docs` | 문서 |
| `refactor` | 기능 변화 없는 구조 개선 |
| `test` | 테스트 추가 또는 수정 |
| `chore` | 빌드, 설정, 패키지, CI 등 |

예시:

```text
feat: 덧셈 함수 추가
fix: 빈 입력 처리 오류 수정
docs: GitHub 운영 가이드 추가
test: math utils 테스트 추가
chore: Python 캐시 파일 제외
```

커밋 작성 원칙:

- 한 커밋은 하나의 논리적 변경만 담습니다.
- 제목은 50자 안팎으로 짧게 씁니다.
- 리뷰가 쉬워지도록 불필요한 포맷 변경과 기능 변경을 섞지 않습니다.

## 작업 흐름

### 1. Issue 생성

GitHub repository의 `Issues` 탭에서 `New issue`를 누르고 작업에 맞는 form을 선택합니다.

예:

```text
[FEAT] 덧셈 함수 추가
```

issue가 생성되면 선택한 form의 label이 자동 적용됩니다. open issue 자동 추가 필터에 따라 Project에도 추가되고 `Status: Todo`가 설정됩니다.

### 2. Branch 생성

issue 번호를 확인한 뒤 브랜치를 생성합니다.

```bash
git switch main
git pull origin main
git switch -c feat/12-add-math-utils
```

### 3. 작업과 테스트

작업 후 관련 테스트를 실행합니다.

```bash
python -m unittest discover
```

새 기능이나 버그 수정은 가능하면 테스트를 먼저 작성합니다.

### 4. Commit

```bash
git status
git add <변경 파일>
git commit -m "feat: 덧셈 함수 추가"
```

### 5. Push

```bash
git push -u origin feat/12-add-math-utils
```

### 6. PR 생성

PR 제목은 커밋 컨벤션과 비슷하게 작성합니다.

```text
feat: 덧셈 함수 추가
```

PR 본문에는 반드시 관련 issue를 연결합니다.

```md
Closes #12
```

PR이 생성되면 Project에 자동으로 추가되고 `Status: Todo`가 설정됩니다.

### 7. Review

리뷰어는 다음을 확인합니다.

- issue의 목적과 PR 변경이 일치하는가
- 변경 범위가 너무 크지 않은가
- 테스트 또는 검증 방법이 충분한가
- `Closes #이슈번호`가 있는가
- 불필요한 파일, 캐시, 시크릿이 포함되지 않았는가

### 8. Merge

권장 merge 방식은 **Squash and merge**입니다.

merge 후 기대되는 자동화:

```text
PR merge
-> PR Status: Done
-> Closes #issue 로 issue close
-> Issue Status: Done
```

현재 저장소 설정에서는 merge commit, rebase merge도 허용되어 있을 수 있습니다. 팀 운영 규칙은 Squash merge를 기본으로 삼고, GitHub 설정에서 가능하면 다른 merge 방식을 비활성화하는 것을 권장합니다.

## Project 상태 운영

### Todo

아직 작업을 시작하지 않은 issue/PR입니다.

자동 추가된 issue와 PR은 기본적으로 `Todo`로 들어옵니다.

### In Progress

실제로 작업 중인 항목입니다.

팀원이 브랜치를 따고 작업을 시작하면 Project에서 직접 `In Progress`로 옮깁니다.

### Done

완료된 issue/PR입니다.

다음 경우 자동으로 `Done`이 됩니다.

- PR이 merge됨
- issue 또는 PR이 close됨
- Project에서 issue의 `Status`를 `Done`으로 직접 변경함

## Label 컨벤션

현재 사용 중이거나 권장하는 label은 다음과 같습니다.

| label | 사용 기준 |
|---|---|
| `feature` | 기능 추가 또는 개선 |
| `bug` | 버그 수정 |
| `experiment` | 실험, 검증, 모델 비교 |
| `documentation` | 문서 작업 |
| `enhancement` | 기존 기능 개선. `feature`와 겹치면 `feature`를 우선 사용 |
| `good first issue` | 신규 팀원이 시작하기 쉬운 작업 |
| `help wanted` | 담당자나 추가 논의가 필요한 작업 |
| `question` | 결정이 필요한 질문성 issue |
| `chore` | 필요하면 추가 권장. 설정, 정리, CI 같은 작업 |
| `refactor` | 필요하면 추가 권장. 기능 변화 없는 구조 개선 |

Issue Forms와 자동화를 단순하게 유지하려면 `feature`, `bug`, `experiment`를 우선 사용합니다.

## PR 컨벤션

좋은 PR은 다음 조건을 만족합니다.

- 하나의 issue를 해결합니다.
- 제목만 봐도 변경 목적이 드러납니다.
- 본문에 `Closes #이슈번호`가 있습니다.
- 변경 사항이 bullet list로 정리되어 있습니다.
- 테스트 또는 검증 명령이 적혀 있습니다.
- draft PR은 아직 리뷰 준비가 안 된 상태일 때만 사용합니다.

PR 크기는 작게 유지합니다.

- 작은 PR: 리뷰가 빠르고 안전합니다.
- 큰 PR: 리뷰가 느려지고 버그를 놓치기 쉽습니다.

권장 기준:

- 가능하면 PR 하나는 한 가지 목적만 다룹니다.
- unrelated refactor와 기능 변경을 섞지 않습니다.
- 리뷰 중 발견된 별도 작업은 새 issue로 분리합니다.

## 추천 GitHub 저장소 설정

가능하면 GitHub repository settings에서 다음을 권장합니다.

### Merge 설정

- `Allow squash merging`: 켜기
- `Allow merge commits`: 끄기
- `Allow rebase merging`: 끄기
- `Automatically delete head branches`: 켜기

### Branch protection 또는 ruleset

`main` 브랜치에는 다음 규칙을 권장합니다.

- 직접 push 금지
- PR을 통한 변경만 허용
- 최소 1명 approve 필요
- conversation resolved 필요
- status check가 생기면 통과 필수
- 가능하면 branch 최신화 후 merge

private repository에서는 일부 보호 기능이 GitHub 플랜에 따라 제한될 수 있습니다. 설정으로 강제할 수 없는 경우에도 팀 규칙으로 동일하게 운영합니다.

### CODEOWNERS

현재 `.github/CODEOWNERS`는 placeholder 상태입니다.

```text
@member1
@member2
```

실제 팀원 계정이나 GitHub team으로 교체해야 자동 reviewer 지정이 의미 있게 동작합니다.

예:

```text
/.github/ @SKYAHO/platform-maintainers
/autoresearch/ @SKYAHO/backend
/tests/ @SKYAHO/qa
```

## 예시: 기능 개발 전체 흐름

```bash
# 1. main 최신화
git switch main
git pull origin main

# 2. issue #12 작업 브랜치 생성
git switch -c feat/12-add-math-utils

# 3. 작업 후 테스트
python -m unittest discover

# 4. 커밋
git add autoresearch tests
git commit -m "feat: math utils 추가"

# 5. push
git push -u origin feat/12-add-math-utils
```

PR 본문:

```md
## 작업 내용

math utils에 기본 계산 함수를 추가했습니다.

## 변경 사항

- `add` 함수 추가
- 단위 테스트 추가

## 관련 이슈

Closes #12

## 리뷰어 참고사항

- 검증 명령: `python -m unittest discover`
```

## 자주 생기는 문제

### PR이 merge되지 않는 경우

확인할 것:

- PR이 draft 상태인지 확인합니다.
- 리뷰 approve가 있는지 확인합니다.
- 충돌이 있는지 확인합니다.
- required check가 실패했는지 확인합니다.

Draft PR은 approve를 받아도 merge할 수 없습니다. `Ready for review`로 바꾼 뒤 merge합니다.

### Project에 안 보이는 경우

확인할 것:

- Board의 `Done` 컬럼에 있는지 확인합니다.
- view filter가 걸려 있는지 확인합니다.
- issue/PR이 open 상태일 때 Project에 자동 추가되었는지 확인합니다.
- 이미 closed/merged된 항목은 `Auto-add` 필터(`is:issue,pr is:open`)에 걸리지 않을 수 있습니다.

### Issue가 자동으로 닫히지 않는 경우

확인할 것:

- PR 본문에 `Closes #이슈번호`가 있는지 확인합니다.
- PR이 default branch인 `main`으로 merge되었는지 확인합니다.
- issue 번호가 같은 repository의 번호인지 확인합니다.

## 참고 문서

- GitHub Docs: [Configuring issue templates](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/configuring-issue-templates-for-your-repository)
- GitHub Docs: [Issue forms syntax](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-issue-forms)
- GitHub Docs: [Creating a pull request template](https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/creating-a-pull-request-template-for-your-repository)
- GitHub Docs: [Linking a pull request to an issue](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue)
- GitHub Docs: [Adding items automatically to Projects](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/adding-items-automatically)
- GitHub Docs: [Using built-in Project automations](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-built-in-automations)
