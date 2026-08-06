# 실험 executor Phase 2 구현 계획

> **에이전트 작업 필수 절차:** 각 Stage를 구현할 때
> `superpowers:subagent-driven-development`(권장) 또는
> `superpowers:executing-plans`를 사용한다. 모든 진행 단계는 아래 체크박스로 추적한다.

**목표:** 봉인된 `base_dev_sha`에서 실험 브랜치를 만들고, 격리된 Codex가 이슈 범위의
코드를 수정하게 한 뒤, executor가 검증·commit·push하고 `candidate_sha`를 평가 단계로
인계하는 하나의 Kubernetes Job을 구현한다.

**아키텍처:** 기존 Phase 1 branch 생성 Job을 없애지 않고 `branch-creator`로 유지한 뒤,
같은 Pod의 순차 initContainer가 clone → Codex → verifier를 실행한다. 마지막 main
container만 commit·push·Candidate API 보고를 수행한다. Codex와 verifier에는 GitHub·API
자격증명을 mount하지 않고, DB 상태와 원격 Git ref를 함께 확인해 재시도를 멱등하게 만든다.

**기술 스택:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.x, Alembic, PostgreSQL,
Git CLI, GitHub App installation token, Codex CLI, uv, pytest, Ruff, Kubernetes Job.

## 전역 제약

- 구현 정본은 `docs/specs/2026-08-06-experiment-executor-phase2.md`다.
- 지능형 코드 판단·수정은 Codex가, branch·clone·검증·commit·push·상태 전이는 executor
  코드가 담당한다.
- Codex에는 GitHub App private key, installation token, Executor API token, Kubernetes
  ServiceAccount token, GCP 자격증명을 전달하지 않는다.
- 수정 허용 기본 경로는 `src/**`(`src/features/model_contract.py` 제외),
  `autoresearch/**`, `tests/**`, `tools/**`다.
- `prod_model_contract`는 `src/features/model_contract.py`, `feast_definition`은
  `feature_repo/**`만 추가 허용한다. `promotion`은 수정 범위를 넓히지 않는다.
- `.git/**`, `.github/**`, `.claude/**`, `docs/**`, `deploy/**`, `proxy/**`,
  `agent_orchestration/**`, `.env*`, `pyproject.toml`, `uv.lock`, symlink, submodule,
  Git LFS pointer 변경은 항상 거부한다.
- candidate당 변경 경로 50개, textual diff 1 MiB, 일반 파일 하나 10 MiB를 상한으로 둔다.
- 최종 검증은 `git diff --check`, `uv run --no-sync ruff check agent_orchestration
  autoresearch tests tools`, `uv run --no-sync python -m pytest` 순서다.
- `UV_PROJECT_ENVIRONMENT=/opt/autoresearch-venv`는 executor image가 소유하는 고정값이다.
  candidate가 의존성을 설치하거나 verifier 환경을 바꾸지 못한다.
- 성공 candidate는 parent가 `base_dev_sha`인 정확히 한 commit이며, force-push·ref 삭제·
  다른 branch push는 구현 경로에 두지 않는다.
- 코드 변경은 `실패 테스트 작성 → 실패 확인 → 최소 구현 → 통과 확인 → 커밋` 순서로 한다.
- 기능을 추가·변경하는 기존 Python 모듈과 모든 새 모듈은 전체 파이프라인 담당 구간·기능·
  비책임을 모듈 최상단 docstring에 함께 기록한다.
- 각 Stage가 끝날 때 해당 좁은 테스트와 누적 executor/API 테스트를 실행한다. Stage 7에서
  전체 pytest, Ruff, Docker build를 실행한다.

---

## 파일 책임 지도

### 생성

- `agent_orchestration/migrations/versions/0005_experiment_candidate_sha.py` —
  `candidate_sha` nullable 컬럼과 형식 CHECK의 upgrade/downgrade.
- `agent_orchestration/app/experiments/executor_router.py` — 일반 사용자 API와 분리된 Executor
  Candidate HTTP endpoint.
- `agent_orchestration/executor/branch_creator.py` — 기존 Phase 1 branch 생성·멱등 검증 CLI.
- `agent_orchestration/executor/github_issues.py` — GitHub issue read-only 조회 adapter.
- `agent_orchestration/executor/workspace.py` — 이슈/marker/hash 검증과 clone·checkout.
- `agent_orchestration/executor/state.py` — container 사이에 전달할 봉인 workspace state JSON.
- `agent_orchestration/executor/prompt.py` — 검증된 이슈와 허용 경계를 Codex prompt로 변환.
- `agent_orchestration/executor/codex_worker.py` — 비대화식 Codex subprocess와 환경 격리.
- `agent_orchestration/executor/verifier.py` — diff 정책과 고정 Ruff/pytest 실행.
- `agent_orchestration/executor/finalizer.py` — candidate 상태 분류, commit·push·원격 검증.
- `agent_orchestration/executor/api_client.py` — Executor Candidate API 멱등 보고.
- `tests/test_experiment_candidate_migration.py` — migration 대칭성과 CHECK 계약.
- `tests/test_experiment_candidate_api.py` — Candidate API 인증·원자성·멱등성.
- `tests/test_experiment_workspace.py` — 이슈 검증·clone·checkout 계약.
- `tests/test_experiment_codex_worker.py` — prompt·환경·timeout·로그 비노출.
- `tests/test_experiment_candidate_verifier.py` — 경로·파일 형식·크기·고정 명령.
- `tests/test_experiment_candidate_finalizer.py` — commit·push·복구 상태표.
- `tests/test_experiment_executor_integration.py` — fake Git/API/Codex end-to-end.

