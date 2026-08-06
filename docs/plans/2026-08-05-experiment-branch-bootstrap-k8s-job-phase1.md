# 실험 브랜치 Bootstrap Kubernetes Job Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 발행된 `[AR]` 이슈가 있는 `CREATED` Experiment를 CronJob launcher가 선점해 Kubernetes Job을 만들고, executor Pod가 저장된 `base_dev_sha`에서 exp branch를 생성한다.

**Architecture:** API는 read-only GitHub App으로 이슈 발행 전에 `dev` SHA를 DB에 봉인한다. launcher는 PostgreSQL advisory lock 안에서 Experiment를 `RUNNING`으로 바꾸고 결정론적 Job 이름을 저장한 뒤 Job을 생성한다. Job 존재를 확인한 시각을 별도 기록해, 선점과 생성 사이에서 launcher가 종료되어도 다음 tick이 미완료 생성만 재개한다. executor Pod에서는 initContainer만 branch-writer App private key를 받아 installation token을 메모리 파일로 전달하고, 본 컨테이너가 Git refs REST API로 branch를 생성한다.

**Tech Stack:** Python 3.11/3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, Pydantic v2, httpx, PyJWT 2.13, Kubernetes Python client 36, Kubernetes CronJob/Job, GitHub App REST API, Terraform.

## Global Constraints

- 기준 ref는 `dev`다. launcher와 executor는 Job 시작 시 최신 `dev` 또는 `main`을 기준으로 다시 읽지 않는다.
- `base_dev_sha`는 이슈 발행보다 먼저 commit하고 이후 갱신하지 않는다.
- launcher가 전달하는 좌표는 `experiment_id`, `issue_number`, `issue_branch`, `base_dev_sha` 네 개다.
- Phase 1 성공은 Kubernetes Job `Complete`와 exp ref tip 일치로 확인한다. 새 공개 status는 추가하지 않으며 Experiment는 `RUNNING`에 남는다.
- Job 완료·실패를 DB status로 회수하는 reconciler는 다음 Phase 범위다.
- 기존 ref는 tip이 `base_dev_sha`와 같을 때만 멱등 성공이다. 다른 tip을 update·reset·force-push하지 않는다.
- GitHub Actions는 `auto-experiment` label을 받아도 exp ref를 생성하지 않는다.
- baseline-reader App은 선택 저장소의 Contents read만, branch-writer App은 선택 저장소의 Contents write만 가진다.
- branch-writer private key는 token-minter initContainer에만 mount한다. executor에는 memory volume의 token 파일만 전달한다.
- token과 private key를 환경 변수, command argument, Git remote, stdout/stderr에 넣지 않는다.
- launcher만 Job create/get/list 권한을 가지며 executor의 ServiceAccount token mount는 금지한다.
- Job image는 승인된 Artifact Registry의 sha256 digest로 고정한다.
- 최초 live 동시 실행 상한은 현재 namespace quota와 같은 `2`다. 설계 예시의 5개 동시는 launcher 단위 테스트로 검증하고 live 상향은 별도 infra 변경으로 한다.
- Codex, checkout, 코드 수정, test/lint, commit, push, candidate SHA, Airflow 평가, GitHub marker 재설계는 포함하지 않는다.
- 새 runtime 모듈의 최상단 docstring은 파이프라인 위치, 제공 기능, 인접 비책임을 모두 선언한다.

---

## File Map

### `SKYAHO/Autoresearch`

