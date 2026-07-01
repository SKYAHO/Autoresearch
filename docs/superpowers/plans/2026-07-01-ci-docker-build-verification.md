# CI Docker 빌드 검증 구현 계획

> **에이전트 작업자 필수 지침:** REQUIRED SUB-SKILL: 이 계획을 태스크 단위로 구현할 때는 `superpowers:subagent-driven-development` 사용을 권장하며, 대안으로 `superpowers:executing-plans`를 사용할 수 있습니다. 진행 상황 추적은 체크박스(`- [ ]`) 문법을 사용합니다.

**목표:** 현재 pytest 테스트를 GitHub Actions에서 자동 실행하고, 프로젝트 Docker 이미지가 빌드 및 기본 실행되는지 smoke check로 검증합니다.

**아키텍처:** 현재 코드베이스 구조에 맞춰 CI 범위를 작게 유지합니다. 이 저장소는 `autoresearch/` Python 패키지, `tests/` 테스트, `requirements.txt` 의존성 파일로 구성되어 있으므로, 이번 작업은 Python 테스트와 Docker 이미지 빌드 검증에 집중합니다. GKE 지향 패키징을 위한 최소 Docker 이미지는 추가하지만, 실제 배포, registry push, cluster 인증, Kubernetes manifest는 이번 이슈에 포함하지 않습니다.

**기술 스택:** GitHub Actions, Python 3.11/3.12, pytest, Docker, `python:3.12-slim`.

---

## 현재 코드베이스 맥락

- Python 패키지 코드는 `autoresearch/`에 있습니다.
- 테스트 코드는 `tests/`에 있습니다.
- 의존성은 `requirements.txt`에 정의되어 있습니다.
- 기존 workflow인 `.github/workflows/claude.yml`은 Claude PR review 용도이므로 일반 테스트/빌드 CI와 분리해 유지합니다.
- 현재 저장소에는 `Dockerfile`, `.dockerignore`, 패키지 설치 메타데이터, CLI entrypoint, GKE manifest, Artifact Registry push workflow, 배포 workflow가 없습니다.
- 사용자가 실제 배포는 이번 브랜치 범위가 아니라고 명시했으므로, 이 계획은 GKE 배포를 추가하지 않습니다.

## 파일 구조

- 생성: `Dockerfile`
  - 현재 Python 패키지용 runtime image를 빌드합니다.
  - `requirements.txt`를 설치합니다.
  - `autoresearch/`만 이미지에 복사합니다.
  - non-root user로 실행합니다.
  - 기본 명령으로 import smoke check를 실행하여 `docker run --rm autoresearch:ci`가 패키지 import를 검증하게 합니다.
- 생성: `.dockerignore`
  - git 메타데이터, GitHub workflow 파일, 캐시, 가상환경, 로컬 env 파일, 생성 데이터가 Docker build context에 포함되지 않도록 제외합니다.
- 생성: `.github/workflows/ci.yml`
  - Python 3.11과 3.12에서 pytest를 실행합니다.
  - Docker 이미지를 빌드합니다.
  - Docker 이미지 smoke check를 실행합니다.
- 수정: `GITHUB_WORKFLOW.md`
  - CI가 무엇을 검증하는지, 이번 이슈에서 의도적으로 제외한 항목이 무엇인지 짧게 문서화합니다.

---

### Task 1: Docker 빌드 기반 추가

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`

- [ ] **Step 1: Dockerfile이 없어서 Docker build가 실패하는지 확인**

Run:

```powershell
docker build --tag autoresearch:ci .
```

Expected: `Dockerfile`을 찾을 수 없다는 오류와 함께 명령이 실패합니다.

- [ ] **Step 2: `.dockerignore` 생성**

`.dockerignore`를 아래 내용으로 생성합니다.

```dockerignore
.git
.github
.pytest_cache
__pycache__/
*.py[cod]
.venv
venv
env
.env
.env.*
data
*.parquet
dist
build
*.egg-info
```

- [ ] **Step 3: `Dockerfile` 생성**

`Dockerfile`을 아래 내용으로 생성합니다.

```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN adduser --disabled-password --gecos "" appuser

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY autoresearch ./autoresearch

USER appuser

CMD ["python", "-c", "import autoresearch; print('autoresearch image ready')"]
```

- [ ] **Step 4: Docker 이미지가 빌드되는지 확인**

Run:

```powershell
docker build --tag autoresearch:ci .
```

Expected: 명령이 exit status `0`으로 종료되고, `autoresearch:ci` 태그의 로컬 이미지가 생성됩니다.

- [ ] **Step 5: Docker 이미지 smoke command가 실행되는지 확인**

Run:

```powershell
docker run --rm autoresearch:ci
```

Expected output contains:

```text
autoresearch image ready
```

- [ ] **Step 6: Docker 빌드 파일 커밋**

Run:

```powershell
git add Dockerfile .dockerignore
git commit -m "chore: Docker 빌드 검증 기반 추가"
```

Expected: 커밋이 성공하고 `Dockerfile`, `.dockerignore`만 포함됩니다.

---

### Task 2: GitHub Actions CI workflow 추가

**Files:**
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: `.github/workflows/ci.yml` 생성**

`.github/workflows/ci.yml`을 아래 내용으로 생성합니다.

```yaml
name: Python CI

