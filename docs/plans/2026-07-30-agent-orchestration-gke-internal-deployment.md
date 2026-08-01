# Agent Orchestration GKE 내부 배포 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 공용 Codex OAuth를 Runner에만 제공하고, API가 Runner 응답을 Cloud SQL에 저장하는 내부 dev 배포 경로를 구축한다.

**Architecture:** API는 `/chat` 인증·요청 검증·PostgreSQL 저장을 담당하고 `codex_runner` 백엔드에서 private Runner의 `/v1/generate`를 호출한다. Runner는 Codex CLI와 Runner 전용 `CODEX_HOME` PVC만 가지며, OAuth 초기 파일은 Runner 전용 Workload Identity가 Secret Manager에서 읽어 최초 한 번 초기화한다.

**Tech Stack:** Python 3.12, FastAPI, httpx, psycopg 3, `@openai/codex` 0.146.0, Google Secret Manager, Workload Identity, Cloud SQL PostgreSQL 15, GKE, PVC, ArgoCD, GAR.

## Global Constraints

- API와 Runner는 서로 다른 Deployment·KSA·GSA·ClusterIP Service를 사용하며 API Pod에는 OAuth 시크릿·PVC를 마운트하지 않는다.
- API GSA는 DB 비밀번호·Cloud SQL에만, Runner GSA는 Codex OAuth 초기 인증 시크릿 하나에만 접근한다.
- OAuth `auth.json`, DB 비밀번호, 완성 DB URL은 Git·이미지·ConfigMap·환경 변수·일반 로그에 넣지 않는다.
- Runner는 `replicas: 1`과 전용 `ReadWriteOnce` PVC를 사용한다. 외부 Ingress·LoadBalancer·사용자별 OAuth·다중 replica는 추가하지 않는다.
- Runner의 `/healthcheck`는 요청 처리 전 전용 설정을 로드해 readiness/liveness probe가 OAuth 실행 경계의 기본 구성을 검증한다. Codex stderr 원문은 수집·로그화하지 않는다.
- API·Runner 이미지는 `uv export --only-group orchestration`으로 오케스트레이션 런타임 의존성만 설치한다. CI 워크플로 문자열 비교 대신 실제 이미지 build/smoke를 실행한다.
- Runner는 API만 아는 별도 `X-Runner-Token`을 상수 시간 비교해 심층 방어를 제공한다. 토큰은 API의 `ORCH_API_TOKEN`과 다르며, Kubernetes Secret으로 API·Runner에만 주입한다.
- 동시성 상한에 도달한 Runner 요청은 대기열에 쌓지 않고 503을 반환하며, API는 이를 503으로 보존한다. Runner Codex 시간 제한은 110초, API Runner HTTP 시간 제한은 120초로 둔다.
- `codex_cli` 로컬 백엔드와 `openai` 백엔드의 기존 계약을 유지하고, 서버 배포용으로만 `codex_runner`를 추가한다.
- 모든 새 Python 런타임 모듈은 책임 docstring과 반환 타입을 가지며, 요청·응답은 Pydantic으로 검증한다.

---

### Task 1: API·Runner 내부 생성 계약 구현

**Files:**
- Create: `agent_orchestration/contracts.py`
- Create: `agent_orchestration/codex.py`
- Create: `agent_orchestration/runner/__init__.py`
- Create: `agent_orchestration/runner/config.py`
- Create: `agent_orchestration/runner/app.py`
- Modify: `agent_orchestration/app/config.py`
- Modify: `agent_orchestration/app/llm.py`
- Modify: `tests/test_agent_orchestration.py`
- Create: `tests/test_agent_orchestration_runner.py`
- Modify: `pyproject.toml`, `uv.lock`, `.env.example`, `agent_orchestration/README.md`

**Consumes:** Existing `POST /chat` request/response contract and `CODEX_CLI_PATH`, `CODEX_HOME`, `CODEX_MODEL`, `CODEX_TIMEOUT_SEC` settings.

**Produces:** `LLM_BACKEND=codex_runner`에서 `CODEX_RUNNER_URL`의 private Runner를 호출하는 API와 `POST /v1/generate` Runner API.