| 파일 | 책임 |
| --- | --- |
| `agent_orchestration/migrations/versions/0004_experiment_branch_bootstrap.py` | `base_dev_sha`, `executor_job_name`, `executor_job_created_at` 컬럼 |
| `agent_orchestration/app/experiments/models.py` | 기준 SHA와 Job 생성 lineage 컬럼의 ORM 계약 |
| `agent_orchestration/app/experiments/schemas.py` | API 응답의 기준 SHA·Job 이름 |
| `agent_orchestration/app/experiments/service.py` | 이슈 발행 전 기준 SHA 봉인 |
| `agent_orchestration/app/config.py` | baseline-reader App 설정 |
| `agent_orchestration/github_app.py` | App JWT와 installation token 발급 |
| `agent_orchestration/github_refs.py` | Git ref 조회·생성 REST 경계 |
| `agent_orchestration/executor/config.py` | 네 좌표와 token 파일 검증 |
| `agent_orchestration/executor/token_minter.py` | initContainer token 파일 생성 |
| `agent_orchestration/executor/main.py` | exp ref 생성·멱등 검증 |
| `agent_orchestration/launcher/config.py` | DB·namespace·image·동시 상한 설정 |
| `agent_orchestration/launcher/repository.py` | advisory lock과 `CREATED → RUNNING` 선점 |
| `agent_orchestration/launcher/jobs.py` | 고정 Kubernetes Job manifest·API 경계 |
| `agent_orchestration/launcher/main.py` | 1회 launcher tick |
| `deploy/agent_orchestration/executor.Dockerfile` | token-minter와 branch executor 이미지 |
| `deploy/agent_orchestration/launcher.Dockerfile` | launcher 이미지 |
| `.github/workflows/release.yml` | 두 image digest 게시 |
| `.github/workflows/auto-research-issue-branch.yml` | 삭제할 기존 branch 생성 workflow |
| `tests/test_experiment_branch_migration.py` | migration 대칭성 |
| `tests/test_experiment_branch_baseline.py` | SHA 선커밋·재시도 |
| `tests/test_github_app.py`, `tests/test_github_refs.py` | GitHub 인증·ref API |
| `tests/test_experiment_executor.py` | executor fail-closed 계약 |
| `tests/test_experiment_launcher.py` | 선점·상한·Job manifest |
| `tests/test_agent_orchestration_container.py` | 이미지·release 계약 |
| `tests/test_auto_research_issue_branch.py`, `tests/test_auto_experiment_trigger_label.py` | Actions 주체 제거와 parser·label 유지 |
| `.env.example`, `README.md`, `.claude/docs/agent-project-reference.md` | 이미지·환경 변수 정본 |
| `.claude/docs/agent-workflow-reference.md`, `CONTRIBUTING.md` | branch 생성 주체 정정 |

### `SKYAHO/Autoresearch-infra` companion change

| 파일 | 책임 |
| --- | --- |
| `deploy/agent-orchestration/launcher-cronjob.yaml` | 1분 주기 launcher |
| `terraform/admin/autoresearch-k8s/experiment_jobs.tf` | launcher/executor KSA·RBAC·admission·egress |
| `terraform/admin/autoresearch-k8s/variables.tf` | launcher/executor 설정 검증 |
| `terraform/envs/dev/gke.tf` | launcher GSA·Cloud SQL client·Workload Identity |
| `terraform/envs/dev/secret_manager.tf` | launcher DB password 단일 Secret accessor |
| `terraform/envs/dev/locals.tf` | launcher identity 이름 파생 |
| `deploy/agent-orchestration/api-deployment.yaml` | baseline-reader App private key mount |
| `deploy/agent-orchestration/api-migration-job.yaml` | revision 0004 API digest |
| `docs/runbooks/2026-08-01-auto-research-experiment-job.md` | 운영·장애·token 회수 절차 |

---

### Task 1: 기준 SHA와 결정론적 Job 좌표 저장

**Files:**
- Create: `agent_orchestration/migrations/versions/0004_experiment_branch_bootstrap.py`
- Create: `tests/test_experiment_branch_migration.py`
- Create: `tests/test_experiment_branch_baseline.py`
- Create: `agent_orchestration/github_app.py`
- Create: `agent_orchestration/github_refs.py`
- Create: `tests/test_github_app.py`
- Create: `tests/test_github_refs.py`
- Modify: `agent_orchestration/app/experiments/models.py`
- Modify: `agent_orchestration/app/experiments/schemas.py`
- Modify: `agent_orchestration/app/experiments/service.py`
- Modify: `agent_orchestration/app/experiments/router.py`
- Modify: `agent_orchestration/app/config.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Produces: `Experiment.base_dev_sha: str | None`, `Experiment.executor_job_name: str | None`, `Experiment.executor_job_created_at: datetime | None`.
- Produces: `GitHubAppCredentials`, `InstallationToken`, `create_installation_token()`.
- Produces: `GitHubRefs.get_sha(repository, ref, token)`, `GitHubRefs.create(repository, ref, sha, token)`.
- Changes: `publish_experiment_issue()`가 body·title·base SHA를 외부 issue create보다 먼저 commit.

- [ ] **Step 1: migration 실패 테스트를 작성한다**

`tests/test_experiment_branch_migration.py`는 기존 migration recorder 형식으로 다음을 단언한다.

```python
def test_upgrade_adds_branch_bootstrap_columns(recorder) -> None:
    revision = load_revision("0004_experiment_branch_bootstrap")
    recorder.run(revision.upgrade)
    assert recorder.added_columns == [
        ("experiments", "base_dev_sha", "VARCHAR(40)", True),
        ("experiments", "executor_job_name", "VARCHAR(63)", True),
        ("experiments", "executor_job_created_at", "DATETIME", True),
    ]


def test_downgrade_removes_branch_bootstrap_columns_in_reverse_order(recorder) -> None:
    revision = load_revision("0004_experiment_branch_bootstrap")
    recorder.run(revision.downgrade)
    assert recorder.dropped_columns == [
        ("experiments", "executor_job_created_at"),
        ("experiments", "executor_job_name"),
        ("experiments", "base_dev_sha"),
    ]
