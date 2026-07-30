# Agent Orchestration GKE 내부 배포 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 공용 Codex CLI OAuth를 단일 PVC에 안전하게 보존하면서 Agent Orchestration API를 GKE dev와 기존 Cloud SQL에 내부 전용으로 배포한다.

**Architecture:** Agent 이미지는 FastAPI·Codex CLI·시크릿 부트스트랩 모듈을 제공한다. GKE init container는 동일 이미지와 Workload Identity로 Secret Manager에서 DB 비밀번호·OAuth 초기 인증 파일을 읽어 Pod 공유 임시 볼륨과 `CODEX_HOME` PVC를 준비하고, 앱 컨테이너는 Cloud SQL Private IP로 연결한다.

**Tech Stack:** Python 3.12, FastAPI/Uvicorn, `@openai/codex` 0.146.0, psycopg 3, Google Secret Manager, Workload Identity, Cloud SQL PostgreSQL 15, GKE Standard, PVC, Terraform, ArgoCD, GAR.

## Global Constraints

- PR #431이 `main`에 병합된 뒤 최신 `origin/main`에서 구현한다.
- API는 `ClusterIP`만 사용하며, Ingress·LoadBalancer·외부 공개·다중 replica는 만들지 않는다.
- Cloud SQL DB/user는 `agent_orchestration`/`agent_orchestration_app`, Deployment는 `replicas: 1`, PVC는 `ReadWriteOnce`로 고정한다.
- OAuth `auth.json`, DB 비밀번호, 완성 DB URL은 Git·이미지·ConfigMap·Kubernetes Secret·환경 변수·일반 로그에 넣지 않는다.
- Secret Manager CSI는 추가하지 않는다. 전용 GSA/KSA의 Workload Identity와 동일 Agent 이미지 init container를 사용한다.
- Codex 호출 옵션은 `--sandbox read-only --ephemeral --skip-git-repo-check`을 유지한다.
- 배포 manifest는 GAR immutable digest만 참조한다.

## 파일 구조

| 저장소 | 생성·변경 파일 | 책임 |
|---|---|---|
| Autoresearch | `agent_orchestration/bootstrap_secrets.py`, `agent_orchestration/entrypoint.sh` | Secret Manager 읽기, DB 런타임 파일, 앱 시작 |
| Autoresearch | `deploy/agent_orchestration/Dockerfile`, `tests/test_agent_orchestration_{bootstrap,container}.py` | Codex 포함 non-root 이미지와 계약 테스트 |
| Autoresearch | `.github/workflows/{ci,release}.yml` | Agent 이미지 build, GAR push, digest verify |
| Autoresearch-infra | `terraform/envs/dev/{cloud_sql,secret_manager,gke,locals,variables,outputs}.tf` | 전용 DB/user, GSA, 최소 Secret Manager IAM |
| Autoresearch-infra | `terraform/admin/autoresearch-k8s/{main,variables,locals,outputs}.tf` | `agent-orchestration` KSA와 GSA 매핑 |
| Autoresearch-infra | `terraform/admin/argocd-k8s/{main,variables,locals}.tf`, `deploy/agent-orchestration/{deployment,service}.yaml` | 수동 ArgoCD Application과 내부 GKE workload |
| Autoresearch-infra | `docs/runbooks/2026-07-30-agent-orchestration-gke.md` | OAuth 등록·복구, smoke, rollback |

---

### Task 1: 병합 기준점 동기화

**Files:** 없음 (Git 상태만 변경)

**Consumes:** PR #431의 Agent API 구현.

**Produces:** #431을 포함한 최신 `main` 기반 배포 브랜치.

- [ ] **Step 1: #431 병합을 확인한다.**

```bash
gh pr view 431 --json state,mergedAt,mergeCommit
```

Expected: `state=MERGED`이며 `mergedAt`과 `mergeCommit`이 비어 있지 않다.

- [ ] **Step 2: 배포 브랜치를 rebase한다.**

```bash
git fetch origin main
git rebase origin/main
uv run python -m pytest tests/test_agent_orchestration.py -q
```

