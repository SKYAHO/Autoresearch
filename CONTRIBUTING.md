# 기여 가이드 (Contributing Guide)

AutoResearch 프로젝트에 기여해 주셔서 감사합니다.
원활한 협업을 위해 아래 규칙을 따라 주세요.

- 기준 저장소: `SKYAHO/Autoresearch`
- 기준 Project: `SKYAHO / Autoresearch`

---

## 워크플로우

```
이슈 생성 → 브랜치 생성 → 작업 → PR 생성 → 리뷰 → Squash Merge
```

1. **이슈 생성**: 작업 시작 전 반드시 이슈를 먼저 생성합니다.
   `Issues > New issue`에서 Issue Form(Feature / Bug / Experiment)을 선택해 작성해 주세요. Form을 선택하면 제목 prefix와 label이 자동으로 적용됩니다. 빈 이슈 생성은 비활성화되어 있습니다.

2. **브랜치 생성**: `main`을 최신화한 뒤 분기하여 작업 브랜치를 만듭니다.
   브랜치 네이밍 규칙은 아래를 따릅니다.

3. **작업 및 커밋**: 커밋 컨벤션에 따라 커밋 메시지를 작성합니다.

4. **PR 생성**: PR 템플릿을 채우고, 본문에 `Closes #이슈번호`를 포함합니다.

5. **코드 리뷰**: 팀원 **최소 2명**의 Approve를 받아야 머지할 수 있습니다.

6. **Squash Merge**: 머지는 항상 **Squash and merge** 방식으로 합니다.
   머지 커밋 제목은 `<type>: <설명> (#PR번호)` 형식으로 작성합니다.

---

## Issue 작성 규칙

`.github/ISSUE_TEMPLATE`의 Issue Form을 사용합니다.

| Form | 제목 prefix | 자동 label | 사용 상황 |
|------|------|------|------|
| `feature.yml` | `[FEAT]` | `feature` | 새 기능, 기능 개선 |
| `bug.yml` | `[BUG]` | `bug` | 오류, 장애, 기대와 다른 동작 |
| `experiment.yml` | `[EXP]` | `experiment` | 모델, 데이터, 지표, 방법론 실험 |

**이슈를 만드는 경우**: 새 기능, 버그, 실험 계획·결과 기록, 문서·설정·리팩터링처럼 추적이 필요한 작업, PR 리뷰 중 생긴 범위 밖 후속 작업. 아주 작은 오타 수정은 바로 PR로 처리할 수 있습니다.

**Form별 최소 작성 내용**:

- Feature: 목적, 작업 범위, 영향받는 컴포넌트, 완료 조건
- Bug: 현상, 재현 방법, 기대 동작, 실행 환경, 로그 또는 에러 메시지
- Experiment: 가설, 데이터셋, 모델, 피처, 평가지표, Champion 대비 결과, 결론

---

## 브랜치 네이밍 규칙

브랜치는 항상 `main` 최신 상태에서 생성합니다.

```bash
git switch main
git pull origin main
git switch -c feat/42-add-feature-store-schema
```

| 유형 | 패턴 | 예시 |
|------|------|------|
| 기능 개발 | `feat/이슈번호-간략한-설명` | `feat/42-add-feature-store-schema` |
| 버그 수정 | `fix/이슈번호-간략한-설명` | `fix/57-training-oom-error` |
| 실험 | `exp/이슈번호-간략한-설명` | `exp/61-lgbm-baseline` |
| 문서 | `docs/이슈번호-간략한-설명` | `docs/30-update-readme` |
| 리팩터링 | `refactor/이슈번호-간략한-설명` | `refactor/48-serving-cleanup` |
| 기타 | `chore/이슈번호-간략한-설명` | `chore/10-setup-ci` |

- 영어 소문자, 숫자, 하이픈(`-`)만 사용합니다.
- 이슈 번호를 반드시 포함합니다.
- 한 브랜치에는 하나의 주요 목적만 담습니다.

---

## 커밋 컨벤션

```
<type>: <설명>
```

### Type 목록

| type | 사용 상황 |
|------|-----------|
| `feat` | 새로운 기능 추가 |
| `fix` | 버그 수정 |
| `refactor` | 기능 변경 없는 코드 개선 |
| `docs` | 문서 추가·수정 |
| `chore` | 빌드, 패키지, CI 설정 등 |
| `exp` | 실험 코드 추가·수정 |
| `test` | 테스트 코드 추가·수정 |

### 예시

```
feat: Feature Store에 스키마 버전 관리 기능 추가
fix: Training 파이프라인 OOM 오류 수정
exp: LightGBM 베이스라인 실험 추가
docs: CONTRIBUTING.md 초안 작성
test: math utils 테스트 추가
```

- 설명은 한국어로 작성합니다.
- 제목은 현재형 동사로 시작합니다 (추가, 수정, 삭제, ...).
- 제목은 50자 이내로 작성합니다.
- 한 커밋은 하나의 논리적 변경만 담고, 포맷 변경과 기능 변경을 섞지 않습니다.