```

- [ ] **Step 2: 기준선 재시도 실패 테스트를 작성한다**

```python
def test_publish_retry_reuses_frozen_sha_after_dev_moves(db_session, monkeypatch) -> None:
    experiment = create_experiment(db_session, ExperimentCreate(hypothesis="ratio"))
    resolver = AsyncMock(side_effect=["a" * 40, "b" * 40])
    monkeypatch.setattr(
        "agent_orchestration.app.experiments.service.resolve_dev_sha", resolver
    )
    _publish_with_first_github_call_failing(db_session, experiment.id)
    result = _retry_publication(db_session, experiment.id)
    assert result.base_dev_sha == "a" * 40
    assert resolver.await_count == 1
```

이 테스트 파일은 기존 `tests/test_experiment_issue_publication.py`의 SQLite Session fixture,
`IssuePublicationRequest` fixture와 fake `create_issue()`를 그대로 import하지 않고 파일 안에
동일 타입의 fixture를 명시적으로 정의한다.

- [ ] **Step 3: GitHub App 최소 권한 요청 실패 테스트를 작성한다**

```python
@pytest.mark.asyncio
async def test_installation_token_requests_only_supplied_permissions(tmp_path: Path) -> None:
    key_path = tmp_path / "app.pem"
    key_path.write_text(TEST_RSA_PRIVATE_KEY, encoding="utf-8")
    transport = RecordingTransport(
        httpx.Response(
            201,
            json={"token": "secret-token", "expires_at": "2026-08-05T01:00:00Z"},
        )
    )
    token = await create_installation_token(
        GitHubAppCredentials(123, 456, key_path),
        permissions={"contents": "read"},
        transport=transport,
    )
    assert transport.request.url.path == "/app/installations/456/access_tokens"
    assert json.loads(transport.request.content) == {
        "permissions": {"contents": "read"}
    }
    assert token.value == "secret-token"
```

`TEST_RSA_PRIVATE_KEY`는 tests 전용 즉석 키 fixture이며 실제 App key를 복사하지 않는다.

- [ ] **Step 4: 세 실패 테스트를 실행한다**

Run: `uv run python -m pytest tests/test_experiment_branch_migration.py tests/test_experiment_branch_baseline.py tests/test_github_app.py tests/test_github_refs.py -v`

Expected: revision과 GitHub 모듈이 없어 FAIL.

- [ ] **Step 5: migration과 ORM·response를 구현한다**

`0004`는 `down_revision = "0003_experiment_issue_lineage"`로 `base_dev_sha`,
`executor_job_name`, timezone-aware `executor_job_created_at` nullable 컬럼을 추가한다.
`ExperimentResponse`에는 `base_dev_sha`와 `executor_job_name`을 nullable로,
`IssuePublicationResponse`에는 `base_dev_sha: str`을 추가한다. 생성 확인 시각은 내부 복구
표식이므로 API에 노출하지 않는다. 기존 행은 launcher 대상에서 제외한다.

- [ ] **Step 6: GitHub App 인증·ref 클라이언트를 구현한다**

```python
@dataclass(frozen=True)
class GitHubAppCredentials:
    app_id: int
    installation_id: int
    private_key_path: Path


@dataclass(frozen=True)
class InstallationToken:
    value: str
    expires_at: datetime