Expected: 충돌 없이 완료되고 기존 Agent API 테스트가 통과한다. rebase 충돌 해소가 필요하면 해소 파일만 `chore: 배포 브랜치 기준점 동기화`로 커밋한다.

### Task 2: 런타임 시크릿 부트스트랩 구현

**Files:**
- Create: `agent_orchestration/bootstrap_secrets.py`
- Create: `agent_orchestration/entrypoint.sh`
- Create: `tests/test_agent_orchestration_bootstrap.py`
- Modify: `pyproject.toml`, `uv.lock`

**Consumes:** `ORCH_DB_PASSWORD_SECRET_ID`, `ORCH_CODEX_AUTH_SECRET_ID`, `ORCH_DB_HOST`, `ORCH_DB_NAME`, `ORCH_DB_USER`, `ORCH_RUNTIME_DIR`, `CODEX_HOME`과 Workload Identity ADC.

**Produces:** `bootstrap_runtime_secrets(settings, read_secret) -> None` 및 권한 제한 `db.env`.

- [ ] **Step 1: 실패하는 단위 테스트를 작성한다.**

```python
def test_bootstrap_writes_database_env_and_initial_auth_once(tmp_path: Path) -> None:
    bootstrap_runtime_secrets(settings, secret_reader)
    assert runtime_env.read_text().startswith("ORCH_DATABASE_URL=postgresql://")
    assert auth_path.read_bytes() == b'{"tokens":"secret"}'
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600
```

추가 테스트는 기존 `auth.json`을 덮어쓰지 않고, 예외·로그에 비밀번호/OAuth payload가 없음을 검증한다.

- [ ] **Step 2: 실패를 확인한다.**

```bash
uv run python -m pytest tests/test_agent_orchestration_bootstrap.py -q
```

Expected: 모듈 또는 함수 부재로 실패한다.

- [ ] **Step 3: 최소 구현을 추가한다.**

`google-cloud-secret-manager`를 런타임 의존성으로 추가한다. 아래 계약의 `BootstrapSettings`와 `bootstrap_runtime_secrets()`를 구현한다.

```python
@dataclass(frozen=True)
class BootstrapSettings:
    db_password_secret_id: str
    codex_auth_secret_id: str
    db_host: str
    db_name: str
    db_user: str
    runtime_dir: Path
    codex_home: Path

def bootstrap_runtime_secrets(
    settings: BootstrapSettings,
    read_secret: Callable[[str], bytes],
) -> None: ...
```

DB 비밀번호와 비민감 DB 좌표로 `ORCH_DATABASE_URL=` 한 줄만 담은 `db.env`를 mode `0600`으로 쓴다. `auth.json`은 없을 때만 Secret Manager에서 가져와 mode `0600`으로 쓴다. 로그에는 시크릿 ID만 남긴다.

`entrypoint.sh`는 `set -eu`, `set -a; . "$ORCH_RUNTIME_DIR/db.env"; set +a` 후 다음으로 종료한다.

```sh
exec uvicorn agent_orchestration.app.main:app --host 0.0.0.0 --port 8000
```

- [ ] **Step 4: 테스트·lint·커밋을 수행한다.**

```bash
uv lock
uv run python -m pytest tests/test_agent_orchestration.py tests/test_agent_orchestration_bootstrap.py -q
uv run --no-sync ruff check agent_orchestration tests/test_agent_orchestration_bootstrap.py
git add pyproject.toml uv.lock agent_orchestration/bootstrap_secrets.py agent_orchestration/entrypoint.sh tests/test_agent_orchestration_bootstrap.py
git commit -m "feat: 오케스트레이션 런타임 시크릿 부트스트랩 추가"
```

Expected: 테스트와 lint가 통과하고 시크릿 원문은 staged diff에 없다.

### Task 3: Agent 이미지와 CI 계약 추가

**Files:**
- Create: `deploy/agent_orchestration/Dockerfile`
- Create: `tests/test_agent_orchestration_container.py`
- Modify: `.dockerignore`, `.github/workflows/ci.yml`, `agent_orchestration/README.md`

**Consumes:** Task 2 모듈과 root `pyproject.toml`/`uv.lock`.

