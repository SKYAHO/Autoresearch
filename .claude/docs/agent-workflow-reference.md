# Agent Workflow Reference

> Last Updated: 2026-07-06

GitHub 워크플로우 전체 가이드: Issue → Branch → Commit → PR → Review →
Merge. 모든 기능 작업의 운영 표준입니다. 사람용 요약은
`CONTRIBUTING.md`에 있으며, 두 문서의 규칙은 항상 일치해야 합니다.

## When To Use This Doc

- 새 기능이나 버그 수정을 시작하며 전체 워크플로우가 필요할 때
- 커밋 메시지나 PR 본문을 작성할 때
- PR이 워크플로우를 따르는지 검증할 때
- 브랜치 이름, 머지 방식, Project 운영이 헷갈릴 때

## Workflow Overview

```
Issue 생성 (Project Todo 자동 추가)
    ↓
Branch 생성 (이슈의 Create a branch, feat/이슈번호-설명)
    ↓
Commit (<type>: 한국어 설명)
    ↓
PR 생성 (Draft 또는 Ready)
    ↓
Review → Approve 2명 → Squash Merge
    ↓
Issue 자동 close → Project Done
```

## Issue Creation

**이슈를 만드는 경우:**
- 새 기능 또는 개선
- 버그 발견
- 실험 계획 또는 결과 기록
- 문서, 설정, 리팩터링 등 추적이 필요한 작업
- PR 리뷰 중 생긴 범위 밖 후속 작업

아주 작은 오타 수정은 바로 PR로 처리할 수 있습니다.

**Issue Forms** (`.github/ISSUE_TEMPLATE/`, 빈 이슈 생성 불가):

| Form | 제목 prefix | 자동 label | 필수 내용 |
|---|---|---|---|
| `feature.yml` | `[FEAT]` | `feature` | 목적, 작업 범위, 영향 컴포넌트, 완료 조건 |
| `bug.yml` | `[BUG]` | `bug` | 현상, 재현 방법, 기대 동작, 환경, 로그 |
| `experiment.yml` | `[EXP]` | `experiment` | 가설, 데이터셋, 모델, 피처, 평가지표, Champion 대비 결과, 결론 |

GitHub는 `form 선택 → label 자동 적용` 방식으로 동작합니다. Project의
`Add item`으로 제목만 추가하면 form을 우회하므로, 새 작업은 Issues
화면에서 생성합니다.

## Branch Naming

**코드가 변경되는 작업은 반드시 이슈를 먼저 발행하고, 그 이슈에서 브랜치를
생성합니다.** GitHub 이슈 우측 `Development > Create a branch`를 사용하면
브랜치가 이슈에 자동 연결(`main` 기준 분기)되어, PR을 `main`으로 머지할 때
이슈 자동 close와 Project `Done` 전환이 확실해집니다. 로컬에서 임의로 분기하는
대신 이슈에서 만든 브랜치를 체크아웃해 작업합니다.

**형식:** `<type>/<이슈번호>-<간략한-설명>`

**Type:** `feat/`, `fix/`, `exp/`, `docs/`, `refactor/`, `chore/`

- 영어 소문자, 숫자, 하이픈만 사용합니다.
- 이슈 번호를 반드시 포함합니다.
- 한 브랜치에는 하나의 주요 목적만 담습니다.

```bash
# 이슈에서 Create a branch로 생성(예: feat/45-docs-system-phase1) 후
git fetch origin
git switch feat/45-docs-system-phase1
```

## Commit Messages

**형식:** `<type>: <한국어 설명>`

| type | 의미 |
|---|---|
| `feat` | 새 기능 |
| `fix` | 버그 수정 |
| `exp` | 실험 코드 또는 실험 설정 |
| `docs` | 문서 |
| `refactor` | 기능 변화 없는 구조 개선 |
| `test` | 테스트 추가 또는 수정 |
| `chore` | 빌드, 설정, 패키지, CI 등 |

**규칙:**
1. 한 커밋에는 하나의 논리적 변경만 담습니다.
2. 포맷 변경과 기능 변경을 섞지 않습니다.
3. 제목은 현재형 동사로 50자 이내로 씁니다.

```text
feat: CLAUDE.md 라우팅 표 추가
test: config 로딩 단위 테스트 추가
docs: 아키텍처 개요 갱신
```

## PR Creation

**PR 생성 전 체크:**
- [ ] 테스트 통과: `python -m pytest -v`
- [ ] 시크릿, `.env`, 데이터 파일이 포함되지 않았다
- [ ] 커밋 메시지가 컨벤션을 따른다
- [ ] PR 라벨을 1개 이상 부착했다 (아래 매핑 참조)

**PR 라벨 (Release Drafter 연동):**

Release Drafter가 라벨 기반으로 release note 분류와 semantic version을
자동 계산합니다. 라벨이 없으면 `Other Changes`(patch)로 분류되므로, 변경
성격에 맞는 라벨을 반드시 부착합니다.

| 라벨 | 분류 | 버전 영향 |
|---|---|---|
| `feature`, `enhancement` | Features | minor |
| `bug` | Bug Fixes | patch |
| `breaking` | Breaking Changes | major |
| `documentation` | Documentation | 없음 |
| `experiment` | Experiments | 없음 |