```

App JWT는 `iat=now-60`, `exp=now+540`, `iss=str(app_id)`, `RS256`으로 서명한다. token
endpoint에는 호출자가 넘긴 permissions만 보낸다. 오류에는 token, Authorization header,
response body 원문을 포함하지 않는다. `github_refs.py`는 404를 `None`, 다른 오류를
`GitHubRefError`, 성공 SHA를 `^[0-9a-f]{40}$`로 검증한다.

- [ ] **Step 7: API 설정과 이슈 발행 순서를 구현한다**

```text
ORCH_BASELINE_GITHUB_APP_ID
ORCH_BASELINE_GITHUB_APP_INSTALLATION_ID
ORCH_BASELINE_GITHUB_APP_PRIVATE_KEY_PATH
```

`publish_experiment_issue()`는 body/title을 만들고, `base_dev_sha`가 null일 때만 read-only
installation token으로 `heads/dev`를 읽은 뒤 세 값을 commit한다. 그 다음에 기존
`find_issue_by_marker()`·`create_issue()`를 호출한다. 재호출은 저장된 SHA를 사용한다.

- [ ] **Step 8: 의존성과 lock을 갱신한다**

`pyproject.toml` orchestration group에 아래를 추가하고 `uv lock`을 실행한다.

```toml
"PyJWT[crypto]>=2.13,<3",
```

- [ ] **Step 9: Task 1 테스트를 통과시킨다**

Run: `uv run python -m pytest tests/test_experiment_branch_migration.py tests/test_experiment_branch_baseline.py tests/test_github_app.py tests/test_github_refs.py tests/test_experiment_issue_publication.py tests/test_experiment_issue_endpoint.py -v`

Expected: 발행 실패·dev 이동 뒤에도 최초 SHA 유지, API 응답에 SHA 포함, token 비노출.

- [ ] **Step 10: Task 1 커밋을 만든다**

```bash
git add agent_orchestration/migrations/versions/0004_experiment_branch_bootstrap.py agent_orchestration/app/experiments agent_orchestration/app/config.py agent_orchestration/github_app.py agent_orchestration/github_refs.py pyproject.toml uv.lock tests/test_experiment_branch_migration.py tests/test_experiment_branch_baseline.py tests/test_github_app.py tests/test_github_refs.py
git commit -m "feat: 실험 기준 dev SHA를 발행 전에 봉인한다"
```

---

### Task 2: executor Pod branch 생성 구현

**Files:**
- Create: `agent_orchestration/executor/__init__.py`
- Create: `agent_orchestration/executor/config.py`
- Create: `agent_orchestration/executor/token_minter.py`
- Create: `agent_orchestration/executor/main.py`
- Create: `tests/test_experiment_executor.py`

**Interfaces:**
- Consumes: Task 1의 `GitHubAppCredentials`, `create_installation_token()`, `GitHubRefs`.
- Produces: `BranchBootstrapInput.from_environment()`, `bootstrap_branch()`.
- Exit 0: ref 생성 또는 same-SHA 기존 ref. Exit 1: 입력·인증·different-SHA·GitHub 오류.

- [ ] **Step 1: 입력·ref 멱등 실패 테스트를 작성한다**

```python
def test_executor_rejects_missing_base_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_executor_environment(monkeypatch)
    monkeypatch.delenv("ORCH_BASE_DEV_SHA")
    with pytest.raises(ExecutorConfigError, match="ORCH_BASE_DEV_SHA"):
        BranchBootstrapInput.from_environment()


@pytest.mark.asyncio
async def test_existing_ref_at_same_sha_is_success() -> None:
    refs = FakeRefs(existing_sha="a" * 40)
    result = await bootstrap_branch(_valid_input("a" * 40), refs, "token")
    assert result.created is False
    assert refs.create_calls == []


@pytest.mark.asyncio
async def test_existing_ref_at_different_sha_never_updates() -> None:
    refs = FakeRefs(existing_sha="b" * 40)
    with pytest.raises(BranchConflictError):
        await bootstrap_branch(_valid_input("a" * 40), refs, "token")
    assert refs.create_calls == []
```

테스트 파일은 `_set_valid_executor_environment`, `_valid_input`, `FakeRefs`를 파일 상단에
정의하며 `FakeRefs`는 `get_sha_calls`, `create_calls`를 기록한다.

- [ ] **Step 2: token 파일 보안 실패 테스트를 작성한다**

```python
@pytest.mark.asyncio
async def test_token_minter_writes_0400_without_printing_token(tmp_path, capsys) -> None:
    output = tmp_path / "token"
    await write_installation_token(
        credentials=_test_credentials(tmp_path),
        output=output,
        permissions={"contents": "write"},
        token_factory=_fake_token_factory,
    )
    assert stat.S_IMODE(output.stat().st_mode) == 0o400
    assert output.read_text(encoding="utf-8") == "secret-token"
    captured = capsys.readouterr()
    assert "secret-token" not in captured.out + captured.err
```

- [ ] **Step 3: executor 테스트 실패를 확인한다**

Run: `uv run python -m pytest tests/test_experiment_executor.py -v`

Expected: executor package가 없어 FAIL.

- [ ] **Step 4: executor 입력 계약을 구현한다**

```python
@dataclass(frozen=True)
class BranchBootstrapInput:
    experiment_id: uuid.UUID
    issue_number: int
    issue_branch: str
    base_dev_sha: str
    github_repository: str
    token_file: Path