### 수정

- `agent_orchestration/app/config.py` — `ORCH_EXECUTOR_API_TOKEN` 로딩과 토큰 분리 검증.
- `agent_orchestration/app/main.py` — executor router와 전용 constant-time 인증 연결.
- `agent_orchestration/app/experiments/exceptions.py` — candidate 좌표·SHA 충돌 오류.
- `agent_orchestration/app/experiments/models.py` — `Experiment.candidate_sha` ORM 계약.
- `agent_orchestration/app/experiments/schemas.py` — Candidate 요청/응답과 공개 응답 필드.
- `agent_orchestration/app/experiments/service.py` — candidate 저장과 평가 중 (`EVALUATING`) 전이.
- `agent_orchestration/executor/config.py` — Stage별 봉인 입력·경로·timeout 파싱.
- `agent_orchestration/executor/token_minter.py` — branch/clone/push 목적별 최소 권한 token.
- `agent_orchestration/executor/__init__.py` — Phase 2 전체 책임 docstring.
- `agent_orchestration/launcher/config.py` — Codex/API Secret 참조와 Job 실행 한도.
- `agent_orchestration/launcher/repository.py` — `issue_body_sha256`을 claim에 봉인.
- `agent_orchestration/launcher/jobs.py` — 8-container Job, volume과 Secret mount 경계.
- `agent_orchestration/launcher/main.py` — terminal Failed Job 회수.
- `deploy/agent_orchestration/executor.Dockerfile` — Git·uv·Node·Codex·전체 dev 환경.
- `.github/workflows/release.yml` — Phase 2 executor image import/binary/digest 검증.
- `tests/test_experiment_executor.py` — 기존 branch 생성 회귀와 새 명칭.
- `tests/test_experiment_launcher.py` — claim·manifest·terminal 회수.
- `tests/test_agent_orchestration.py`, `tests/test_experiment_router.py`,
  `tests/test_experiment_step_router.py`, `tests/test_experiment_issue_endpoint.py`,
  `tests/test_agent_orchestration_runner.py` — 새 필수 API token 설정 반영.
- `tests/test_agent_orchestration_container.py` — executor image 도구·버전·비루트 계약.
- `README.md`, `.claude/docs/agent-project-reference.md`, `docs/README.md` — 실행·배포·환경 계약.

---

## Task 1 / Stage 1: Candidate 저장 계약과 Executor 전용 API

**산출물:** 원격에서 확인된 SHA를 DB에 한 번만 기록하고 실행 중 (`RUNNING`)에서 평가 중
(`EVALUATING`)으로 원자 전이하는 인증된 내부 API.

**인터페이스:**

```python
class CandidateReportRequest(BaseModel):
    idempotency_key: str
    issue_number: int
    issue_branch: str
    base_dev_sha: str
    candidate_sha: str

def record_candidate(
    session: Session,
    experiment_id: uuid.UUID,
    request: CandidateReportRequest,
) -> Experiment: ...
```

Endpoint는 `POST /internal/executor/experiments/{experiment_id}/candidate`, 인증 헤더는
`X-Orch-Executor-Token`으로 고정한다. `ORCH_EXECUTOR_API_TOKEN`은 32자 이상이며
`ORCH_API_TOKEN`·`ORCH_RUNNER_TOKEN`과 달라야 한다.

- [ ] **1.1 실패 migration 테스트 작성**

  `tests/test_experiment_candidate_migration.py`에서 revision
  `0005_experiment_candidate_sha`가 `candidate_sha VARCHAR(40) NULL`과
  `ck_experiment_candidate_sha_format`을 추가하고 downgrade에서 제약→컬럼 역순으로
  제거하는지 단언한다. ORM `CheckConstraint`는 PostgreSQL에서만 DDL이 나오도록
  `.ddl_if(dialect="postgresql")`를 적용해 SQLite 단위 테스트와 실제 DB 방언을 구분한다.

- [ ] **1.2 migration 테스트 실패 확인**

  실행:

  ```bash
  uv run python -m pytest tests/test_experiment_candidate_migration.py -v
  ```

  기대: revision 파일 부재로 `FAIL`.

- [ ] **1.3 migration과 ORM 최소 구현**

  PostgreSQL CHECK는 다음 의미로 고정한다.

  ```sql
  candidate_sha IS NULL OR candidate_sha ~ '^[0-9a-f]{40}$'
  ```

  `Experiment`와 `ExperimentResponse`에 `candidate_sha: str | None`을 추가하고 모델
  docstring의 lineage 설명을 갱신한다.