- [ ] **Step 1: 실패하는 API→Runner·Runner 계약 테스트를 작성한다.**

```python
async def test_generate_response_uses_private_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(llm_backend="codex_runner", codex_runner_url="http://runner:8080")
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post_returning_runner_result)
    result = await generate_response(settings, "hello")
    assert result == LLMResult(text="runner answer", model="codex-cli", token_count=None)

def test_runner_rejects_unknown_request_fields() -> None:
    response = TestClient(create_runner_app()).post("/v1/generate", json={"prompt": "x", "model": "x"})
    assert response.status_code == 422
```

- [ ] **Step 2: 실패를 확인한다.**

```bash
uv run python -m pytest tests/test_agent_orchestration_runner.py -q
```

Expected: `agent_orchestration.runner` 및 `codex_runner` 백엔드 부재로 실패한다.

- [ ] **Step 3: 공통 Codex 실행 경계와 Runner를 구현한다.**

`agent_orchestration/contracts.py`에 다음 타입을 둔다.

```python
class LLMBackendError(RuntimeError): ...

@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    token_count: int | None
```

`agent_orchestration/codex.py`에는 다음 공개 경계를 둔다.

```python
@dataclass(frozen=True)
class CodexSettings:
    cli_path: str
    home: str
    model: str | None
    timeout_sec: int

async def generate_codex_response(settings: CodexSettings, prompt: str) -> LLMResult: ...
```

기존 `codex exec --sandbox read-only --ephemeral --skip-git-repo-check` 호출·요청별
임시 작업 디렉터리·프로세스 그룹 종료를 이 함수로 옮긴다. API `llm.py`는
`codex_cli`에서 이 함수를 재사용하고, `codex_runner`에서는 `httpx.AsyncClient`
(`trust_env=False`)로 `${CODEX_RUNNER_URL}/v1/generate`를 호출한다. 타임아웃,
HTTP 오류, 형식 오류는 `LLMBackendError("Codex runner call failed.")`로 정규화한다.

`RunnerSettings`는 Codex 설정과 `RUNNER_MAX_CONCURRENCY`(기본 1)만 읽는다.
Runner의 Pydantic `GenerateRequest(prompt)`와 `GenerateResponse(response, model,
latency_ms, token_count)`는 `extra="forbid"`이며, app은
비대기 용량 토큰을 획득한 뒤 `generate_codex_response()`를 호출한다. 상한에 도달하면
요청을 대기시키지 않고 503을 반환한다. Runner는 DB·API 공유 토큰을 읽지 않는다.

- [ ] **Step 4: 설정·문서를 갱신한다.**

`ServiceSettings`에 `codex_runner_url: str | None` 및 `codex_runner_timeout_sec: int`
를 추가한다. `LLM_BACKEND` 허용값은 `codex_cli`, `codex_runner`, `openai`이며,
`codex_runner`에는 절대 URL `CODEX_RUNNER_URL`을 요구한다. `.env.example`과
`agent_orchestration/README.md`에는 로컬 기본값은 `codex_cli`, GKE API는
`codex_runner`이라는 구분만 기록하고 OAuth 값은 기록하지 않는다. `httpx`를
`orchestration` 의존성 그룹에 추가하고 lockfile을 갱신한다.

- [ ] **Step 5: 테스트·lint·커밋을 수행한다.**

```bash
uv lock
uv run python -m pytest tests/test_agent_orchestration.py tests/test_agent_orchestration_runner.py -q
uv run --no-sync ruff check agent_orchestration tests/test_agent_orchestration.py tests/test_agent_orchestration_runner.py
git add agent_orchestration tests/test_agent_orchestration.py tests/test_agent_orchestration_runner.py pyproject.toml uv.lock .env.example
git commit -m "feat: 오케스트레이션 Codex runner 분리"
```

Expected: 기존 로컬 Codex/OpenAI 테스트와 새 API→Runner·Runner 스키마 테스트가 모두 통과한다.

### Task 2: API DB와 Runner OAuth 부트스트랩 분리