```

검증식은 issue number 양수, branch `^exp/[0-9]+-[a-z0-9]+(?:-[a-z0-9]+)*$`, SHA
`^[0-9a-f]{40}$`, repository `owner/repo`, token regular file이다.

- [ ] **Step 5: token-minter와 branch 생성을 구현한다**

```text
ORCH_GITHUB_APP_ID
ORCH_GITHUB_APP_INSTALLATION_ID
ORCH_GITHUB_APP_PRIVATE_KEY_FILE=/var/run/secrets/github-app/private-key.pem
ORCH_GITHUB_TOKEN_FILE=/var/run/github-token/token
```

token-minter는 temp 파일을 0400으로 만든 뒤 같은 memory volume에서 `os.replace()`한다.
executor는 token 파일을 읽고 ref 조회 → 없으면 create → 422 경합이면 한 번 재조회 순서로
동작한다. 로그에는 experiment ID, issue number, branch, base SHA와 `created` boolean만 쓴다.

- [ ] **Step 6: executor 테스트를 통과시킨다**

Run: `uv run python -m pytest tests/test_experiment_executor.py tests/test_github_app.py tests/test_github_refs.py -v`

Expected: same-SHA 멱등 성공, different-SHA fail-closed, token 비노출.

- [ ] **Step 7: Task 2 커밋을 만든다**

```bash
git add agent_orchestration/executor tests/test_experiment_executor.py
git commit -m "feat: Pod 내부 실험 브랜치 생성을 추가한다"
```

---

### Task 3: CronJob launcher의 최소 선점·Job 생성 구현

**Files:**
- Create: `agent_orchestration/launcher/__init__.py`
- Create: `agent_orchestration/launcher/config.py`
- Create: `agent_orchestration/launcher/repository.py`
- Create: `agent_orchestration/launcher/jobs.py`
- Create: `agent_orchestration/launcher/main.py`
- Create: `tests/test_experiment_launcher.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`

**Interfaces:**
- Produces: `claim_experiments() -> list[ClaimedExperiment]`.
- Produces: `build_branch_job(claim, settings) -> V1Job`.
- Consumes: Task 1의 네 좌표와 `Experiment.executor_job_name`, `Experiment.executor_job_created_at`.

- [ ] **Step 1: 선점·상한 실패 테스트를 작성한다**

```python
def test_claim_skips_incomplete_coordinates(session: Session) -> None:
    _created_experiment(session, issue_number=546, issue_branch="exp/546-x", base_sha=None)
    assert claim_experiments(session, active_jobs=0, max_concurrency=5) == []


def test_claim_reserves_only_available_slots(session: Session) -> None:
    _created_experiments(session, count=100)
    claims = claim_experiments(session, active_jobs=4, max_concurrency=5)
    assert len(claims) == 1
    assert claims[0].job_name == f"ar-branch-{claims[0].experiment_id.hex}"


def test_tick_recovers_claim_when_job_creation_was_not_confirmed(session: Session) -> None:
    experiment = _running_experiment(
        session,
        job_name=f"ar-branch-{EXPERIMENT_ID.hex}",
        job_created_at=None,
    )
    kubernetes = FakeJobs(existing_names=set())
    run_tick(session, kubernetes, _settings())
    assert kubernetes.created_names == {experiment.executor_job_name}
    assert experiment.executor_job_created_at is not None


def test_tick_does_not_recreate_ttl_deleted_confirmed_job(session: Session) -> None:
    _running_experiment(
        session,
        job_name=f"ar-branch-{EXPERIMENT_ID.hex}",
        job_created_at=UTC_NOW,
    )
    kubernetes = FakeJobs(existing_names=set())
    run_tick(session, kubernetes, _settings())
    assert kubernetes.created_names == set()
```

테스트 파일은 `FakeJobs`와 고정 UTC clock을 파일 안에 정의한다. `claim_experiments()`는
SQLite로 transaction 결과를 검증하고, PostgreSQL advisory lock SQL 자체는 statement
compile 단언으로 고정한다.

- [ ] **Step 2: Job manifest 실패 테스트를 작성한다**

```python
def test_job_passes_only_frozen_coordinates_and_token_file() -> None:
    job = build_branch_job(_claim(), _settings())
    pod = job.spec.template.spec
    assert job.metadata.name == f"ar-branch-{EXPERIMENT_ID.hex}"
    assert job.spec.backoff_limit == 0
    assert pod.automount_service_account_token is False
    assert [container.name for container in pod.init_containers] == ["github-token-minter"]
    assert [container.name for container in pod.containers] == ["branch-bootstrap"]
    assert _environment(pod.containers[0]) == {
        "ORCH_EXPERIMENT_ID": str(EXPERIMENT_ID),
        "ORCH_ISSUE_NUMBER": "546",
        "ORCH_ISSUE_BRANCH": "exp/546-example",
        "ORCH_BASE_DEV_SHA": "a" * 40,
        "ORCH_GITHUB_REPOSITORY": "SKYAHO/Autoresearch",
        "ORCH_GITHUB_TOKEN_FILE": "/var/run/github-token/token",
    }
```

- [ ] **Step 3: launcher 테스트 실패를 확인한다**

Run: `uv run python -m pytest tests/test_experiment_launcher.py -v`

Expected: launcher package가 없어 FAIL.

- [ ] **Step 4: launcher 설정과 의존성을 구현한다**

```python
@dataclass(frozen=True)
class LauncherSettings:
    database_url: str
    job_namespace: str
    executor_image: str
    executor_service_account: str
    github_app_secret_name: str
    github_app_id: int
    github_app_installation_id: int
    github_repository: str
    max_concurrent_experiments: int
    active_deadline_sec: int = 300
    ttl_after_finished_sec: int = 30