- [ ] **1.4 Candidate service 실패 테스트 작성**

  `tests/test_experiment_candidate_api.py`에 다음 사례를 구체적으로 작성한다.

  - `RUNNING` + 모든 봉인 좌표 일치 → SHA 저장, `EVALUATING` event 한 건.
  - 같은 key·같은 fingerprint → 같은 Experiment 반환, event 추가 없음.
  - 같은 key·다른 payload → `IdempotencyConflictError`.
  - `candidate_sha`가 이미 다른 값 → `CandidateConflictError`.
  - issue/base/branch 불일치 → `CandidateConflictError`.
  - event flush 실패 → SHA와 상태 모두 rollback.

- [ ] **1.5 service 실패 확인**

  ```bash
  uv run python -m pytest tests/test_experiment_candidate_api.py -k service -v
  ```

  기대: `CandidateReportRequest` 또는 `record_candidate` import 실패.

- [ ] **1.6 원자 service 구현**

  `find_experiment(session, experiment_id, for_update=True)`로 row lock을 잡고 다음
  fingerprint를 사용한다.

  ```python
  {
      "issue_number": request.issue_number,
      "issue_branch": request.issue_branch,
      "base_dev_sha": request.base_dev_sha,
      "candidate_sha": request.candidate_sha,
  }
  ```

  idempotency key는 정확히 `executor-candidate:{experiment_id}`여야 한다. 최초 성공은
  `validate_transition(ExperimentStatus(experiment.status),
  ExperimentStatus.EVALUATING)`을 호출한 뒤 같은 `with session.begin()` 안에서
  `candidate_sha`, `status=EVALUATING`, 위 fingerprint의 `ExperimentEvent`를 저장한다.
  기존 event 조회는 상태 검증보다 먼저 수행해 같은 요청 재시도가 이미 평가 중인 행에서도
  멱등 성공하게 한다.

- [ ] **1.7 전용 인증·router 실패 테스트 작성**

  다음 HTTP 사례를 고정한다.

  ```python
  response = client.post(
      f"/internal/executor/experiments/{experiment.id}/candidate",
      headers={"X-Orch-Executor-Token": executor_token},
      json=payload,
  )
  assert response.status_code == 200
  ```

  헤더 없음·일반 `X-Orch-Token`·틀린 executor token은 `401`, 좌표 충돌은 `409`여야 한다.
  uppercase·39/41자리 SHA, branch와 다른 이슈 번호, extra field는 request validation
  `422`로 거부한다.

- [ ] **1.8 설정·router 구현 및 테스트 통과**

  ```bash
  uv run python -m pytest \
    tests/test_experiment_candidate_migration.py \
    tests/test_experiment_candidate_api.py \
    tests/test_experiment_router.py \
    tests/test_agent_orchestration.py -v
  ```

- [ ] **1.9 Stage 1 커밋**

  ```bash
  git add agent_orchestration/app agent_orchestration/migrations/versions \
    tests/test_experiment_candidate_migration.py \
    tests/test_experiment_candidate_api.py tests/test_experiment_router.py \
    tests/test_agent_orchestration.py tests/test_experiment_step_router.py \
    tests/test_experiment_issue_endpoint.py tests/test_agent_orchestration_runner.py
  git commit -m "feat: candidate SHA 저장 계약을 추가한다"
  ```

---

## Task 2 / Stage 2: Branch 생성과 검증된 Workspace 준비

**산출물:** 봉인된 SHA에서 branch를 생성하고, GitHub 이슈와 DB body hash를 대조한 뒤
token 흔적 없이 정확한 branch를 clone한 workspace.

**인터페이스:**

```python
@dataclass(frozen=True)
class BranchCreatorInput:
    experiment_id: uuid.UUID
    issue_number: int
    issue_branch: str
    base_dev_sha: str
    github_repository: str
    token_file: Path

@dataclass(frozen=True)
class WorkspacePrepareInput:
    experiment_id: uuid.UUID
    issue_number: int
    issue_branch: str
    base_dev_sha: str
    issue_body_sha256: str
    github_repository: str
    token_file: Path
    workspace: Path

@dataclass(frozen=True)
class PreparedWorkspace:
    repository: Path
    issue_body: str
    allowed_scope: tuple[str, ...]
    remote_tip: str

@dataclass(frozen=True)
class BranchCreatorResult:
    created: bool
    remote_tip: str

@dataclass(frozen=True)
class GitHubIssueSnapshot:
    title: str
    body: str

class IssueClient(Protocol):
    async def get(
        self,
        repository: str,
        issue_number: int,
        token: str,
    ) -> GitHubIssueSnapshot: ...

@dataclass(frozen=True)
class ExecutorWorkspaceState:
    schema_version: Literal[1]
    repository: str
    issue_body: str
    allowed_scope: tuple[str, ...]
    base_dev_sha: str
    remote_tip: str

async def prepare_workspace(
    config: WorkspacePrepareInput,
    issues: IssueClient,
) -> PreparedWorkspace: ...
```

- [ ] **2.1 기존 branch 생성 명칭 회귀 테스트 작성**

  `tests/test_experiment_executor.py` import를 `executor.branch_creator`로 바꾸고
  `ensure_issue_branch()`가 branch 부재 시 `base_dev_sha`에 생성하고, 같은 SHA와 다른
  SHA의 기존 branch는 모두 변경하지 않는지 먼저 실패시킨다. 다른 SHA는 첫 Pod가 push한
  candidate일 수 있으므로 이 단계에서 충돌로 확정하지 않고 `remote_tip`으로 반환한다.