**Files:**
- Modify: `agent_orchestration/bootstrap_secrets.py`
- Modify: `agent_orchestration/entrypoint.sh`
- Create: `agent_orchestration/runner_entrypoint.sh`
- Modify: `tests/test_agent_orchestration_bootstrap.py`

**Consumes:** Task 1의 API·Runner 경계와 Secret Manager의 resource-level accessor 권한.

**Produces:** API는 `db.env`만, Runner는 최초 `auth.json`만 준비하는 독립 bootstrap 함수.

- [ ] **Step 1: 실패하는 분리 테스트를 작성한다.**

```python
def test_api_bootstrap_never_reads_codex_auth_secret(tmp_path: Path) -> None:
    bootstrap_api_database(settings, reader)
    assert reader.calls == [settings.db_password_secret_id]

def test_runner_bootstrap_preserves_refreshed_auth_file(tmp_path: Path) -> None:
    bootstrap_runner_codex_auth(settings, reader)
    auth_path.write_bytes(b'{"access_token":"refreshed"}')
    bootstrap_runner_codex_auth(settings, reader)
    assert auth_path.read_bytes() == b'{"access_token":"refreshed"}'
```

- [ ] **Step 2: 실패를 확인한다.**

```bash
uv run python -m pytest tests/test_agent_orchestration_bootstrap.py -q
```

Expected: 분리된 public bootstrap 함수 부재로 실패한다.

- [ ] **Step 3: 최소 분리 구현을 추가한다.**

`DatabaseBootstrapSettings`와 `RunnerAuthBootstrapSettings`를 별도 dataclass로
정의한다. `bootstrap_api_database()`는 DB 비밀번호만 읽고 mode `0600`의
`$ORCH_RUNTIME_DIR/db.env`만 쓴다. `bootstrap_runner_codex_auth()`는 OAuth 시크릿을
`$CODEX_HOME/auth.json`이 없을 때만 mode `0600`으로 쓰며 기존 regular file을
덮어쓰지 않는다. 두 함수의 오류·로그에는 시크릿 resource ID만 포함하고 본문은
포함하지 않는다.

API `entrypoint.sh`는 `db.env`의 단일 `ORCH_DATABASE_URL=` 행을 셸 평가 없이 읽은 뒤
API uvicorn을 exec한다. Runner
`runner_entrypoint.sh`는 DB 파일을 읽지 않고 다음만 실행한다.

```sh
exec uvicorn agent_orchestration.runner.app:app --host 0.0.0.0 --port 8080
```

- [ ] **Step 4: 테스트·lint·커밋을 수행한다.**

```bash
uv run python -m pytest tests/test_agent_orchestration_bootstrap.py -q
uv run --no-sync ruff check agent_orchestration/bootstrap_secrets.py tests/test_agent_orchestration_bootstrap.py
git add agent_orchestration/bootstrap_secrets.py agent_orchestration/entrypoint.sh agent_orchestration/runner_entrypoint.sh tests/test_agent_orchestration_bootstrap.py
git commit -m "refactor: 오케스트레이션 시크릿 부트스트랩 분리"
```

Expected: API bootstrap은 OAuth 시크릿을 읽지 않고 Runner bootstrap은 DB 시크릿을 읽지 않는다.

### Task 3: 분리된 API·Runner 이미지와 CI 계약 추가

**Files:**
- Create: `deploy/agent_orchestration/api.Dockerfile`
- Create: `deploy/agent_orchestration/runner.Dockerfile`
- Create: `tests/test_agent_orchestration_container.py`
- Modify: `.dockerignore`, `.github/workflows/ci.yml`, `agent_orchestration/README.md`

**Consumes:** Task 1·2 코드와 root `pyproject.toml`/`uv.lock`.

**Produces:** `autoresearch-agent-orchestration-api:ci`, `autoresearch-agent-orchestration-runner:ci` non-root 이미지.

- [ ] **Step 1: 실패하는 이미지 계약 테스트를 작성한다.**