```

`executor_image`는 `@sha256:<64 hex>`만 허용한다. orchestration group에
`"kubernetes>=36,<37"`을 추가하고 `uv lock`을 실행한다.

- [ ] **Step 5: 최소 선점을 구현한다**

한 tick에서 `pg_try_advisory_xact_lock(546, 1)`을 얻지 못하면 아무 행도 선점하지 않고
정상 종료한다. lock을 얻으면 먼저 아래 조건의 미완료 생성을 복구한다.

```text
status == RUNNING
executor_job_name IS NOT NULL
executor_job_created_at IS NULL
ORDER BY updated_at ASC, id ASC
```

각 행은 같은 이름의 Job을 조회해 이미 있으면 생성 시각만 기록하고, 없으면 동일 좌표로
생성한다. 그 뒤 caller가 Kubernetes에서 센 active Job 수를 뺀 슬롯만큼 다음 query를
수행한다.

```text
status == CREATED
issue_number IS NOT NULL
issue_branch IS NOT NULL
base_dev_sha IS NOT NULL
executor_job_name IS NULL
ORDER BY created_at ASC, id ASC
FOR UPDATE SKIP LOCKED
```

각 행에 `executor_job_name=ar-branch-<uuid hex>`를 저장하고
`executor_job_created_at=NULL`을 유지한 채 기존 service 전이 함수를 통해
`CREATED → RUNNING` event를 같은 transaction에 기록한다.

- [ ] **Step 6: 결정론적 Job create를 구현한다**

launcher는 namespace에서 `app.kubernetes.io/component=branch-bootstrap` label의 active
Job을 세고 남은 슬롯만 claim한다. claim 뒤 같은 이름 Job을 먼저 조회하며, 있으면 create를
건너뛴다. 없으면 Task 2의 init/app 두 컨테이너와 네 좌표로 Job을 만든다. create 409는
같은 이름 Job을 다시 조회해 확인한 경우에만 멱등 성공으로 처리한다. Job이 존재하거나 생성된
것을 확인한 뒤 `executor_job_created_at`을 현재 UTC 시각으로 저장한다. 그 전에 launcher가
종료되면 다음 tick의 미완료 생성 query가 같은 이름과 봉인 좌표로 재개한다. 생성 시각이 있는
행은 TTL로 Job이 삭제되어도 다시 만들지 않는다. 다른 오류는 non-zero exit로 CronJob 실패를
남긴다. Job 완료·실패에 따른 DB status 자동 회수는 하지 않는다.

- [ ] **Step 7: launcher 테스트를 통과시킨다**

Run: `uv run python -m pytest tests/test_experiment_launcher.py -v`

Expected: 설정 상한 5에서 active 4면 하나만 선점, 좌표 누락 제외, 이름·manifest 고정,
선점 직후 종료 복구, 생성 시각이 기록된 행의 TTL 삭제 후 미재생성.

- [ ] **Step 8: Task 3 커밋을 만든다**

```bash
git add agent_orchestration/launcher pyproject.toml uv.lock tests/test_experiment_launcher.py
git commit -m "feat: 실험 브랜치 Job launcher를 추가한다"
```

---

### Task 4: GitHub Actions 제거와 두 runtime image 게시

**Files:**
- Delete: `.github/workflows/auto-research-issue-branch.yml`
- Create: `deploy/agent_orchestration/launcher.Dockerfile`
- Create: `deploy/agent_orchestration/executor.Dockerfile`
- Modify: `.github/workflows/release.yml`
- Modify: `tests/test_auto_research_issue_branch.py`
- Modify: `tests/test_auto_experiment_trigger_label.py`
- Modify: `tests/test_agent_orchestration_container.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `.claude/docs/agent-project-reference.md`
- Modify: `.claude/docs/agent-workflow-reference.md`
- Modify: `CONTRIBUTING.md`
- Modify: `docs/specs/2026-08-04-hypothesis-to-auto-research-issue.md`
- Modify: `docs/README.md`

**Interfaces:**
- Removes: GitHub Actions `createRef`와 branch marker 생성.
- Preserves: Issue Form parser, `auto-experiment` 분류 label, promotion label guard.
- Produces: `autoresearch-agent-orchestration-launcher`, `autoresearch-agent-orchestration-executor` digest.

- [ ] **Step 1: workflow 부재·image 실패 테스트를 작성한다**