- [ ] **2.2 `branch_creator.py`로 구조 변경**

  기존 `executor/main.py` 책임을 옮기고 공개 이름을 다음으로 고정한다.

  ```python
  async def ensure_issue_branch(
      coordinates: BranchCreatorInput,
      refs: RefClient,
      token: str,
  ) -> BranchCreatorResult: ...
  ```

  ref가 다른 SHA일 때 update·delete·force-push하지 않는 Phase 1 불변식은 유지하되,
  Phase 2 재시도를 위해 즉시 실패 대신 후속 candidate 검증으로 넘긴다.

- [ ] **2.3 이슈·workspace 실패 테스트 작성**

  `tests/test_experiment_workspace.py`에 다음 사례를 작성한다.

  - GitHub body marker가 Experiment UUID와 일치.
  - UTF-8 body SHA-256이 봉인 hash와 일치.
  - `parse_issue_input()`이 계산한 branch가 봉인 branch와 일치.
  - marker/hash/branch 하나라도 다르면 clone subprocess 호출 없음.
  - clone 후 `HEAD`와 `origin/<issue_branch>`가 같은 원격 tip.
  - tip이 `base_dev_sha`일 때만 후속 workflow가 Codex 실행을 허용.
  - 다른 tip이면 Codex를 건너뛰고 Stage 5 `ADOPTABLE` 검증으로 전달.
  - remote URL과 credential helper에 token이 없음.

  workspace-preparer는 성공 결과를 mode `0400`의
  `/var/run/executor-state/state.json`에 canonical JSON으로 기록한다. `remote_tip ==
  base_dev_sha`면 Codex 실행 경로이고, 다르면 Codex skip·기존 candidate 검증 경로다.
  state는 repository workspace와 다른 `emptyDir` volume을 사용한다.

- [ ] **2.4 실패 확인**

  ```bash
  uv run python -m pytest \
    tests/test_experiment_executor.py \
    tests/test_experiment_workspace.py -v
  ```

  기대: workspace 모듈 부재 또는 새 interface import 실패.

- [ ] **2.5 GitHub issue adapter와 입력 검증 구현**

  `GitHubIssues.get()`은 `GET /repos/{repository}/issues/{issue_number}`만 호출하며 제목과
  본문을 반환한다. body는 `tools.auto_research_issue_branch.parse_issue_input()`으로
  fail-closed 파싱하여 `allowed_scope`를 얻는다. GitHub response body와 token은 로그에
  남기지 않는다.

- [ ] **2.6 clone·checkout 구현**

  subprocess는 argv list와 임시 `GIT_ASKPASS`를 사용한다.

  ```text
  git clone --no-checkout --origin origin <clean-url> <workspace>/repository
  git -C <repository> checkout --detach origin/<issue_branch>
  git -C <repository> switch -c <issue_branch>
  ```

  clone 직후 clean URL, `credential.helper` 부재, `core.hooksPath` 안전값, remote tip을
  확인한다. `GIT_ASKPASS` 파일은 `finally`에서 삭제하고 token 문자열은 예외에 포함하지
  않는다.

  `state.py`는 `ExecutorWorkspaceState`의 schema version, 절대 repository 경로가 고정
  workspace 아래인지, SHA·scope 형식을 읽을 때마다 다시 검증한다.

- [ ] **2.7 Workspace 테스트 통과**

  ```bash
  uv run python -m pytest \
    tests/test_experiment_executor.py \
    tests/test_experiment_workspace.py \
    tests/test_auto_research_issue_branch.py -v
  ```

- [ ] **2.8 Stage 2 커밋**

  ```bash
  git add agent_orchestration/executor \
    tests/test_experiment_executor.py tests/test_experiment_workspace.py
  git commit -m "feat: 봉인 브랜치 workspace 준비 경계를 추가한다"
  ```

---

## Task 3 / Stage 3: 격리된 Codex 코드 수정 실행

**산출물:** 검증된 이슈만 입력받고 workspace 파일만 수정할 수 있는 noninteractive Codex
worker.

**인터페이스:**

```python
@dataclass(frozen=True)
class CodexRunInput:
    repository: Path
    issue_body: str
    allowed_scope: tuple[str, ...]
    codex_home: Path
    timeout_seconds: int

@dataclass(frozen=True)
class CodexRunResult:
    exit_code: int
    duration_ms: int

def build_codex_prompt(run: CodexRunInput) -> str: ...
def run_codex(run: CodexRunInput) -> CodexRunResult: ...
```

- [ ] **3.1 prompt 실패 테스트 작성**

  `tests/test_experiment_codex_worker.py`에서 prompt가 검증된 body, 기본 허용 경로,
  선택된 조건부 경로, 고정 검증 명령을 포함하고 token/Secret 경로/API URL을 포함하지
  않는지 단언한다.