```python
def test_api_image_excludes_codex_and_runner_image_pins_codex() -> None:
    assert "@openai/codex" not in Path("deploy/agent_orchestration/api.Dockerfile").read_text()
    assert "@openai/codex@0.146.0" in Path("deploy/agent_orchestration/runner.Dockerfile").read_text()
    assert "USER appuser" in both_dockerfiles
```

추가 검사로 두 Dockerfile이 `auth.json`, `.env`, DB URL을 COPY·ARG·ENV로 포함하지
않고 `.dockerignore`가 `.codex`와 `.env`를 제외하는지 확인한다.

- [ ] **Step 2: 실패를 확인한다.**

```bash
uv run python -m pytest tests/test_agent_orchestration_container.py -q
```

Expected: Dockerfile 부재로 실패한다.

- [ ] **Step 3: multi-stage Dockerfile을 구현한다.**

두 이미지 모두 uv export의 `orchestration` 그룹만 설치하고 UID/GID `10001`의
`appuser`로 실행한다. API 이미지는 `agent_orchestration/app`, bootstrap, API
entrypoint만 복사한다. Runner 이미지는 node stage에서 고정 Codex CLI를 설치하고
Runner·Codex 실행 모듈·Runner entrypoint만 복사한다. Runner의 기본 `CODEX_HOME`은
`/var/lib/codex`, scratch 경로는 `/tmp`이며 Kubernetes가 전용 볼륨으로 마운트한다.

- [ ] **Step 4: build·smoke·CI를 검증하고 커밋한다.**

```bash
docker build -f deploy/agent_orchestration/api.Dockerfile -t autoresearch-agent-orchestration-api:ci .
docker build -f deploy/agent_orchestration/runner.Dockerfile -t autoresearch-agent-orchestration-runner:ci .
docker run --rm autoresearch-agent-orchestration-api:ci python -c "import agent_orchestration.app.main"
docker run --rm autoresearch-agent-orchestration-runner:ci codex --version
docker run --rm autoresearch-agent-orchestration-runner:ci python -c "import agent_orchestration.runner.app"
uv run python -m pytest tests/test_agent_orchestration_container.py -q
git add deploy/agent_orchestration tests/test_agent_orchestration_container.py .dockerignore .github/workflows/ci.yml agent_orchestration/README.md
git commit -m "feat: 오케스트레이션 API와 runner 이미지 추가"
```

Expected: 두 이미지는 non-root로 실행되고 API에는 Codex binary가 없으며 Runner만 Codex 버전을 출력한다.

### Task 4: GAR release와 운영 문서 갱신

**Files:**
- Modify: `.github/workflows/release.yml`
- Modify: `docs/guides/release-pipeline.md`
- Modify: `tests/test_agent_orchestration_container.py`

**Consumes:** Task 3 API·Runner Dockerfiles.

**Produces:** `autoresearch-agent-orchestration-api@sha256:...`와 `autoresearch-agent-orchestration-runner@sha256:...` release digest.

- [ ] **Step 1: release workflow 계약 테스트를 작성하고 실패를 확인한다.**

```python
def test_release_workflow_publishes_api_and_runner_digests() -> None:
    workflow = Path(".github/workflows/release.yml").read_text()
    assert "publish-agent-orchestration-api-image" in workflow
    assert "publish-agent-orchestration-runner-image" in workflow
```

```bash
uv run python -m pytest tests/test_agent_orchestration_container.py::test_release_workflow_publishes_api_and_runner_digests -q
```

Expected: 두 release job 부재로 실패한다.

- [ ] **Step 2: immutable release job을 추가한다.**

기존 `publish-serving-image`의 immutable checkout·WIF·Buildx 패턴을 각각
`autoresearch-agent-orchestration-api`와 `autoresearch-agent-orchestration-runner`
URI에 적용한다. 각 verify는 digest, OCI revision, non-root user, 해당 import를
검사하며 Runner verify만 `codex --version`을 실행한다. summary에는 digest와 source
SHA만 기록한다.

- [ ] **Step 3: 검증·커밋을 수행한다.**