**Produces:** `autoresearch-agent-orchestration:ci` non-root 이미지.

- [ ] **Step 1: 실패하는 컨테이너 계약 테스트를 작성한다.**

```python
def test_agent_dockerfile_pins_codex_and_uses_nonroot_entrypoint() -> None:
    dockerfile = Path("deploy/agent_orchestration/Dockerfile").read_text()
    assert "@openai/codex@0.146.0" in dockerfile
    assert "USER appuser" in dockerfile
    assert "agent_orchestration/entrypoint.sh" in dockerfile
    assert "auth.json" not in dockerfile
```

또한 `.dockerignore`가 `.env`와 `.codex`를 build context에서 제외하는지 검증한다.

- [ ] **Step 2: Dockerfile 부재 실패를 확인한다.**

```bash
uv run python -m pytest tests/test_agent_orchestration_container.py -q
```

Expected: `FileNotFoundError`로 실패한다.

- [ ] **Step 3: multi-stage Dockerfile을 구현한다.**

`node:22-bookworm-slim` stage에서 아래처럼 고정 Codex CLI를 설치하고, 최종 `python:3.12-slim-bookworm` stage에 필요한 `/usr/local` runtime을 복사한다.

```dockerfile
ARG CODEX_VERSION=0.146.0
RUN npm install --global "@openai/codex@${CODEX_VERSION}"
```

최종 stage는 UID `10001`의 `appuser`, `HOME=/home/appuser`, `CODEX_HOME=/var/lib/codex`, `ORCH_RUNTIME_DIR=/var/run/agent-orchestration`을 설정하고 `entrypoint.sh`를 `CMD`로 실행한다. OAuth·DB 값은 `COPY`, `ARG`, `ENV`에 넣지 않는다.

- [ ] **Step 4: build·smoke·CI를 검증하고 커밋한다.**

```bash
docker build -f deploy/agent_orchestration/Dockerfile -t autoresearch-agent-orchestration:ci .
docker run --rm autoresearch-agent-orchestration:ci codex --version
docker run --rm autoresearch-agent-orchestration:ci python -c "import agent_orchestration.app.main, agent_orchestration.bootstrap_secrets"
uv run python -m pytest tests/test_agent_orchestration_container.py -q
git add deploy/agent_orchestration/Dockerfile tests/test_agent_orchestration_container.py .dockerignore .github/workflows/ci.yml agent_orchestration/README.md
git commit -m "feat: 오케스트레이션 배포 이미지 추가"
```

Expected: Codex 버전은 `0.146.0`, 이미지 사용자는 non-root, import와 CI smoke는 성공한다.

### Task 4: GAR release digest 발행 추가

**Files:**
- Modify: `.github/workflows/release.yml`, `docs/guides/release-pipeline.md`, `tests/test_agent_orchestration_container.py`

**Consumes:** Task 3 Dockerfile.

**Produces:** `autoresearch-agent-orchestration@sha256:...`를 출력하는 `publish-agent-orchestration-image` job.

- [ ] **Step 1: workflow 계약 테스트를 추가하고 실패를 확인한다.**

```python
def test_release_workflow_publishes_agent_orchestration_digest() -> None:
    workflow = Path(".github/workflows/release.yml").read_text()
    assert "publish-agent-orchestration-image" in workflow
    assert "deploy/agent_orchestration/Dockerfile" in workflow
```

```bash
uv run python -m pytest tests/test_agent_orchestration_container.py::test_release_workflow_publishes_agent_orchestration_digest -q
```

Expected: 새 job 부재로 실패한다.

- [ ] **Step 2: release job을 추가한다.**

`publish-serving-image`의 immutable checkout/WIF/GAR 패턴을 사용하되 URI를 `${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${GAR_REPOSITORY}/autoresearch-agent-orchestration`으로 고정한다. verify는 digest, OCI revision, non-root user, `codex --version`, `import agent_orchestration.app.main, agent_orchestration.bootstrap_secrets`를 확인하고 summary에 digest/source SHA만 기록한다.

- [ ] **Step 3: workflow를 검증하고 커밋한다.**