- [ ] **3.2 환경·timeout 실패 테스트 작성**

  fake `codex` executable로 다음을 검증한다.

  - argv가 `codex exec --sandbox workspace-write -C <repository> <prompt>`.
  - 환경은 `CODEX_HOME`, 임시 `HOME/XDG_CONFIG_HOME/XDG_CACHE_HOME/TMPDIR`, `PATH`,
    locale, 고정 `UV_PROJECT_ENVIRONMENT`만 존재.
  - `GITHUB_TOKEN`, `ORCH_*TOKEN*`, `KUBERNETES_SERVICE_HOST`, `GOOGLE_*` 부재.
  - timeout 시 process group과 child가 종료됨.
  - stdout/stderr의 sentinel secret이 caplog에 없음.
  - state의 `remote_tip != base_dev_sha`면 Codex executable 호출 없이 성공 종료.

- [ ] **3.3 실패 확인**

  ```bash
  uv run python -m pytest tests/test_experiment_codex_worker.py -v
  ```

- [ ] **3.4 prompt와 worker 최소 구현**

  `subprocess.Popen(argv, cwd=run.repository, env=filtered_environment,
  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False,
  start_new_session=True)`을 사용한다. stdout/stderr pipe는 제한된 ring buffer로 읽은 뒤
  영속화하지 않는다. exit code·duration·정제된 실패 분류만 반환한다. timeout이면
  process group에 `SIGTERM`, 5초 뒤에도 남으면 `SIGKILL`한다.

- [ ] **3.5 Codex worker 테스트 통과**

  ```bash
  uv run python -m pytest tests/test_experiment_codex_worker.py -v
  ```

- [ ] **3.6 Stage 3 커밋**

  ```bash
  git add agent_orchestration/executor/prompt.py \
    agent_orchestration/executor/codex_worker.py \
    tests/test_experiment_codex_worker.py
  git commit -m "feat: 격리된 Codex 코드 수정 실행기를 추가한다"
  ```

---

## Task 4 / Stage 4: Candidate 변경 범위와 고정 검증

**산출물:** Codex 응답이 아니라 실제 Git diff와 봉인된 명령으로 candidate를 승인·거부하는
credential-free verifier.

**인터페이스:**

```python
@dataclass(frozen=True)
class CandidatePolicy:
    allowed_scope: tuple[str, ...]
    max_changed_paths: int = 50
    max_text_diff_bytes: int = 1024 * 1024
    max_regular_file_bytes: int = 10 * 1024 * 1024

@dataclass(frozen=True)
class VerificationResult:
    changed_paths: tuple[str, ...]
    content_fingerprint: str
    verified_tree_oid: str

def verify_candidate(
    repository: Path,
    base_sha: str,
    candidate_sha: str | None,
    policy: CandidatePolicy,
) -> VerificationResult: ...
```

`candidate_sha=None`은 Codex가 만든 working tree diff, SHA가 있으면 복구 대상인
`base_sha..candidate_sha` committed diff를 검증한다.

반환값은 다음 handoff 계약을 가진다.

- 변경 경로 (`changed_paths`): 정책을 통과한 정렬된 경로 목록이다.
- 콘텐츠 지문 (`content_fingerprint`): domain separator, `base_sha`, 정렬된 change
  kind·이전 경로·현재 경로, 각 경로의 missing/regular 상태·mode·size·bytes를 canonical
  SHA-256으로 계산한 64자리 lowercase 값이다.
- 검증 tree 객체 ID (`verified_tree_oid`): verifier snapshot에 finalizer와 동일하게
  `git add --all` 후 `git write-tree`를 실행해 얻은 tree OID다. committed candidate는
  검증한 commit tree와 같아야 한다.

- [ ] **4.1 정책 실패 테스트 작성**

  `tests/test_experiment_candidate_verifier.py`에서 허용/금지 경로, 50/51개 경계,
  1 MiB/초과 diff, 10 MiB/초과 파일, symlink mode `120000`, submodule mode `160000`,
  `version https://git-lfs.github.com/spec/v1` pointer를 각각 검증한다.

- [ ] **4.2 고정 명령 실패 테스트 작성**

  fake command runner로 정확한 순서와 `UV_PROJECT_ENVIRONMENT=/opt/autoresearch-venv`,
  자격증명 환경 부재, 첫 실패 즉시 중단을 단언한다.

- [ ] **4.3 실패 확인**

  ```bash
  uv run python -m pytest tests/test_experiment_candidate_verifier.py -v
  ```

- [ ] **4.4 verifier 최소 구현**

  `git status --porcelain=v1 -z`, `git diff --name-status -z`, `git ls-files -s`, `lstat()`을
  조합한다. rename의 이전·새 경로를 모두 정책 검사하며 untracked 파일도 포함한다.
  변경이 0개면 `CandidateVerificationError("no_changes")`를 발생시킨다.

- [ ] **4.5 verifier 테스트 통과**

  ```bash
  uv run python -m pytest \
    tests/test_experiment_candidate_verifier.py \
    tests/test_experiment_codex_worker.py -v
  ```

- [ ] **4.6 Stage 4 커밋**

  ```bash
  git add agent_orchestration/executor/verifier.py \
    tests/test_experiment_candidate_verifier.py
  git commit -m "feat: candidate 변경 범위와 검증 계약을 추가한다"
  ```

---

## Task 5 / Stage 5: Commit·Push·Candidate 보고와 재시도