```bash
actionlint .github/workflows/release.yml
git diff --check
uv run python -m pytest tests/test_agent_orchestration_container.py -q
git add .github/workflows/release.yml docs/guides/release-pipeline.md tests/test_agent_orchestration_container.py
git commit -m "feat: 오케스트레이션 분리 이미지 release 발행"
```

Expected: workflow 문법과 이미지 release 계약 테스트가 통과한다.

### Task 5: 인프라 저장소의 최소 권한·내부 네트워크 배포

**Files:**
- Create or modify in `SKYAHO/Autoresearch-infra`: `terraform/envs/dev/{cloud_sql,secret_manager,gke,locals,variables,outputs}.tf`
- Create or modify in `SKYAHO/Autoresearch-infra`: `terraform/admin/autoresearch-k8s/{main,variables,locals,outputs}.tf`
- Create: `SKYAHO/Autoresearch-infra/deploy/agent-orchestration/{api-deployment,runner-deployment,api-service,runner-service,network-policy}.yaml`
- Create: `SKYAHO/Autoresearch-infra/docs/runbooks/2026-07-30-agent-orchestration-gke.md`

**Consumes:** Task 4 API·Runner immutable digest와 이 문서의 GSA/KSA/PVC 계약.

**Produces:** Cloud SQL 전용 DB/user, API GSA/KSA, Runner GSA/KSA, OAuth PVC, private services와 restricted NetworkPolicy.

- [ ] **Step 1: 인프라 저장소에서 #432을 참조하는 feature 이슈와 Create-a-branch 브랜치를 만든다.**

이슈 완료 조건은 API GSA의 DB 접근, Runner GSA의 OAuth 시크릿 단일 접근, API/Runner
서로 다른 KSA, `ReadWriteOnce` PVC, ClusterIP 두 개, API→Runner ingress만 허용하는
NetworkPolicy, dry-run과 Terraform plan이다.

- [ ] **Step 2: Terraform plan의 실패 기준을 기록한다.**

```bash
terraform -chdir=terraform/envs/dev plan -out=/tmp/agent-orchestration.tfplan
terraform -chdir=terraform/envs/dev show -json /tmp/agent-orchestration.tfplan | jq -e '[.resource_changes[].address] | index("google_sql_database.agent_orchestration")'
```

Expected before implementation: 신규 DB resource가 없어 `jq`가 실패한다.

- [ ] **Step 3: 최소 권한 Terraform과 manifest를 구현한다.**

DB/user `agent_orchestration`/`agent_orchestration_app`와 DB 비밀번호 시크릿을
생성한다. OAuth bootstrap 시크릿은 Terraform이 payload를 관리하지 않는
`autoresearch-dev-agent-orchestration-codex-auth-bootstrap`으로 만들고
`prevent_destroy = true`를 둔다. API GSA에는 `roles/cloudsql.client`와 DB password
시크릿의 accessor만, Runner GSA에는 OAuth 시크릿의 accessor만 준다.

API Deployment는 `LLM_BACKEND=codex_runner`, Runner ClusterIP URL, DB 관련
비민감 좌표를 받으며 OAuth PVC를 마운트하지 않는다. Runner init container는
`bootstrap_runner_codex_auth`을 실행하고, main container만 OAuth PVC를
`/var/lib/codex`로 마운트한다. Runner Deployment는 `replicas: 1`, PVC는 1Gi
`ReadWriteOnce`, 두 Service는 `ClusterIP`다. NetworkPolicy는 API→Runner:8080,
API→Cloud SQL:5432, API/Runner의 DNS·필요 HTTPS만 허용하고 외부 ingress를 만들지
않는다.

- [ ] **Step 4: Terraform·manifest·runbook을 검증하고 커밋한다.**

```bash
terraform -chdir=terraform/envs/dev fmt -check
terraform -chdir=terraform/envs/dev validate
terraform -chdir=terraform/admin/autoresearch-k8s fmt -check
terraform -chdir=terraform/admin/autoresearch-k8s validate
kubectl apply --dry-run=client -f deploy/agent-orchestration/api-deployment.yaml
kubectl apply --dry-run=client -f deploy/agent-orchestration/runner-deployment.yaml
```