on:
  pull_request:
  push:
    branches:
      - main
  workflow_dispatch:

permissions:
  contents: read

jobs:
  pytest:
    name: pytest (Python ${{ matrix.python-version }})
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version:
          - "3.11"
          - "3.12"
    steps:
      - name: Checkout repository
        uses: actions/checkout@v6

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: pip
          cache-dependency-path: requirements.txt

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          python -m pip install -r requirements.txt

      - name: Run pytest
        run: python -m pytest

  docker-build:
    name: Docker build
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v6

      - name: Build Docker image
        run: docker build --tag autoresearch:ci .

      - name: Run Docker image smoke check
        run: docker run --rm autoresearch:ci
```

- [ ] **Step 2: pytest job에서 사용하는 명령이 로컬에서 통과하는지 확인**

Run:

```powershell
python -m pip install -r requirements.txt
python -m pytest
```

Expected: pytest가 exit status `0`으로 종료됩니다.

- [ ] **Step 3: Docker job에서 사용하는 명령이 로컬에서 통과하는지 확인**

Run:

```powershell
docker build --tag autoresearch:ci .
docker run --rm autoresearch:ci
```

Expected: Docker build가 exit status `0`으로 종료되고, Docker run이 `autoresearch image ready`를 출력합니다.

- [ ] **Step 4: 공백 오류 확인**

Run:

```powershell
git diff --check
```

Expected: 출력이 없고 exit status `0`으로 종료됩니다.

- [ ] **Step 5: CI workflow 커밋**

Run:

```powershell
git add .github/workflows/ci.yml
git commit -m "chore: Python CI 워크플로우 추가"
```

Expected: 커밋이 성공하고 `.github/workflows/ci.yml`만 포함됩니다.

---

### Task 3: CI 동작 문서화

**Files:**
- Modify: `GITHUB_WORKFLOW.md`

- [ ] **Step 1: CI 문서 섹션 추가**

`GITHUB_WORKFLOW.md`의 `### Pull Request` 섹션 뒤, `### GitHub Project` 섹션 앞에 아래 내용을 삽입합니다.

```markdown
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
```

- [ ] **Step 2: 문서에 CI 섹션이 포함됐는지 확인**

Run:

```powershell
Select-String -Path GITHUB_WORKFLOW.md -Pattern "GitHub Actions CI"
```

Expected: `### GitHub Actions CI`에 대한 match가 1개 출력됩니다.

- [ ] **Step 3: 문서 커밋**

Run:

```powershell
git add GITHUB_WORKFLOW.md
git commit -m "docs: CI 운영 기준 추가"
```

Expected: 커밋이 성공하고 `GITHUB_WORKFLOW.md`만 포함됩니다.

---

### Task 4: 최종 검증

**Files:**
- Verify: `Dockerfile`
- Verify: `.dockerignore`
- Verify: `.github/workflows/ci.yml`
- Verify: `GITHUB_WORKFLOW.md`

- [ ] **Step 1: git 브랜치 확인**

Run:

```powershell
git status --short --branch
```

Expected output starts with:

```text
## 21-feat-github-actions-기반-python-ci-구성
```

- [ ] **Step 2: 전체 로컬 테스트 실행**

Run:

```powershell
python -m pytest
```

Expected: 모든 테스트가 통과하고 exit status `0`으로 종료됩니다.

- [ ] **Step 3: Docker build와 smoke check 실행**

Run:

```powershell
docker build --tag autoresearch:ci .
docker run --rm autoresearch:ci
```

Expected: Docker build가 exit status `0`으로 종료되고, Docker run이 `autoresearch image ready`를 출력합니다.

- [ ] **Step 4: 최종 diff 확인**

Run:

```powershell
git diff --stat HEAD~3..HEAD
```

Expected output includes these files:

```text
.dockerignore
.github/workflows/ci.yml
Dockerfile
GITHUB_WORKFLOW.md
```

- [ ] **Step 5: PR 본문에 issue 연결 문구 포함**

PR을 열 때 아래 본문 조각을 사용합니다.

```markdown
## 관련 이슈

Closes #21

## 검증

- `python -m pytest`
- `docker build --tag autoresearch:ci .`
- `docker run --rm autoresearch:ci`
```

Expected: PR이 issue `#21`에 연결되고, 로컬에서 실행한 것과 같은 검증 명령이 문서화됩니다.

---

## 자체 검토

- 요구사항 반영: pytest CI, Dockerfile build 검증, Docker smoke 검증, 운영 문서화를 모두 포함했습니다. 사용자가 실제 배포는 이번 브랜치 범위가 아니라고 명시했으므로 GKE 배포는 제외했습니다.
- 미해결 항목 확인: 미정 상태의 플레이스홀더, 뒤로 미룬 구현 설명, 내용이 비어 있는 파일 지시가 없습니다.
- 명령 일관성: Docker tag는 `autoresearch:ci`, smoke output은 `autoresearch image ready`, pytest 명령은 `python -m pytest`로 일관되게 사용했습니다.