**산출물:** 유효한 working tree를 정확히 한 commit으로 만들고 원격 tip과 DB candidate를
같은 SHA로 수렴시키는 finalizer.

**인터페이스:**

```python
class CandidateState(str, Enum):
    NEW = "NEW"
    ADOPTABLE = "ADOPTABLE"

@dataclass(frozen=True)
class FinalizeInput:
    experiment_id: uuid.UUID
    issue_number: int
    issue_branch: str
    base_dev_sha: str
    repository: Path
    github_repository: str
    push_token_file: Path
    api_url: str
    api_token_file: Path

def classify_candidate_state(
    repository: Path,
    *,
    base_dev_sha: str,
    issue_number: int,
    remote_tip: str,
) -> CandidateState: ...
def finalize_candidate(
    config: FinalizeInput,
    verification: VerificationResult,
) -> str: ...
```

- [ ] **5.1 상태표 실패 테스트 작성**

  `tests/test_experiment_candidate_finalizer.py`에서 원격 상태를 다음 표로 parameterize한다.

  | 원격 tip | 기대 |
  | --- | --- |
  | `base_dev_sha` | `NEW` |
  | base의 단일 executor commit | `ADOPTABLE` |
  | 위 조건 외 SHA | 충돌 |

  채택 가능한 commit은 parent, author/committer identity, message, changed tree를 모두
  재검증해야 한다. DB candidate가 같은지·없는지·다른지는 Stage 1 Candidate API가 row
  lock 안에서 판정한다. 같은 SHA 보고는 멱등 성공하고 다른 SHA는 `409`다.

- [ ] **5.2 commit·push 실패 테스트 작성**

  임시 bare repository를 사용해 parent, 정확히 한 commit, 고정 identity,
  `exp: issue #<number> candidate`, branch refspec, fast-forward만 허용되는지 검증한다.
  push 직전 tip 변경을 주입하면 원격 ref가 바뀌지 않아야 한다.
  `NEW` (신규 candidate)에서는 commit 직전에 원본 working tree의 콘텐츠 지문
  (`content_fingerprint`)과 `git add --all` 뒤 tree 객체 ID (`verified_tree_oid`)를
  재계산해 Stage 4 반환값과 둘 다 같을 때만 commit한다. `ADOPTABLE` (기존 candidate
  채택 가능)에서는 검증 commit의 tree 객체 ID가 `verified_tree_oid`와 같은지 확인한다.

- [ ] **5.3 API client 실패 테스트 작성**

  fake HTTP server로 header, exact payload, timeout, 409, 응답 SHA 불일치를 검증한다.
  token/API response body는 예외와 로그에서 제거한다.

- [ ] **5.4 실패 확인**

  ```bash
  uv run python -m pytest tests/test_experiment_candidate_finalizer.py -v
  ```

- [ ] **5.5 finalizer와 API client 구현**

  새 candidate는 `git add --all` 후 `user.name=Autoresearch Experiment Executor`,
  `user.email=experiment-executor@autoresearch.invalid`를 해당 commit 명령에만 적용한다.
  clean remote URL과 `GIT_ASKPASS`로 정확히
  `HEAD:refs/heads/<issue_branch>`를 push하고 `git ls-remote`로 결과 SHA를 확인한 뒤 API를
  호출한다. commit 직전 `HEAD == base_dev_sha`, credential helper 부재와 remote URL을
  재확인하고 `core.hooksPath=/dev/null`을 해당 Git 명령에만 적용한다. API가 실패하면
  nonzero로 종료하여 다음 Pod가 기존 remote candidate를
  `ADOPTABLE`로 채택하게 한다.

- [ ] **5.6 Stage 5 누적 테스트 통과**

  ```bash
  uv run python -m pytest \
    tests/test_experiment_candidate_api.py \
    tests/test_experiment_candidate_verifier.py \
    tests/test_experiment_candidate_finalizer.py -v
  ```

- [ ] **5.7 Stage 5 커밋**

  ```bash
  git add agent_orchestration/executor/finalizer.py \
    agent_orchestration/executor/api_client.py \
    tests/test_experiment_candidate_finalizer.py
  git commit -m "feat: candidate commit과 멱등 push를 추가한다"
  ```

---

## Task 6 / Stage 6: 8-container Job 조립과 실패 회수

**산출물:** 앞 Stage를 정확한 권한·순서로 실행하고 한 번의 Pod 재시도 후 최종 실패를
오류 (`ERROR`)로 회수하는 launcher.

**최종 container 순서:**

```text
branch-token-minter
→ branch-creator
→ clone-token-minter
→ workspace-preparer
→ codex-worker
→ candidate-verifier
→ push-token-minter
→ candidate-finalizer (main)
```

`LauncherSettings`에는 `executor_api_url`, `executor_api_token_secret_name`,
`codex_home_pvc_name`, `workspace_size_limit`, `codex_timeout_sec`를 추가한다. 각각
`ORCH_EXECUTOR_API_URL`, `ORCH_EXECUTOR_API_TOKEN_SECRET_NAME`,
`ORCH_CODEX_HOME_PVC_NAME`, `ORCH_EXECUTOR_WORKSPACE_SIZE_LIMIT`,
`ORCH_CODEX_TIMEOUT_SEC`에서 읽으며 실제 값과 리소스 생성은 Autoresearch-infra가 소유한다.