Expected: 기존 Cloud SQL instance 교체 없이 신규 DB/user/시크릿/GSA/KSA/PVC와
internal-only workload만 plan에 나타난다.

### Task 6: dev 배포·저장 경로 검증

**Files:**
- Modify: `SKYAHO/Autoresearch-infra/docs/runbooks/2026-07-30-agent-orchestration-gke.md`

**Consumes:** Task 4 immutable digests and Task 5 applied infrastructure.

**Produces:** 재현 가능한 healthcheck/chat/DB 저장/rollback 검증 기록.

- [ ] **Step 1: OAuth 초기 인증 시크릿을 안전한 운영 절차로 등록한다.**

신뢰된 로컬 환경에서 `CODEX_HOME`을 별도 임시 경로로 설정해 `codex login` 후
`auth.json`을 얻는다. 값은 터미널 출력·Git·티켓에 붙이지 않고 Secret Manager의
이미 생성된 bootstrap 시크릿 version으로만 등록한다.

- [ ] **Step 2: ArgoCD로 두 immutable digest를 수동 sync하고 Ready를 확인한다.**

```bash
kubectl -n autoresearch rollout status deployment/agent-orchestration-runner --timeout=5m
kubectl -n autoresearch rollout status deployment/agent-orchestration-api --timeout=5m
```

Expected: API와 Runner가 단일 Ready Pod로 실행된다.

- [ ] **Step 3: 내부 smoke와 DB 메타데이터 저장을 검증한다.**

```bash
kubectl -n autoresearch port-forward service/agent-orchestration-api 8000:8000
curl --fail --silent http://127.0.0.1:8000/healthcheck
curl --fail --silent --request POST http://127.0.0.1:8000/chat --header "Content-Type: application/json" --header "X-Orch-Token: ${ORCH_API_TOKEN}" --data '{"prompt":"한 문장으로 상태를 알려주세요."}'
```

Cloud SQL에서는 `id, model, latency_ms, created_at`만 조회한다. 프롬프트·응답·토큰·OAuth
본문을 출력하지 않는다.

- [ ] **Step 4: rollback·시크릿 복구 절차를 기록하고 완료 조건을 확인한다.**

이전 API/Runner digest로의 ArgoCD sync, Runner Deployment를 0으로 축소한 뒤 OAuth
PVC의 인증 상태를 안전하게 교체하고 1로 복구하는 절차를 runbook에 검증 결과와 함께
기록한다. 최종으로 API와 Runner manifest·logs·image history·Git diff에 OAuth 본문,
DB 비밀번호, 완성 DB URL이 없는지 확인한다.

## 최종 검증 체크리스트

- [ ] `uv run python -m pytest -q` 및 `uv run --no-sync ruff check autoresearch tests tools agent_orchestration`
- [ ] API·Runner Docker build, non-root·import smoke, Runner `codex --version`, release workflow `actionlint`
- [ ] Infra Terraform `fmt -check`, `validate`, 신규 리소스만 포함한 plan, Kubernetes manifest dry-run
- [ ] API/Runner Ready, private `/healthcheck`·`/chat`, Cloud SQL 메타데이터 저장, 이전 digest rollback
- [ ] OAuth payload·DB 비밀번호·완성 DB URL이 Git·이미지·manifest·일반 로그에 없음

## Plan Self-Review

- **Spec coverage:** API/Runner 분리, 기존 Secret Manager·Workload Identity 사용, API DB와 Runner OAuth의 최소 권한, PVC, ClusterIP·NetworkPolicy, API→Runner 오류 처리, 이미지 release, 실제 dev smoke·rollback을 Task 1~6에 배정했다.
- **Placeholder scan:** 리소스명, 환경 변수, 서비스/포트, 역할별 권한과 검증 명령을 명시했다.
- **Type consistency:** `LLMResult`, `CodexSettings`, `GenerateRequest/GenerateResponse`, `codex_runner`, `CODEX_RUNNER_URL`, bootstrap 함수와 Runner 포트 8080을 모든 작업에서 동일하게 사용한다.