```bash
actionlint .github/workflows/release.yml
git diff --check
uv run python -m pytest tests/test_agent_orchestration_container.py -q
git add .github/workflows/release.yml docs/guides/release-pipeline.md tests/test_agent_orchestration_container.py
git commit -m "feat: 오케스트레이션 이미지 release 발행 추가"
```

Expected: 세 검증이 통과한다. `actionlint` 미설치 시 CI workflow syntax 결과를 PR에 남긴다.

### Task 5: Infra DB, 전용 Workload Identity, Secret Manager 추가

**Files:**
- Modify: `Autoresearch-infra/terraform/envs/dev/{cloud_sql,secret_manager,gke,locals,variables,outputs}.tf`
- Modify: `Autoresearch-infra/terraform/admin/autoresearch-k8s/{main,variables,locals,outputs}.tf`

**Consumes:** Task 4 digest와 기존 `autoresearch-dev-pg`/`autoresearch` namespace.

**Produces:** 전용 DB/user, `autoresearch-dev-agent-orchestration` GSA, `agent-orchestration` KSA, 두 시크릿 최소 권한.

- [ ] **Step 1: infra 저장소에 별도 feature 이슈와 연결 브랜치를 만든다.**

완료 조건은 전용 DB/user, OAuth bootstrap secret, DB password secret, GSA/KSA, ArgoCD Deployment, 내부 smoke와 rollback이다. 브랜치는 GitHub Issue의 `Create a branch`에서 만들어 `main` 기준 worktree로 checkout한다.

- [ ] **Step 2: plan 실패 상태를 기록한다.**

```bash
terraform -chdir=terraform/envs/dev plan -out=/tmp/agent-orchestration.tfplan
terraform -chdir=terraform/envs/dev show -json /tmp/agent-orchestration.tfplan | jq -e '[.resource_changes[].address] | index("google_sql_database.agent_orchestration")'
```

Expected before implementation: 대상 resource가 없어 jq가 실패한다.

- [ ] **Step 3: 최소 권한 Terraform을 구현한다.**

`cloud_sql.tf`에 `random_password.agent_orchestration_db_password`, `google_sql_database.agent_orchestration`, `google_sql_user.agent_orchestration`을 추가하고 기존 URI-safe 문자 집합 `-_.~`을 유지한다. `secret_manager.tf`에는 Terraform이 version을 관리하는 `autoresearch-dev-agent-orchestration-db-password`와 operator payload 전용의 빈 `autoresearch-dev-agent-orchestration-codex-auth-bootstrap` secret(`prevent_destroy = true`)을 분리한다.

`gke.tf`에는 전용 GSA, `roles/cloudsql.client`, 두 시크릿에 한정한 `roles/secretmanager.secretAccessor`, KSA subject의 `roles/iam.workloadIdentityUser`를 추가한다. 기존 `gke_app` 권한은 넓히지 않는다. admin root는 `agent-orchestration` KSA annotation을 전용 GSA email로 설정한다.

- [ ] **Step 4: Terraform을 검증하고 커밋한다.**

```bash
terraform -chdir=terraform/envs/dev fmt -check
terraform -chdir=terraform/envs/dev validate
terraform -chdir=terraform/admin/autoresearch-k8s fmt -check
terraform -chdir=terraform/admin/autoresearch-k8s validate
git add terraform/envs/dev terraform/admin/autoresearch-k8s
git commit -m "feat: 오케스트레이션 GKE 인증 기반 추가"
```

Expected: 신규 DB/user/GSA/secret/IAM만 plan에 나타나며 기존 Cloud SQL 인스턴스 교체는 없다.

### Task 6: ArgoCD workload, runbook, 실배포 검증

**Files:**
- Create: `Autoresearch-infra/deploy/agent-orchestration/{deployment,service}.yaml`
- Modify: `Autoresearch-infra/terraform/admin/argocd-k8s/{main,variables,locals}.tf`
- Create: `Autoresearch-infra/docs/runbooks/2026-07-30-agent-orchestration-gke.md`

**Consumes:** Task 5 GSA/KSA/secret IDs와 Task 4 immutable digest.

**Produces:** 수동 ArgoCD sync가 가능한 single Pod 내부 API와 OAuth 복구·rollback runbook.