- [ ] **6.1 claim 입력 실패 테스트 작성**

  `ClaimedExperiment`에 `issue_body_sha256`을 추가한다. launcher가 DB `issue_body` UTF-8
  SHA-256을 계산하고 body가 없으면 claim하지 않는지 테스트한다. body 원문은 Job env에
  넣지 않는다.

- [ ] **6.2 manifest 실패 테스트 작성**

  `tests/test_experiment_launcher.py`에서 다음을 단언한다.

  - initContainer 7개와 main 1개의 이름·순서.
  - private key는 세 token-minter만 mount.
  - branch/clone/push token 파일이 서로 다른 memory volume 경로.
  - workspace는 workspace-preparer부터 finalizer까지 필요한 mode로 mount.
  - executor-state volume은 workspace-preparer만 write, codex-worker·verifier·finalizer는
    read-only mount.
  - codex-worker와 candidate-verifier는 workspace root를 쓰거나 읽되 동적으로 생성된
    `repository/.git` subPath를 같은 경로에 read-only로 겹쳐 mount.
  - `CODEX_HOME`은 codex-worker만 mount.
  - Executor API token은 finalizer만 mount.
  - `automount_service_account_token=False`, non-root, seccomp, capability drop.
  - `backoff_limit=1`; 첫 Pod의 push 후 API 실패를 두 번째 Pod가 채택 가능.
  - Job 이름은 `ar-exec-<experiment UUID hex>`, label selector는
    `app.kubernetes.io/component=experiment-executor`로 Phase 1 Job과 구분.

- [ ] **6.3 workflow 조립 실패 테스트 작성**

  fake issue/Git/Codex/verifier/API로 다음 두 경로를 검증한다.

  ```text
  base tip → Codex → verifier → new commit → push → report
  existing valid candidate → Codex skip → verifier → report only
  ```

- [ ] **6.4 terminal 회수 실패 테스트 작성**

  `KubernetesJobs.list_terminal()`과 launcher reconciler가 최종 Failed Job만 찾아
  `RUNNING → ERROR` event를 한 번 기록하는지 검증한다. Complete Job은 Candidate API가
  성공한 뒤에만 main이 exit 0이라는 불변식을 두므로 별도 성공-누락 상태를 만들지 않는다.
  기존 `ar-branch-*` 또는 `component=branch-bootstrap` Phase 1 Job과 이미 실행 중
  (`RUNNING`)인 #554 계열 행은 Phase 2 회수·재실행 대상이 아님을 함께 고정한다.

- [ ] **6.5 실패 확인**

  ```bash
  uv run python -m pytest \
    tests/test_experiment_launcher.py \
    tests/test_experiment_executor_integration.py -v
  ```

- [ ] **6.6 token purpose와 Job 구현**

  `TokenPurpose`는 다음 권한만 허용한다.

  ```python
  TOKEN_PERMISSIONS = {
      "branch": {"contents": "write"},
      "clone": {"contents": "read", "issues": "read"},
      "push": {"contents": "write"},
  }
  ```

  각 entrypoint는 read-only `ExecutorWorkspaceState`의 remote tip을 사용한다. codex-worker는
  base tip에서만 실행하고, verifier는 base tip이면 working tree diff, 다른 tip이면
  `base_dev_sha..remote_tip` committed diff를 검사하며, finalizer는 같은 remote tip을 다시
  조회해 바뀌지 않았을 때만 commit 또는 보고를 수행한다.

- [ ] **6.7 Job·통합 테스트 통과**

  ```bash
  uv run python -m pytest \
    tests/test_experiment_executor.py \
    tests/test_experiment_workspace.py \
    tests/test_experiment_codex_worker.py \
    tests/test_experiment_candidate_verifier.py \
    tests/test_experiment_candidate_finalizer.py \
    tests/test_experiment_launcher.py \
    tests/test_experiment_executor_integration.py -v
  ```

- [ ] **6.8 Stage 6 커밋**

  ```bash
  git add agent_orchestration/executor agent_orchestration/launcher \
    tests/test_experiment_executor.py tests/test_experiment_launcher.py \
    tests/test_experiment_executor_integration.py
  git commit -m "feat: Phase 2 executor Job과 실패 회수를 연결한다"
  ```

---

## Task 7 / Stage 7: Executor image·문서·Infra handoff·운영 검증

**산출물:** immutable image와 명시적인 Infra 입력 계약, 전체 CI 증거, 새 Experiment smoke
체크리스트.

- [x] **7.1 image 계약 실패 테스트 작성**

  `tests/test_agent_orchestration_container.py`에서 executor image에 다음이 있는지 검사한다.

  - Git CLI와 uv.
  - `/opt/autoresearch-venv`에 lock 기반 기본+dev 의존성.
  - Node.js와 고정 버전 `@openai/codex@0.146.0`.
  - `UV_PROJECT_ENVIRONMENT=/opt/autoresearch-venv`.
  - 새 executor module import와 `git --version`, `uv --version`, `node --version`,
    `codex --version` release 검증.
  - image에 봉인된 `tools/__init__.py`와 `tools/auto_research_issue_branch.py`; runtime clone의
    동명 파일보다 이 image copy를 issue parser로 사용.
  - UID/GID 10001, repository 소스·`.env`·`auth.json` 미포함.

