# AutoResearch GitHub 협업 운영 가이드

## Notion 속성 추천

| Property | Value |
|---|---|
| 문서 유형 | Team Wiki / How-to |
| 상태 | Active |
| 담당 | Repository Maintainer |
| 태그 | GitHub, Issue, Pull Request, Projects, Workflow |
| 기준 저장소 | `SKYAHO/Autoresearch` |
| 기준 Project | `SKYAHO / Autoresearch` |
| 마지막 업데이트 | 2026-06-29 |

## 한 줄 요약

AutoResearch는 **Issue로 작업을 정의하고, Branch에서 구현하고, PR로 리뷰/병합하며, GitHub Projects로 상태를 추적**한다.

```text
Issue 생성 -> Project Todo -> Branch 생성 -> 작업 -> PR 생성 -> Review -> Merge -> Project Done
```

## 현재 운영 상태

### Issue Forms

| Form 파일 | 제목 prefix | 자동 label | 사용 상황 |
|---|---|---|---|
| `bug.yml` | `[BUG]` | `bug` | 오류, 장애, 기대와 다른 동작 |
| `feature.yml` | `[FEAT]` | `feature` | 새 기능, 기능 개선 |
| `experiment.yml` | `[EXP]` | `experiment` | 모델, 데이터, 지표, 방법론 실험 |

- 빈 issue 생성은 비활성화되어 있다.
- 팀원은 repository의 `Issues > New issue` 화면에서 form 중 하나를 선택한다.
- GitHub는 `label 선택 -> template 변경`이 아니라 `form 선택 -> label 자동 적용` 방식으로 동작한다.
- Project의 `Add item`에서 제목만 바로 추가하면 form을 우회할 수 있으므로 새 작업은 Issues 화면에서 만든다.

### Project View

| View | 목적 |
|---|---|
| Assignee | 담당자별 table view |
| Board | `Todo`, `In Progress`, `Done` 칸반 보드 |
| View 4 | Roadmap view. 날짜 필드 설정 후 사용 권장 |

### Project 자동화

| Workflow | 상태 | 동작 |
|---|---|---|
| Auto-add to project | On | open issue/PR을 Project에 자동 추가 |
| Item added to project | On | 새로 추가된 issue/PR을 `Todo`로 설정 |
| Item closed | On | 닫힌 issue/PR을 `Done`으로 설정 |
| Pull request merged | On | merge된 PR을 `Done`으로 설정 |
| Auto-close issue | On | Project에서 issue를 `Done`으로 옮기면 issue close |

현재 자동 추가 필터:

```text
is:issue,pr is:open
```

## 팀 작업 원칙

1. 코드 작업은 가능한 한 issue에서 시작한다.
2. branch 이름에는 issue 번호를 포함한다.
3. PR 본문에는 `Closes #이슈번호`를 넣는다.
4. PR은 작게 유지한다.
5. 리뷰 중 나온 별도 작업은 새 issue로 분리한다.
6. merge는 Squash and merge를 기본으로 한다.
7. Project는 현재 상태를 보여주는 보드로 사용한다.

## 언제 Issue를 만드는가

다음 경우 issue를 만든다.

- 새 기능을 추가해야 할 때
- 버그를 발견했을 때
- 실험을 계획하거나 결과를 남겨야 할 때
- 문서, 설정, 리팩터링처럼 추적 가능한 작업이 생겼을 때
- PR 리뷰 중 후속 작업이 생겼지만 현재 PR 범위를 벗어날 때
- 작업 범위가 커져 여러 PR로 나누어야 할 때

아주 작은 오타 수정은 바로 PR로 처리할 수 있다. 다만 팀 기록과 협업 연습을 우선한다면 작은 작업도 issue로 남긴다.

## Issue 작성법

### Feature

제목 예시:

```text
[FEAT] 덧셈 함수 추가
```

form 필수 내용:

- 목적
- 작업 범위
- 영향받는 컴포넌트
- 완료 조건

### Bug

제목 예시:

```text
[BUG] 로그인 실패 시 에러 메시지 누락
```

form 필수 내용:

- 현상
- 재현 방법
- 기대 동작
- 실행 환경
- 로그 또는 에러 메시지

### Experiment

제목 예시:

```text
[EXP] LightGBM baseline 성능 비교
```

form 필수 내용:

- 가설
- 데이터셋
- 모델
- 피처
- 평가지표
- Champion 대비 결과
- 결론

## Branch 컨벤션

항상 `main`을 최신화한 뒤 branch를 만든다.

```bash
git switch main
git pull origin main
git switch -c feat/12-add-math-utils
```

| 작업 유형 | 형식 | 예시 |
|---|---|---|
| 기능 | `feat/이슈번호-간략한-설명` | `feat/12-add-math-utils` |
| 버그 | `fix/이슈번호-간략한-설명` | `fix/18-handle-empty-input` |
| 실험 | `exp/이슈번호-간략한-설명` | `exp/21-lgbm-baseline` |
| 문서 | `docs/이슈번호-간략한-설명` | `docs/7-github-workflow-guide` |
| 리팩터링 | `refactor/이슈번호-간략한-설명` | `refactor/15-split-training-module` |
| 기타 | `chore/이슈번호-간략한-설명` | `chore/3-setup-ci` |

규칙:

- 영어 소문자, 숫자, 하이픈만 사용한다.
- issue 번호를 반드시 포함한다.
- 한 branch에는 하나의 주요 목적만 담는다.

## Commit 컨벤션

형식:

```text
<type>: <한국어 설명>
```

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