- [ ] **Step 1: manifest 부재 실패를 확인한다.**

```bash
kubectl apply --dry-run=client -f deploy/agent-orchestration/deployment.yaml
kubectl apply --dry-run=client -f deploy/agent-orchestration/service.yaml
```

Expected: 파일 부재로 실패한다.

- [ ] **Step 2: Deployment와 Service를 구현한다.**

Deployment는 `namespace: autoresearch`, `serviceAccountName: agent-orchestration`, `replicas: 1`, immutable Agent digest를 사용한다. 명시적 1Gi `ReadWriteOnce` PVC `agent-orchestration-codex-state`를 `/var/lib/codex`, `emptyDir`를 `/var/run/agent-orchestration`에 마운트한다.

init container는 동일 Agent digest에서 다음을 실행한다.

```yaml
command: ["python", "-m", "agent_orchestration.bootstrap_secrets"]
```

컨테이너에는 시크릿 ID와 비민감 DB 좌표만 전달한다. `runAsUser`, `runAsGroup`, `fsGroup`은 `10001`, privilege escalation은 금지한다. startup/readiness는 `/healthcheck`, liveness는 TCP 8000이며 Service는 port 8000 `ClusterIP`만 가진다.

- [ ] **Step 3: ArgoCD와 runbook을 추가한다.**

`application_agent_orchestration`은 `deploy/agent-orchestration`을 `autoresearch` namespace로 수동 sync하고 `CreateNamespace=false`만 사용한다. runbook에는 `codex login` 후 payload를 출력하지 않고 Secret Manager version 등록, `kubectl port-forward` smoke, id/model/latency/created_at만 DB 조회, 이전 digest rollback, Deployment 0→PVC auth 교체→1 복구 순서를 적는다.

- [ ] **Step 4: manifest·Terraform·실배포를 검증한다.**

```bash
kubectl apply --dry-run=client -f deploy/agent-orchestration/deployment.yaml
kubectl apply --dry-run=client -f deploy/agent-orchestration/service.yaml
terraform -chdir=terraform/admin/argocd-k8s fmt -check
terraform -chdir=terraform/admin/argocd-k8s validate
kubectl -n autoresearch rollout status deployment/agent-orchestration --timeout=5m
```

Expected: single Ready Pod가 된다. `kubectl port-forward service/agent-orchestration 8000:8000` 뒤 `/healthcheck`은 200, 짧은 `/chat`은 201을 반환하며 DB에는 `model=codex-cli`, `token_count=NULL` 행이 저장된다.

- [ ] **Step 5: infra 변경을 커밋한다.**

```bash
git add deploy/agent-orchestration terraform/admin/argocd-k8s docs/runbooks/2026-07-30-agent-orchestration-gke.md
git commit -m "feat: 오케스트레이션 내부 GKE 배포 추가"
```

## 최종 검증 체크리스트

- [ ] `uv run python -m pytest -q` 및 `uv run --no-sync ruff check autoresearch tests tools agent_orchestration`
- [ ] Agent Docker build, `codex --version`, app import, release workflow actionlint
- [ ] Infra Terraform `fmt -check`, `validate`, 신규 리소스만 추가하는 plan
- [ ] ArgoCD manual diff, single Pod readiness, internal healthcheck/chat, Cloud SQL 메타데이터 저장
- [ ] Git diff·이미지 history·Pod manifest·로그에 OAuth payload, DB 비밀번호, 완성 DB URL이 없음

## Plan Self-Review

- **Spec coverage:** 내부 ClusterIP, 단일 PVC/replica, Workload Identity의 직접 Secret Manager 읽기, 전용 DB/user, 전용 GSA/KSA, immutable GAR digest, ArgoCD manual sync, OAuth 복구, 실제 smoke/rollback을 Task 1~6에 배정했다.
- **Placeholder scan:** 리소스명·파일 경로·시크릿 전달 방식·검증 명령을 모두 명시했다.
- **Type consistency:** `BootstrapSettings`/`bootstrap_runtime_secrets()`와 init command, runtime/CodeX 경로, DB 환경 변수명이 모든 작업에서 동일하다.
