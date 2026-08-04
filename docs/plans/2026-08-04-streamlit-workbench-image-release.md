# Streamlit Experiment Workbench Image Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Streamlit Experiment Workbench를 기존 release workflow가 검증한 immutable GAR 이미지로 발행한다.

**Architecture:** UI는 FastAPI API·Codex Runner와 분리된 Python 3.12 이미지에서 `agent_orchestration/ui/app.py`를 실행한다. 기존 API publish job과 같은 source SHA·OIDC/WIF·GAR 계약을 재사용하되 UI 전용 repository와 import smoke를 둔다.

**Tech Stack:** Docker multi-stage build, uv lock export, Python 3.12 slim, Streamlit, GitHub Actions, Artifact Registry.

## Global Constraints

- `ORCH_UI_API_TOKEN`과 API base URL은 Dockerfile·workflow·image layer·문서에 넣지 않는다.
- 이미지는 UID/GID `10001`의 비루트 사용자로 실행하고 OCI revision label에 full source SHA를 기록한다.
- 인프라는 UI의 `@sha256:` digest만 소비하며 mutable tag를 배포 입력으로 쓰지 않는다.
- 실제 release workflow dispatch와 GAR push는 PR merge 뒤 운영 승인으로만 수행한다.

---

### Task 1: Streamlit 전용 이미지 정의

**Files:**
- Create: `deploy/agent_orchestration/ui.Dockerfile`
- Test: Docker image import·사용자 smoke command

**Interfaces:**
- Consumes: `pyproject.toml`의 `orchestration-ui` dependency group과 `uv.lock`
- Produces: port `8501`에서 `agent_orchestration/ui/app.py`를 실행하는 image

- [ ] **Step 1: 이미지 계약을 드러내는 실패 기준을 작성한다**

```bash
docker run --rm autoresearch-agent-orchestration-ui:local \
  python -c "import agent_orchestration.ui.app"
docker image inspect autoresearch-agent-orchestration-ui:local \
  --format '{{ .Config.User }}'
```

첫 명령은 UI module import 성공, 두 번째 명령은 root가 아닌 사용자 값을 기대한다.

- [ ] **Step 2: Streamlit UI Dockerfile을 추가한다**

`uv export --frozen --only-group orchestration-ui`로 requirements를 고정 export한다.
`python:3.12-slim` final stage에서 UID/GID `10001` appuser를 만들고 requirements와
`agent_orchestration/ui`, UI가 import하는 Experiment API models, package initializer를
복사한다. `VCS_REF` OCI label, `EXPOSE 8501`, `streamlit run` headless command를 둔다.

- [ ] **Step 3: 로컬 image contract를 검증한다**

```bash
docker build -f deploy/agent_orchestration/ui.Dockerfile \
  -t autoresearch-agent-orchestration-ui:local \
  --build-arg VCS_REF=local .
docker run --rm autoresearch-agent-orchestration-ui:local \
  python -c "import agent_orchestration.ui.app"
```

- [ ] **Step 4: 이미지 변경을 커밋한다**

```bash
git add deploy/agent_orchestration/ui.Dockerfile
git commit -m "feat: Streamlit UI 이미지 추가"
```

### Task 2: UI release publish job 추가

**Files:**
- Modify: `.github/workflows/release.yml`
- Test: GitHub Actions release job

**Interfaces:**
- Consumes: `publish-application-image.outputs.source_sha`, 기존 GAR OIDC/WIF 설정
- Produces: `autoresearch-agent-orchestration-ui@sha256:<digest>`와 job summary handoff

- [ ] **Step 1: UI job의 검증 기준을 정의한다**

Job은 source SHA 일치, `sha-<source-sha>` tag, OCI revision label, 비루트 Config.User,
`import agent_orchestration.ui.app`, valid `sha256:` digest를 모두 실패 조건으로 둔다.

- [ ] **Step 2: API publish job과 대칭인 UI publish job을 구현한다**

`publish-agent-orchestration-ui-image` job은 application image job을 needs로 두고,
`deploy/agent_orchestration/ui.Dockerfile`을 build한다. image URI는
`autoresearch-agent-orchestration-ui`이며 기존 GAR pusher OIDC/WIF permissions만
재사용한다. digest verify와 summary에는 UI 이름을 명시한다.

- [ ] **Step 3: workflow 정적 검증을 수행한다**

```bash
git diff --check
actionlint .github/workflows/release.yml
```

`actionlint`가 로컬에 없으면 GitHub Actions lint check가 동등 검증 경로다.

- [ ] **Step 4: release workflow 변경을 커밋한다**

```bash
git add .github/workflows/release.yml
git commit -m "feat: Streamlit UI 릴리스 이미지 발행 추가"
```

### Task 3: 이미지 공급 문서 갱신

**Files:**
- Modify: `README.md`
- Modify: `.claude/docs/agent-project-reference.md`
- Modify: `docs/guides/release-pipeline.md`
- Modify: `agent_orchestration/README.md`

**Interfaces:**
- Consumes: Task 1 image name·실행 경계와 Task 2 release digest output
- Produces: 인프라 담당자가 digest를 소비할 수 있는 운영 문서

- [ ] **Step 1: README와 project reference에 UI image 경계를 기록한다**

API·Runner와 UI가 서로 다른 이미지임을 명시하고 UI가 GKE manifest·KSA/GSA·NetworkPolicy를
소유하지 않는 경계를 기록한다.

- [ ] **Step 2: release guide와 orchestration README를 갱신한다**

release diagram·설명에 UI publish job과 UI digest handoff를 추가한다. Orchestration README에는
UI가 API token을 런타임 환경에서만 받고 internal Service를 소비한다는 운영 제약을 기록한다.

- [ ] **Step 3: 문서 변경을 커밋한다**

```bash
git add README.md .claude/docs/agent-project-reference.md \
  docs/guides/release-pipeline.md agent_orchestration/README.md
git commit -m "docs: Streamlit UI 이미지 배포 경계 기록"
```

### Task 4: PR 전 검증과 handoff

**Files:**
- Verify: `deploy/agent_orchestration/ui.Dockerfile`
- Verify: `.github/workflows/release.yml`

- [ ] **Step 1: 좁은 검증을 실행한다**

```bash
docker build -f deploy/agent_orchestration/ui.Dockerfile \
  -t autoresearch-agent-orchestration-ui:local .
docker run --rm autoresearch-agent-orchestration-ui:local \
  python -c "import agent_orchestration.ui.app"
uv run --no-sync ruff check agent_orchestration/ui
git diff --check
```

- [ ] **Step 2: Draft PR을 만든다**

PR 본문에 `Closes #512`, UI image가 외부 노출·새 secret·새 IAM을 만들지 않는 점과,
release workflow가 발행할 immutable digest를 infra issue가 소비한다는 점을 기록한다.