---

## PR 규칙

좋은 PR의 조건:

- 하나의 이슈를 해결합니다.
- 제목만 봐도 변경 목적이 드러납니다 (커밋 컨벤션과 동일한 형식 권장).
- 본문에 `Closes #이슈번호`가 있습니다.
- 변경 사항이 bullet list로 정리되어 있습니다.
- 테스트 또는 검증 명령이 적혀 있습니다.
- 아직 리뷰 준비가 안 되었으면 Draft PR로 둡니다.

PR은 작게 유지합니다. 무관한 리팩터링과 기능 변경을 섞지 말고, 리뷰 중 발견된 별도 작업은 새 이슈로 분리합니다.

**PR 생성 전 체크**:

- [ ] 로컬 테스트 통과: `python -m pytest`
- [ ] 불필요한 파일, 캐시, 시크릿(`.env` 등)이 없는지 확인
- [ ] 커밋 메시지가 컨벤션을 따르는지 확인

---

## 리뷰와 머지

**리뷰어 확인 사항**:

- 이슈의 목적과 PR 변경이 일치하는가
- 변경 범위가 너무 크지 않은가
- 테스트 또는 검증 방법이 충분한가
- `Closes #이슈번호`가 있는가
- 불필요한 파일, 캐시, 시크릿이 포함되지 않았는가

**Claude 자동 리뷰**: PR이 처음 열리거나 Ready for review로 전환되면 Claude 리뷰가 자동 실행됩니다. 피드백 반영 후 최신 diff를 다시 리뷰받으려면 PR conversation에 `/claude-review` 댓글을 작성합니다 (PR에서만 동작).

**머지 후 자동 흐름**:

```
PR merge → PR Status: Done → Closes #issue로 이슈 close → Issue Status: Done
```

머지 직후 Project에서 항목이 사라진 것처럼 보이면 `Done` 컬럼을 먼저 확인합니다.

---

## GitHub Projects 운영

Project는 현재 상태를 보여주는 보드로 사용합니다.

| 상태 | 의미 | 전환 |
|------|------|------|
| `Todo` | 시작 전 | 이슈/PR 생성 시 자동 추가 |
| `In Progress` | 작업 중 | 브랜치를 따고 작업을 시작하면 직접 이동 |
| `Done` | 완료 | merge/close 시 자동 전환 |

켜져 있는 자동화: open 이슈/PR 자동 추가(`is:issue,pr is:open`), 추가 시 `Todo` 설정, close/merge 시 `Done` 설정, Project에서 `Done`으로 옮기면 이슈 자동 close.

Project의 `Add item`으로 제목만 추가하면 Issue Form을 우회하게 되므로, 새 작업은 Issues 화면에서 생성합니다.

---

## Label 컨벤션

Issue Form과 자동화를 단순하게 유지하기 위해 `feature`, `bug`, `experiment`를 우선 사용합니다. 보조 label: `documentation`, `good first issue`, `help wanted`, `question`. `enhancement`는 `feature`와 겹치면 `feature`를 우선합니다.

---

## CI

`.github/workflows/ci.yml`이 PR과 `main` push에서 자동 실행됩니다.

- Python 3.11 / 3.12에서 `python -m pytest`
- `Dockerfile.app` 기반 이미지 빌드와 import smoke check

---

## main 브랜치 보호 규칙

`main` 브랜치에는 아래 보호 규칙이 적용되어 있습니다.

- **직접 push 금지**: 모든 변경은 PR을 통해서만 반영됩니다.
- **리뷰 승인 필수**: 최소 2명의 팀원 Approve가 있어야 머지할 수 있습니다.
- **CI 통과 필수**: CI 체크가 모두 통과해야 머지할 수 있습니다.
- **머지 방식**: Squash and merge만 허용합니다.

저장소 Merge 설정: squash만 허용, merge commit·rebase merge 비활성, 머지 후 head 브랜치 자동 삭제.

---

## 문제 해결

**PR이 merge되지 않을 때**: Draft 상태인지, approve 2명이 있는지, 충돌이 있는지, required check가 실패했는지 확인합니다. Draft PR은 approve를 받아도 merge할 수 없습니다.

**Project에 항목이 안 보일 때**: `Done` 컬럼과 view filter를 확인합니다. 이미 closed/merged된 항목은 자동 추가 필터(`is:issue,pr is:open`)에 걸리지 않을 수 있습니다.

**이슈가 자동으로 닫히지 않을 때**: PR 본문에 `Closes #이슈번호`가 있는지, PR이 `main`으로 merge되었는지 확인합니다.

---

## 참고 링크

- Repository: https://github.com/SKYAHO/Autoresearch
- Project Board: https://github.com/orgs/SKYAHO/projects/3/views/2
- Issue Forms: `.github/ISSUE_TEMPLATE/*.yml`
- PR template: `.github/PULL_REQUEST_TEMPLATE.md`