```python
def test_issue_label_has_no_branch_creation_workflow() -> None:
    assert not ISSUE_BRANCH_WORKFLOW.exists()


def test_release_publishes_launcher_and_executor_images() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    assert "publish-agent-orchestration-launcher-image:" in workflow
    assert "publish-agent-orchestration-executor-image:" in workflow
    assert "file: deploy/agent_orchestration/launcher.Dockerfile" in workflow
    assert "file: deploy/agent_orchestration/executor.Dockerfile" in workflow
```

- [ ] **Step 2: 실패를 확인한다**

Run: `uv run python -m pytest tests/test_auto_experiment_trigger_label.py tests/test_auto_research_issue_branch.py tests/test_agent_orchestration_container.py -v`

Expected: workflow가 존재하고 새 images가 없어 FAIL.

- [ ] **Step 3: branch workflow와 해당 테스트만 제거한다**

`tests/test_auto_research_issue_branch.py`의 workflow YAML·marker·createRef 단언은 제거한다.
Issue Form parser, `branch_name_for()`, candidate 선택, descendant 검증은 유지한다.
`auto-experiment`는 API와 Issue Form의 분류 label로 남긴다.

- [ ] **Step 4: 두 Dockerfile과 release jobs를 구현한다**

두 이미지는 uv 0.11.26 lock-export, orchestration group, UID/GID 10001, revision label을
사용하며 Codex를 설치하지 않는다. launcher command는
`python -m agent_orchestration.launcher.main`, executor command는
`python -m agent_orchestration.executor.main`이다. release는 각 Dockerfile을 build/push한
뒤 digest 형식과 해당 module import를 검증한다.

- [ ] **Step 5: 문서 정본을 갱신한다**

모든 문서의 branch 생성 주체를 executor Pod로 바꾼다. 기존 marker가 없는 Phase 1
branch는 promotion workflow의 입력이 아니며 marker 재설계가 다음 Phase gate임을 명시한다.
새 환경 변수는 역할별 표로 `.env.example`, README, project reference에 기록한다.

- [ ] **Step 6: Task 4 검증을 실행한다**

Run: `uv run python -m pytest tests/test_auto_experiment_trigger_label.py tests/test_auto_research_issue_branch.py tests/test_agent_orchestration_container.py -v`

Run: `docker build -f deploy/agent_orchestration/launcher.Dockerfile -t autoresearch-launcher:ci .`

Run: `docker build -f deploy/agent_orchestration/executor.Dockerfile -t autoresearch-executor:ci .`

Run: `actionlint`

Run: `git diff --check`

Expected: tests·두 build·설치된 환경의 actionlint·whitespace 검사 PASS.

- [ ] **Step 7: Task 4 커밋을 만든다**

```bash
git add .github/workflows/auto-research-issue-branch.yml .github/workflows/release.yml deploy/agent_orchestration tests/test_auto_research_issue_branch.py tests/test_auto_experiment_trigger_label.py tests/test_agent_orchestration_container.py .env.example README.md .claude/docs/agent-project-reference.md .claude/docs/agent-workflow-reference.md CONTRIBUTING.md docs/specs/2026-08-04-hypothesis-to-auto-research-issue.md docs/README.md
git commit -m "feat: exp 브랜치 생성 주체를 Pod로 전환한다"
```

---

### Task 5: Infra companion change와 smoke test

**Files (`SKYAHO/Autoresearch-infra`):**
- Create: `deploy/agent-orchestration/launcher-cronjob.yaml`
- Modify: `terraform/admin/autoresearch-k8s/experiment_jobs.tf`
- Modify: `terraform/admin/autoresearch-k8s/variables.tf`
- Modify: `terraform/envs/dev/gke.tf`
- Modify: `terraform/envs/dev/secret_manager.tf`
- Modify: `terraform/envs/dev/locals.tf`
- Modify: `deploy/agent-orchestration/api-deployment.yaml`
- Modify: `deploy/agent-orchestration/api-migration-job.yaml`
- Modify: `docs/runbooks/2026-08-01-auto-research-experiment-job.md`
- Modify: `docs/CHANGE_HISTORY.md`

**Interfaces:**
- Consumes: Task 4가 게시한 launcher/executor digest.
- Produces: launcher CronJob, launcher KSA/GSA/RBAC, executor KSA, 두 GitHub App secret mount.
- Deployment order: application merge·release → infra plan·merge → 사용자 승인 apply → smoke test.

- [ ] **Step 1: 사용자 승인 뒤 infra companion 이슈·branch를 만든다**

Infra `feature.yml`로 CronJob·GitHub App Secret·NetworkPolicy 변경 이슈를 만들고 이슈의
`Create a branch`를 사용한다. 원격 issue/branch 변경은 별도 사용자 확인 뒤 수행한다.

- [ ] **Step 2: launcher identity와 RBAC를 추가한다**

