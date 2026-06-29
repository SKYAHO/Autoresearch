# 기여 가이드 (Contributing Guide)

AutoResearch 프로젝트에 기여해 주셔서 감사합니다.  
원활한 협업을 위해 아래 규칙을 따라 주세요.

---

## 워크플로우

```
이슈 생성 → 브랜치 생성 → 작업 → PR 생성 → 리뷰 → Squash Merge
```

1. **이슈 생성**: 작업 시작 전 반드시 이슈를 먼저 생성합니다.  
   이슈 템플릿(Feature / Bug / Experiment)을 선택해 작성해 주세요.

2. **브랜치 생성**: `main`에서 분기하여 작업 브랜치를 만듭니다.  
   브랜치 네이밍 규칙은 아래를 따릅니다.

3. **작업 및 커밋**: 커밋 컨벤션에 따라 커밋 메시지를 작성합니다.

4. **PR 생성**: PR 템플릿을 채우고, 본문에 `Closes #이슈번호`를 포함합니다.

5. **코드 리뷰**: 팀원 **최소 1명**의 Approve를 받아야 머지할 수 있습니다.

6. **Squash Merge**: 머지는 항상 **Squash and merge** 방식으로 합니다.  
   머지 커밋 제목은 `<type>: <설명> (#PR번호)` 형식으로 작성합니다.

---

## 브랜치 네이밍 규칙

| 유형 | 패턴 | 예시 |
|------|------|------|
| 기능 개발 | `feat/이슈번호-간략한-설명` | `feat/42-add-feature-store-schema` |
| 버그 수정 | `fix/이슈번호-간략한-설명` | `fix/57-training-oom-error` |
| 실험 | `exp/이슈번호-간략한-설명` | `exp/61-lgbm-baseline` |
| 문서 | `docs/이슈번호-간략한-설명` | `docs/30-update-readme` |
| 리팩터링 | `refactor/이슈번호-간략한-설명` | `refactor/48-serving-cleanup` |
| 기타 | `chore/이슈번호-간략한-설명` | `chore/10-setup-ci` |

- 영어 소문자와 하이픈(`-`)만 사용합니다.
- 이슈 번호를 반드시 포함합니다.

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

### 예시

```
feat: Feature Store에 스키마 버전 관리 기능 추가
fix: Training 파이프라인 OOM 오류 수정
exp: LightGBM 베이스라인 실험 추가
docs: CONTRIBUTING.md 초안 작성
```

- 설명은 한국어로 작성합니다.
- 제목은 현재형 동사로 시작합니다 (추가, 수정, 삭제, ...).
- 제목은 50자 이내로 작성합니다.

---

## main 브랜치 보호 규칙

`main` 브랜치에는 아래 보호 규칙이 적용되어 있습니다.

- **직접 push 금지**: 모든 변경은 PR을 통해서만 반영됩니다.
- **리뷰 승인 필수**: 최소 1명의 팀원 Approve가 있어야 머지할 수 있습니다.
- **PR 통과 후 머지**: CI 체크가 모두 통과해야 머지할 수 있습니다.
- **머지 방식**: Squash and merge만 허용합니다.

> GitHub 레포 설정 → Settings → Branches → Branch protection rules 에서 확인할 수 있습니다.