커밋 원칙:

- 한 커밋은 하나의 논리적 변경만 담는다.
- 제목은 짧고 명확하게 쓴다.
- 기능 변경과 불필요한 포맷 변경을 섞지 않는다.

## PR 컨벤션

좋은 PR의 조건:

- 하나의 issue를 해결한다.
- 제목만 봐도 변경 목적이 드러난다.
- 본문에 `Closes #이슈번호`가 있다.
- 변경 사항이 bullet list로 정리되어 있다.
- 테스트 또는 검증 명령이 적혀 있다.
- 아직 리뷰 준비가 안 되었으면 Draft PR로 둔다.

PR 제목 예시:

```text
feat: 덧셈 함수 추가
```

PR 본문 필수 연결:

```md
Closes #12
```

## 작업자 체크리스트

작업 시작 전:

- [ ] GitHub issue를 생성했다.
- [ ] issue가 Project `Todo`에 들어왔는지 확인했다.
- [ ] `main`을 최신화했다.
- [ ] issue 번호 기반 branch를 만들었다.

작업 중:

- [ ] Project 상태를 `In Progress`로 옮겼다.
- [ ] 변경 범위를 작게 유지했다.
- [ ] 필요한 테스트를 추가했다.
- [ ] 로컬 검증 명령을 실행했다.

PR 생성 전:

- [ ] 불필요한 파일, 캐시, 시크릿이 없는지 확인했다.
- [ ] 커밋 메시지가 컨벤션을 따른다.
- [ ] PR 본문에 `Closes #이슈번호`를 넣었다.
- [ ] 검증 명령과 결과를 PR에 적었다.

Merge 전:

- [ ] 리뷰 approve를 받았다.
- [ ] 필요한 conversation이 해결되었다.
- [ ] 충돌이 없다.
- [ ] Draft PR이 아니라 Ready for review 상태다.

## 전체 작업 예시

```bash
# 1. main 최신화
git switch main
git pull origin main

# 2. issue #12 작업 branch 생성
git switch -c feat/12-add-math-utils

# 3. 작업 후 테스트
python -m unittest discover

# 4. 커밋
git add autoresearch tests
git commit -m "feat: math utils 추가"

# 5. push
git push -u origin feat/12-add-math-utils
```

PR 본문 예시:

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

## Merge 후 자동 흐름

```text
PR merge
-> PR Status: Done
-> Closes #issue 로 issue close
-> Issue Status: Done
```

merge 직후 Project에서 항목이 사라진 것처럼 보이면 `Done` 컬럼을 먼저 확인한다.

## 추천 GitHub 저장소 설정

### Merge 설정

- [ ] Allow squash merging: 켜기
- [ ] Allow merge commits: 끄기
- [ ] Allow rebase merging: 끄기
- [ ] Automatically delete head branches: 켜기

### main branch 보호

- [ ] 직접 push 금지
- [ ] PR을 통한 변경만 허용
- [ ] 최소 1명 approve 필요
- [ ] conversation resolved 필요
- [ ] status check가 생기면 통과 필수

private repository에서는 GitHub 플랜에 따라 일부 보호 기능이 제한될 수 있다. 설정으로 강제할 수 없는 경우에도 팀 규칙으로 동일하게 운영한다.

### CODEOWNERS 정리

현재 `.github/CODEOWNERS`의 placeholder를 실제 팀원 계정이나 GitHub team으로 교체한다.

예시:

```text
/.github/ @SKYAHO/platform-maintainers
/autoresearch/ @SKYAHO/backend
/tests/ @SKYAHO/qa
```

## 문제 해결

### PR이 merge되지 않을 때

확인할 것:

- PR이 Draft 상태인지 확인한다.
- 리뷰 approve가 있는지 확인한다.
- 충돌이 있는지 확인한다.
- required check가 실패했는지 확인한다.

Draft PR은 approve를 받아도 merge할 수 없다. `Ready for review`로 바꾼 뒤 merge한다.

### Project에 항목이 안 보일 때

확인할 것:

- `Done` 컬럼에 있는지 확인한다.
- view filter가 걸려 있는지 확인한다.
- issue/PR이 open 상태일 때 Project에 자동 추가되었는지 확인한다.
- 이미 closed/merged된 항목은 `Auto-add` 필터에 걸리지 않을 수 있다.

### Issue가 자동으로 닫히지 않을 때

확인할 것:

- PR 본문에 `Closes #이슈번호`가 있는지 확인한다.
- PR이 default branch인 `main`으로 merge되었는지 확인한다.
- issue 번호가 같은 repository의 번호인지 확인한다.

## 참고 링크

- Repository: https://github.com/SKYAHO/Autoresearch
- Project Board: https://github.com/orgs/SKYAHO/projects/3/views/2
- Issue Forms: `.github/ISSUE_TEMPLATE/*.yml`
- PR template: `.github/PULL_REQUEST_TEMPLATE.md`
- CODEOWNERS: `.github/CODEOWNERS`
- GitHub Docs - Issue templates: https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/configuring-issue-templates-for-your-repository
- GitHub Docs - Issue forms syntax: https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/syntax-for-issue-forms
- GitHub Docs - PR templates: https://docs.github.com/en/communities/using-templates-to-encourage-useful-issues-and-pull-requests/creating-a-pull-request-template-for-your-repository
- GitHub Docs - Link PR to issue: https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue
- GitHub Docs - Project auto-add: https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/adding-items-automatically
- GitHub Docs - Project automations: https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-built-in-automations