dev root에 launcher 전용 GSA를 만들고 `roles/cloudsql.client`, launcher KSA 한 개에 대한
`roles/iam.workloadIdentityUser`, 기존 Agent Orchestration DB password Secret 한 개의
`roles/secretmanager.secretAccessor`만 부여한다. admin root의 Job creator RoleBinding은
API KSA에서 launcher KSA로 옮기고 Jobs `create/get/list`, Pods·Events `get/list`만 준다.
executor KSA에는 Kubernetes RBAC를 부여하지 않는다.

- [ ] **Step 3: 두 GitHub App과 secret mount를 구성한다**

사용자 승인 뒤 `autoresearch-baseline-reader`(Contents read)와
`autoresearch-branch-writer`(Contents write)를 `SKYAHO/Autoresearch` 한 저장소에만 설치한다.
Kubernetes Secret 이름은 각각 `agent-orchestration-baseline-reader-app`,
`autoresearch-experiment-branch-writer-app`, key는 `private-key.pem`이다. PEM 값은
Terraform·manifest·state에 넣지 않고 runbook의 수동 주입 절차로 관리한다.

- [ ] **Step 4: admission과 network 경계를 갱신한다**

admission은 initContainer `github-token-minter` 하나, app container `branch-bootstrap`
하나, 승인 digest, branch-writer Secret의 init-only readOnly mount, `medium: Memory` 1Mi
token volume, `automountServiceAccountToken=false`, 기존 nodeSelector·toleration·deadline을
강제한다.

현재 cluster는 Calico라 GKE Dataplane V2 `FQDNNetworkPolicy`를 사용할 수 없다. Phase 1의
`app.kubernetes.io/component=branch-bootstrap` Pod만 TCP 443 public egress를 허용하고
`10/8`, `172.16/12`, `192.168/16`, `100.64/10`, `169.254/16`, `127/8`을 제외한다.

- [ ] **Step 5: 1분 CronJob을 추가한다**

```yaml
spec:
  schedule: "* * * * *"
  concurrencyPolicy: Forbid
  startingDeadlineSeconds: 60
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 3
```

CronJob은 launcher KSA, DB bootstrap memory volume, launcher digest,
`ORCH_MAX_CONCURRENT_EXPERIMENTS=2`를 사용한다. executor Job은 `backoffLimit=0`,
`activeDeadlineSeconds=300`, `ttlSecondsAfterFinished=30`을 사용한다.

- [ ] **Step 6: infra 검증을 실행한다**

Run: `terraform -chdir=terraform/envs/dev fmt -check -recursive`

Run: `scripts/terraform-env --environment dev --root terraform/envs/dev init -backend=false`

Run: `scripts/terraform-env --environment dev --root terraform/envs/dev validate`

Run: `terraform -chdir=terraform/admin/autoresearch-k8s fmt -check`

Run: `terraform -chdir=terraform/admin/autoresearch-k8s init -backend=false`

Run: `terraform -chdir=terraform/admin/autoresearch-k8s validate`

Run: `git diff --check`

Expected: 두 root fmt/validate와 whitespace 검사 PASS. apply는 사용자 승인 전 실행하지 않는다.

- [ ] **Step 7: application 전체 검증을 실행한다**

Run: `uv run python -m pytest -v`

Run: `uv run --no-sync ruff check agent_orchestration autoresearch tests tools`

Run: `git diff --check`

Expected: 전체 tests, Ruff, whitespace 검사 PASS.

- [ ] **Step 8: 사용자 승인 apply 뒤 smoke Experiment 하나를 검증한다**

```text
DB base_dev_sha == 이슈 발행 전 dev SHA
DB executor_job_name == ar-branch-<experiment UUID hex>
Experiment status == RUNNING
Job Pod == initContainer 1 + app container 1
Job status == Complete
exp branch tip == DB base_dev_sha
GitHub Actions branch workflow run 없음
Pod env/log에 token·private key 없음
```

이 Phase에서는 Job 완료 뒤 `RUNNING`을 다른 status로 자동 전이하지 않는다. 검수 결과에 이
의도적 경계를 기록하고 Job reconciler는 후속 이슈로 분리한다.

---

## Plan Self-Review Checklist

- [x] Phase 1 종료점을 `Pod branch 생성`으로 제한하고 candidate 생성·평가를 제외했다.
- [x] 기준 SHA 선커밋과 같은 SHA ref 멱등성이 Task 1·2에 연결되어 있다.
- [x] launcher는 기존 status graph를 재사용하며 새 status와 dispatch table을 만들지 않는다.
- [x] 선점과 Job 생성 사이 종료는 생성 확인 시각으로 복구하고, TTL 삭제 후 재생성은 막았다.
- [x] private key → initContainer → memory token file → executor 흐름이 Task 2·5에 있다.
- [x] live 상한 2와 설계 예시 5를 구분했다.
- [x] application과 infra의 issue/branch/apply 권한 게이트를 구분했다.