- [x] **7.2 image 테스트 실패 확인**

  ```bash
  uv run python -m pytest tests/test_agent_orchestration_container.py -v
  ```

- [x] **7.3 Dockerfile와 release workflow 구현**

  lock export는 project 기본 의존성과 `dev` group을 포함하고 feast group은 제외한다.
  Codex 인증은 build context에서 COPY하지 않고 runtime `CODEX_HOME` mount만 사용한다.

- [x] **7.4 README·정본 문서 갱신**

  `README.md`와 `.claude/docs/agent-project-reference.md`에 producer → exact handoff value →
  consumer → validation → permissions 순서로 다음을 기록한다.

  - 8-container 순서와 container별 Secret/volume.
  - `ORCH_EXECUTOR_API_TOKEN`, issue body hash, token file 경로.
  - executor image의 Git/uv/Node/Codex/venv 계약.
  - candidate 저장과 `RUNNING → EVALUATING` 인계.
  - Infra가 소유하는 실제 Secret/PVC/resource/NetworkPolicy 이름과 값은 이 저장소에서
    단정하지 않는다.

- [x] **7.5 Infra companion handoff 작성**

  Autoresearch PR 본문에 아래 companion 범위를 그대로 옮긴다. 실제 Infra 이슈·브랜치·PR
  생성은 Autoresearch 코드 PR 승인 후 별도 작업으로 수행한다. 이 목록은 실제 Infra
  이름·값을 선언하지 않는 PR 본문용 draft다.

  ```text
  - GitHub App private key: branch/clone/push token-minter만 mount
  - Codex auth CODEX_HOME: codex-worker만 mount
  - Executor API token: candidate-finalizer만 mount
  - workspace/token emptyDir size limit
  - OpenAI/GitHub/internal API 최소 egress
  - immutable launcher/executor/API digest
  - non-root/seccomp/capability drop/automountServiceAccountToken=false
  ```

- [x] **7.6 좁은 검증 실행**

  ```bash
  uv run python -m pytest \
    tests/test_agent_orchestration_container.py \
    tests/test_experiment_candidate_api.py \
    tests/test_experiment_executor.py \
    tests/test_experiment_workspace.py \
    tests/test_experiment_codex_worker.py \
    tests/test_experiment_candidate_verifier.py \
    tests/test_experiment_candidate_finalizer.py \
    tests/test_experiment_launcher.py \
    tests/test_experiment_executor_integration.py -v
  ```

- [x] **7.7 전체 검증 실행**

  ```bash
  uv run python -m pytest -v
  uv run --no-sync ruff check agent_orchestration autoresearch tests tools
  git diff --check
  docker build -f deploy/agent_orchestration/executor.Dockerfile \
    -t autoresearch-executor-phase2:test .
  ```

- [ ] **7.8 운영 smoke는 Infra 반영 승인 후 실행**

  새 Experiment 하나에 대해 관측값만 기록한다. Infra 반영 전에는 시작하지 않는다.

  ```text
  DB base_dev_sha == candidate commit parent
  DB issue_branch == GitHub branch
  DB candidate_sha == GitHub remote tip
  Experiment status == 평가 중 (EVALUATING)
  base_dev_sha..candidate_sha commit count == 1
  main/dev/다른 exp ref 변화 == 없음
  Codex/verifier mount·환경·로그의 GitHub/API credential == 없음
  ```

- [x] **7.9 Stage 7 커밋**

  ```bash
  git add deploy/agent_orchestration/executor.Dockerfile \
    .github/workflows/release.yml tests/test_agent_orchestration_container.py \
    README.md .claude/docs/agent-project-reference.md docs/README.md \
    docs/plans/2026-08-06-experiment-executor-phase2.md
  git commit -m "docs: Phase 2 executor 배포와 검증 계약을 정리한다"
  ```

---

## Stage 완료 게이트

| Stage | 완료 판단 | 다음 Stage가 소비하는 값 |
| --- | --- | --- |
| 1 | candidate API 원자성·인증 테스트 통과 | `candidate_sha`, 전용 endpoint/token |
| 2 | branch/issue/hash/clone 테스트 통과 | `PreparedWorkspace` |
| 3 | credential 없는 Codex 실행 테스트 통과 | 수정된 working tree |
| 4 | diff 정책·Ruff·pytest verifier 통과 | `VerificationResult` |
| 5 | bare Git push·채택·API 보고 테스트 통과 | 원격 candidate SHA |
| 6 | 8-container manifest·재시도·Failed 회수 통과 | 배포 가능한 Job 계약 |
| 7 | 전체 CI·Docker build 통과 | Infra digest handoff와 smoke 준비 상태 |

Stage 1~7은 순차 의존한다. 같은 Stage 안의 테스트 파일 작성은 병렬로 준비할 수 있지만,
공유 구현 파일을 동시에 수정하지 않는다. Stage 7의 운영 smoke는 코드 완료가 아니라 Infra
반영과 별도 운영 승인이 모두 충족된 뒤에만 수행한다.