**PR 본문** (`.github/PULL_REQUEST_TEMPLATE.md` 사용):

```markdown
## 작업 내용
변경 요약

## 변경 사항
- 항목 1
- 항목 2

## 관련 이슈
Closes #45

## 리뷰어 참고사항
검증 명령과 결과
```

**좋은 PR의 조건:**
- 하나의 이슈를 해결합니다.
- 제목만 봐도 변경 목적이 드러납니다 (커밋 컨벤션과 동일 형식).
- 변경 사항이 bullet list로 정리되어 있습니다.
- 무관한 리팩터링과 기능 변경을 섞지 않습니다.
- 리뷰 중 발견된 별도 작업은 새 이슈로 분리합니다.

**Draft vs Ready:**
- Draft: 작업 중이거나 이른 피드백이 필요할 때
- Ready: 정식 리뷰를 요청할 때

## Review & Approval

**머지 조건:**
- 팀원 **2명** approve
- 모든 conversation resolved
- CI status check 통과
- Ready for review 상태 (Draft는 approve가 있어도 merge 불가)

**리뷰어 확인 사항:**
- 이슈의 목적과 PR 변경이 일치하는가
- 변경 범위가 너무 크지 않은가
- 테스트 또는 검증 방법이 충분한가
- `Closes #이슈번호`가 있는가
- 불필요한 파일, 캐시, 시크릿이 포함되지 않았는가

**Claude 자동 리뷰:**
- PR이 처음 열리거나(`opened`) Ready for review로 전환되면
  (`ready_for_review`) 자동 실행됩니다.
- 피드백 반영 후 재리뷰가 필요하면 PR conversation에 `/claude-review`
  댓글을 작성합니다. PR에서만 동작하며 일반 이슈 댓글에서는 실행되지
  않습니다.
- push마다 자동 재리뷰하는 `synchronize` 이벤트는 비용과 노이즈를
  줄이기 위해 사용하지 않습니다.

**Branch protection (`main`):**
- 직접 push 금지, PR을 통한 변경만 허용
- approve 후 새 커밋이 push되면 approve가 초기화될 수 있습니다.

## Merging

**Squash and merge만 사용합니다.** 저장소 설정에서 merge commit과
rebase merge는 비활성화되어 있습니다.

1. "Squash and merge" 클릭
2. 머지 커밋 제목을 `<type>: <설명> (#PR번호)` 형식으로 확인
3. Confirm

**결과:**
- 커밋이 하나로 squash됩니다.
- `Closes #이슈번호`로 연결된 이슈가 자동 close됩니다.
- 브랜치가 자동 삭제됩니다.

## GitHub Projects

Project는 현재 상태를 보여주는 보드로 사용합니다.

| 상태 | 의미 | 전환 |
|---|---|---|
| `Todo` | 시작 전 | 이슈/PR 생성 시 자동 추가 |
| `In Progress` | 작업 중 | 작업 시작 시 직접 이동 |
| `Done` | 완료 | merge/close 시 자동 전환 |

**켜져 있는 자동화:**
- Auto-add to project: open 이슈/PR 자동 추가 (`is:issue,pr is:open`)
- Item added → `Todo` 설정
- Item closed / PR merged → `Done` 설정
- Project에서 `Done`으로 옮기면 이슈 자동 close

## Labels

Issue Form과 자동화를 단순하게 유지하기 위해 `feature`, `bug`,
`experiment`를 우선 사용합니다. 보조: `documentation`,
`good first issue`, `help wanted`, `question`. `enhancement`는
`feature`와 겹치면 `feature`를 우선합니다.

## CI

`.github/workflows/ci.yml`이 PR과 `main` push, 수동 실행
(`workflow_dispatch`)에서 동작합니다.

- Python 3.11 / 3.12에서 `python -m pytest`
- `Dockerfile.app` 기반 이미지 빌드와 import smoke check

## Special Cases

### main과 충돌

```bash
git fetch origin main
git rebase origin/main
git push --force-with-lease origin feat/45-...
```

리뷰어의 재리뷰가 필요합니다.

### 리뷰에서 수정 요청

1. 새 커밋으로 수정합니다 (amend 금지).
2. push 후 재리뷰를 요청합니다.

### PR 분리

새 이슈를 만들고 커밋을 새 브랜치로 cherry-pick한 뒤 별도 PR을
생성합니다. 양쪽 PR 본문에 서로 링크를 남깁니다.

## Troubleshooting

- **PR이 merge되지 않을 때:** Draft 상태, approve 2명 충족, 충돌,
  required check 실패 여부를 확인합니다.
- **Project에 항목이 안 보일 때:** `Done` 컬럼과 view filter를
  확인합니다. 이미 closed/merged된 항목은 자동 추가 필터에 걸리지
  않을 수 있습니다.
- **이슈가 자동으로 닫히지 않을 때:** PR 본문의 `Closes #이슈번호`,
  `main`으로의 merge 여부, 이슈 번호가 같은 저장소의 번호인지
  확인합니다.
