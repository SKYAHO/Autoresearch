# 실험 candidate 이미지 조건부 빌드 구현 계획

> **에이전트 작업자에게:** 이 계획은 task 단위로 실행합니다. 각 step은 체크박스(`- [ ]`)로
> 추적합니다. 정본 설계는 `docs/specs/2026-08-06-experiment-conditional-image-build.md`이며,
> 계획과 설계가 어긋나면 **설계가 정본**입니다.

**목표:** `candidate_sha`와 `base_dev_sha`를 주면 ②candidate 파드가 쓸 이미지 참조와
`CODE_ARCHIVE_SHA`를 돌려주는 인터페이스를, 의존성 diff가 있을 때만 실험 이미지를 굽는
GitHub Actions 워크플로우와 함께 만든다.

**아키텍처:** diff 판단·코드 아카이브 업로드·조건부 이미지 빌드는 전부 GitHub Actions
러너에서 한다(launcher 파드에 git·gcloud가 없다). Python은 워크플로우를 dispatch하고
run·job conclusion을 읽어 상태를 판정하기만 한다. "이미지를 실제로 구웠는가"는 build
job의 `skipped` vs `success`로 읽으므로 GAR·GCS 조회 권한이 필요 없다.

**기술 스택:** Python 3.11+ (`StrEnum`, `dataclass(frozen=True)`, `typing.Protocol`),
`httpx` (async), `pytest`, GitHub Actions, `docker/build-push-action@v6`, `gcloud`.

## Global Constraints

- 응답·커밋 메시지·docstring·주석은 **한국어 격식체**로 쓴다.
- 모든 새 모듈 최상단에 `[파이프라인]` / `[기능]` / `[비책임]` 3부 docstring을 단다
  (`.claude/docs/agent-python-reference.md`의 Module Responsibility).
- 모든 함수에 반환 타입을 포함한 타입 힌트를 유지한다.
- ruff 기본 설정을 쓴다 — `pyproject.toml`에 `line-length` 지정이 없으므로 **88자**다.
- `pytest-asyncio`/`anyio` 플러그인이 **없다.** async 함수는 동기 테스트 안에서
  `asyncio.run(...)`으로 호출한다 (`tests/test_experiment_executor.py`의 관례).
- **`agent_orchestration/launcher/jobs.py`를 수정하지 않는다** — #557과 충돌한다.
- **`.github/workflows/release.yml`과 `.github/workflows/code-archive.yml`을 수정하지
  않는다** — prod 릴리스·아카이브 경로다.
- 이미지 태그는 `exp-<candidate_sha>`만 쓴다. prod 태그 `sha-<sha>`를 만들지 않는다.
- `scripts/upload_code_archive.sh`에 **`--update-latest`를 절대 붙이지 않는다.**
- 회귀 판단 baseline: `uv run python -m pytest` = **68 failed / 2135 passed / 23 skipped**
  (2026-08-06, Windows). 실패 수가 늘지 않으면 회귀 없음으로 본다.

### 확정된 좌표 (기존 워크플로우에서 실측)

| 항목 | 값 | 출처 |
|---|---|---|
| GCS 업로드 SA | `secrets.GCS_CODE_UPLOADER_SA` | `code-archive.yml:83` |
| 코드 버킷 | `secrets.CODE_ARTIFACTS_BUCKET` | `code-archive.yml:90` |
| GAR push SA | `secrets.GAR_PUSHER_SA` | `release.yml:139` |
| dev 좌표 확인 액션 | `./.github/actions/resolve-dev-environment` | `code-archive.yml:74` |
| feast 이미지 URI | `${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${GAR_REPOSITORY}/autoresearch-feast` | `release.yml:1655` |

**아카이브 업로드 스텝을 `code-archive.yml`에서 재사용하지 않고 복제하는 이유:**
`code-archive.yml`에는 `workflow_call` 트리거가 없어 재사용하려면 prod 아카이브 경로를
수정해야 한다. 또 reusable workflow의 job은 부모 run에서
`<호출-job-id> / <피호출-job-name>` 형태로 나타나 §5.2의 job 이름 계약이 복잡해진다.
스텝 4개(액션 1 + 인증 1 + SDK 1 + 실행 1)를 복제하는 쪽이 위험이 작다.

## 파일 구조

| 파일 | 책임 |
|---|---|
| `agent_orchestration/experiment_build/__init__.py` | 공개 심볼 재노출 |
| `agent_orchestration/experiment_build/contracts.py` | `ImageBuildState`, `CandidateRuntime`, `ExperimentBuildError` |
| `agent_orchestration/experiment_build/config.py` | `ExperimentBuildSettings`, `ExperimentBuildConfigError` |
| `agent_orchestration/experiment_build/workflows.py` | `WorkflowRun`, `WorkflowRunClient`(Protocol), `GitHubWorkflowRuns` |
| `agent_orchestration/experiment_build/service.py` | `run_display_title`, `resolve_candidate_runtime` |
| `.github/workflows/experiment-image.yml` | `decide` + `build-experiment-feast-image` job |
| `scripts/bench/measure_experiment_image_build.sh` | 재빌드 회피 효과 측정 |
| `tests/test_experiment_build.py` | 계약·설정·판정 로직 |
| `tests/test_experiment_build_workflow.py` | 워크플로우 YAML 계약 |

---

## Task 1: 계약 타입과 설정

**Files:**
- Create: `agent_orchestration/experiment_build/__init__.py`
- Create: `agent_orchestration/experiment_build/contracts.py`
- Create: `agent_orchestration/experiment_build/config.py`
- Test: `tests/test_experiment_build.py`

**Interfaces:**
- Consumes: 없음
- Produces:
  - `ImageBuildState` (`StrEnum`: `READY="ready"`, `BUILD_PENDING="build_pending"`,
    `BUILD_FAILED="build_failed"`)
  - `CandidateRuntime(state: ImageBuildState, image_ref: str | None = None,
    code_archive_sha: str | None = None)` — frozen dataclass
  - `ExperimentBuildError(RuntimeError)`
  - `ExperimentBuildSettings(github_repository, feast_image_uri, dev_feast_image,
    workflow_file="experiment-image.yml", workflow_ref="main")` — frozen dataclass,
    `from_environment()` classmethod
  - `ExperimentBuildConfigError(ValueError)`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_experiment_build.py`를 새로 만든다.

```python
"""실험 candidate 이미지·코드 참조 결정의 계약을 검증한다.

[파이프라인] ②candidate Job을 만들기 직전 — 의존성 diff에 따라 실험 이미지를 굽거나
기존 dev 이미지를 재사용하기로 결정하는 경계를 검증한다.

[기능] 결정 결과 타입의 불변식, 환경 설정 검증, 워크플로우 run·job conclusion 조합에
대한 판정과 dispatch 멱등성을 검증한다.

[비책임] GitHub Actions 러너에서 도는 diff 판단·아카이브 업로드·이미지 빌드는
``tests/test_experiment_build_workflow.py``와 실제 워크플로우 실행의 검증 범위다.
"""

from __future__ import annotations

import pytest

from agent_orchestration.experiment_build.config import (
    ExperimentBuildConfigError,
    ExperimentBuildSettings,
)
from agent_orchestration.experiment_build.contracts import (
    CandidateRuntime,
    ImageBuildState,
)


CANDIDATE_SHA = "c" * 40
BASE_DEV_SHA = "d" * 40
FEAST_IMAGE_URI = "asia-northeast3-docker.pkg.dev/example/ar/autoresearch-feast"
DEV_FEAST_IMAGE = f"{FEAST_IMAGE_URI}@sha256:{'e' * 64}"


def _settings(**overrides: str) -> ExperimentBuildSettings:
    """검증을 통과하는 기본 설정에 일부 필드만 바꿔 만든다."""
    values = {
        "github_repository": "SKYAHO/Autoresearch",
        "feast_image_uri": FEAST_IMAGE_URI,
        "dev_feast_image": DEV_FEAST_IMAGE,
    }
    values.update(overrides)
    return ExperimentBuildSettings(**values)


def test_ready_runtime_requires_both_references() -> None:
    with pytest.raises(ValueError, match="image_ref"):
        CandidateRuntime(state=ImageBuildState.READY, image_ref=None)


def test_ready_runtime_requires_code_archive_sha() -> None:
    with pytest.raises(ValueError, match="code_archive_sha"):
        CandidateRuntime(
            state=ImageBuildState.READY,
            image_ref=DEV_FEAST_IMAGE,
            code_archive_sha=None,
        )


def test_pending_runtime_must_not_carry_references() -> None:
    with pytest.raises(ValueError, match="references"):
        CandidateRuntime(
            state=ImageBuildState.BUILD_PENDING,
            image_ref=DEV_FEAST_IMAGE,
            code_archive_sha=CANDIDATE_SHA,
        )


def test_pending_runtime_without_references_is_valid() -> None:
    runtime = CandidateRuntime(state=ImageBuildState.BUILD_PENDING)

    assert runtime.image_ref is None
    assert runtime.code_archive_sha is None


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("github_repository", "SKYAHO/Autoresearch/extra"),
        ("feast_image_uri", ""),
        ("dev_feast_image", "   "),
        ("workflow_file", "experiment-image.txt"),
        ("workflow_ref", ""),
    ],
)
def test_settings_reject_invalid_values(field_name: str, invalid_value: str) -> None:
    with pytest.raises(ExperimentBuildConfigError, match=field_name):
        _settings(**{field_name: invalid_value})


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_GITHUB_REPOSITORY", "SKYAHO/Autoresearch")
    monkeypatch.setenv("ORCH_EXPERIMENT_FEAST_IMAGE_URI", FEAST_IMAGE_URI)
    monkeypatch.setenv("ORCH_DEV_FEAST_IMAGE", DEV_FEAST_IMAGE)

    settings = ExperimentBuildSettings.from_environment()

    assert settings == _settings()
    assert settings.workflow_file == "experiment-image.yml"
    assert settings.workflow_ref == "main"


def test_settings_reject_missing_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORCH_EXPERIMENT_FEAST_IMAGE_URI", raising=False)
    monkeypatch.setenv("ORCH_GITHUB_REPOSITORY", "SKYAHO/Autoresearch")
    monkeypatch.setenv("ORCH_DEV_FEAST_IMAGE", DEV_FEAST_IMAGE)

    with pytest.raises(ExperimentBuildConfigError, match="ORCH_EXPERIMENT_FEAST_IMAGE_URI"):
        ExperimentBuildSettings.from_environment()
```

- [ ] **Step 2: 실패를 확인한다**

Run: `uv run python -m pytest tests/test_experiment_build.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agent_orchestration.experiment_build'`

- [ ] **Step 3: `contracts.py`를 만든다**

```python
"""실험 candidate 런타임 결정 결과의 값 타입 경계.

[파이프라인] ②candidate Job manifest를 조립하기 직전 — 어떤 이미지와 어떤 코드
아카이브로 실행할지가 확정됐는지, 아직 이미지 빌드를 기다려야 하는지를 나타내는
구간을 담당한다.

[기능] 준비 상태 열거값과, 상태별로 참조 필드가 있어야/없어야 하는 불변식을 강제하는
불변 결과 타입을 제공한다.

[비책임] 상태를 판정하는 규칙(`service`), GitHub API 호출(`workflows`), Experiment 상태
머신으로의 매핑(호출자)은 담당하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ExperimentBuildError(RuntimeError):
    """실험 이미지 빌드 상태를 신뢰할 수 없다."""


class ImageBuildState(StrEnum):
    """②candidate 파드 실행에 필요한 산출물의 준비 상태."""

    READY = "ready"
    BUILD_PENDING = "build_pending"
    BUILD_FAILED = "build_failed"


@dataclass(frozen=True)
class CandidateRuntime:
    """②candidate Job이 사용할 이미지 참조와 코드 아카이브 SHA."""

    state: ImageBuildState
    image_ref: str | None = None
    code_archive_sha: str | None = None

    def __post_init__(self) -> None:
        """READY에만 참조가 있고 그 외에는 없다는 계약을 fail-closed로 강제한다."""
        if self.state is ImageBuildState.READY:
            if not self.image_ref:
                raise ValueError("READY requires image_ref")
            if not self.code_archive_sha:
                raise ValueError("READY requires code_archive_sha")
            return
        if self.image_ref is not None or self.code_archive_sha is not None:
            raise ValueError(f"{self.state} must not carry references")
```

- [ ] **Step 4: `config.py`를 만든다**

```python
"""실험 이미지 빌드 인터페이스의 환경 설정 검증 경계.

[파이프라인] ②candidate Job을 만드는 호출자가 GitHub 워크플로우를 dispatch하고 결과를
읽기 전에, 저장소 좌표와 이미지 참조를 검증된 불변 값으로 바꾸는 구간을 담당한다.

[기능] 저장소 slug, 실험 이미지 GAR 경로, diff가 없을 때 재사용할 dev 이미지 참조,
dispatch 대상 워크플로우 파일·ref를 환경 변수에서 읽고 형식을 검증한다.

[비책임] GitHub 토큰 발급(`github_app`), 설정의 Kubernetes 주입(Autoresearch-infra),
워크플로우 호출 자체(`workflows`)는 담당하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re


_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_WORKFLOW_FILE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+\.ya?ml$")


class ExperimentBuildConfigError(ValueError):
    """실험 이미지 빌드 설정이 누락됐거나 형식 계약에 맞지 않는다."""


def _required_environment(name: str) -> str:
    """비어 있지 않은 환경 변수 값을 반환한다."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ExperimentBuildConfigError(f"missing {name}")
    return value


@dataclass(frozen=True)
class ExperimentBuildSettings:
    """실험 이미지 결정에 필요한 불변 설정."""

    github_repository: str
    feast_image_uri: str
    dev_feast_image: str
    workflow_file: str = "experiment-image.yml"
    workflow_ref: str = "main"

    def __post_init__(self) -> None:
        """빈 값과 형식 위반을 fail-closed로 거부한다."""
        if _REPOSITORY_PATTERN.fullmatch(self.github_repository) is None:
            raise ExperimentBuildConfigError("invalid github_repository")
        if _WORKFLOW_FILE_PATTERN.fullmatch(self.workflow_file) is None:
            raise ExperimentBuildConfigError("invalid workflow_file")
        required_strings = {
            "feast_image_uri": self.feast_image_uri,
            "dev_feast_image": self.dev_feast_image,
            "workflow_ref": self.workflow_ref,
        }
        for name, value in required_strings.items():
            if not value.strip():
                raise ExperimentBuildConfigError(f"invalid {name}")

    @classmethod
    def from_environment(cls) -> ExperimentBuildSettings:
        """호출자 환경에서 필수 설정을 읽고 기본 워크플로우 좌표를 적용한다."""
        return cls(
            github_repository=_required_environment("ORCH_GITHUB_REPOSITORY"),
            feast_image_uri=_required_environment("ORCH_EXPERIMENT_FEAST_IMAGE_URI"),
            dev_feast_image=_required_environment("ORCH_DEV_FEAST_IMAGE"),
        )
```

- [ ] **Step 5: `__init__.py`를 만든다**

```python
"""실험 candidate 파드의 이미지·코드 참조 결정 경계.

[파이프라인] ①이 만든 candidate 코드가 exp 브랜치에 올라간 뒤 ②candidate Job을
생성하기 직전 — 의존성 diff에 따라 실험 전용 이미지를 굽거나 기존 dev 이미지를
재사용하기로 결정하는 구간을 담당한다.

[기능] 결정 결과 타입, 환경 설정, GitHub Actions run·job 조회 경계와 판정 규칙을
하나의 인터페이스로 묶어 제공한다.

[비책임] ③baseline 파드의 이미지 결정(항상 고정된 dev 이미지 + `base_dev_sha`),
②③④ Job manifest 조립과 Experiment 상태 머신 전이(호출자), GitHub 토큰 발급
(`github_app`)은 담당하지 않는다.
"""

from __future__ import annotations

from agent_orchestration.experiment_build.config import (
    ExperimentBuildConfigError,
    ExperimentBuildSettings,
)
from agent_orchestration.experiment_build.contracts import (
    CandidateRuntime,
    ExperimentBuildError,
    ImageBuildState,
)


__all__ = [
    "CandidateRuntime",
    "ExperimentBuildConfigError",
    "ExperimentBuildError",
    "ExperimentBuildSettings",
    "ImageBuildState",
]
```

- [ ] **Step 6: 테스트 통과를 확인한다**

Run: `uv run python -m pytest tests/test_experiment_build.py -v`
Expected: PASS (11 passed)

- [ ] **Step 7: 린트**

Run: `uv run --no-sync ruff check agent_orchestration tests`
Expected: `All checks passed!`

- [ ] **Step 8: 커밋**

```bash
git add agent_orchestration/experiment_build tests/test_experiment_build.py
git commit -m "feat: 실험 candidate 런타임 결정 결과 타입과 설정 (#560)"
```

---

## Task 2: 판정 서비스와 클라이언트 프로토콜

**Files:**
- Create: `agent_orchestration/experiment_build/workflows.py` (Protocol과 값 타입만)
- Create: `agent_orchestration/experiment_build/service.py`
- Modify: `agent_orchestration/experiment_build/__init__.py` (재노출 추가)
- Test: `tests/test_experiment_build.py` (추가)

**Interfaces:**
- Consumes: Task 1의 `ImageBuildState`, `CandidateRuntime`, `ExperimentBuildError`,
  `ExperimentBuildSettings`
- Produces:
  - `WorkflowRun(run_id: int, status: str, conclusion: str | None)` — frozen dataclass
  - `WorkflowRunClient` Protocol — `find_run`, `dispatch`, `job_conclusion` (전부 async)
  - `run_display_title(candidate_sha: str) -> str`
  - `BUILD_JOB_NAME: str = "build-experiment-feast-image"`
  - `DECIDE_JOB_NAME: str = "decide"`
  - `async resolve_candidate_runtime(candidate_sha, base_dev_sha, *, workflows,
    settings, token) -> CandidateRuntime`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_experiment_build.py` 끝에 이어 붙인다. 파일 상단 import에 다음을 추가한다.

```python
import asyncio
from dataclasses import dataclass, field

from agent_orchestration.experiment_build.contracts import ExperimentBuildError
from agent_orchestration.experiment_build.service import (
    BUILD_JOB_NAME,
    resolve_candidate_runtime,
    run_display_title,
)
from agent_orchestration.experiment_build.workflows import WorkflowRun
```

```python
@dataclass
class FakeWorkflowRuns:
    """dispatch·조회 호출을 기록하는 WorkflowRunClient 구현."""

    run: WorkflowRun | None = None
    job_conclusions: dict[str, str] = field(default_factory=dict)
    dispatch_calls: list[dict[str, str]] = field(default_factory=list)
    find_calls: list[str] = field(default_factory=list)

    async def find_run(
        self, *, repository: str, workflow_file: str, display_title: str, token: str
    ) -> WorkflowRun | None:
        self.find_calls.append(display_title)
        return self.run

    async def dispatch(
        self,
        *,
        repository: str,
        workflow_file: str,
        ref: str,
        inputs: dict[str, str],
        token: str,
    ) -> None:
        self.dispatch_calls.append(dict(inputs))

    async def job_conclusion(
        self, *, repository: str, run_id: int, job_name: str, token: str
    ) -> str | None:
        return self.job_conclusions.get(job_name)


def _resolve(workflows: FakeWorkflowRuns) -> CandidateRuntime:
    """기본 좌표로 판정을 한 번 실행한다."""
    return asyncio.run(
        resolve_candidate_runtime(
            CANDIDATE_SHA,
            BASE_DEV_SHA,
            workflows=workflows,
            settings=_settings(),
            token="token",
        )
    )


def test_run_display_title_is_the_lookup_key() -> None:
    assert run_display_title(CANDIDATE_SHA) == f"experiment-image {CANDIDATE_SHA}"


def test_missing_run_is_dispatched_once_and_reports_pending() -> None:
    workflows = FakeWorkflowRuns(run=None)

    runtime = _resolve(workflows)

    assert runtime == CandidateRuntime(state=ImageBuildState.BUILD_PENDING)
    assert workflows.dispatch_calls == [
        {"base_dev_sha": BASE_DEV_SHA, "candidate_sha": CANDIDATE_SHA}
    ]


@pytest.mark.parametrize("status", ["queued", "in_progress"])
def test_unfinished_run_is_pending_and_never_redispatched(status: str) -> None:
    workflows = FakeWorkflowRuns(run=WorkflowRun(run_id=7, status=status, conclusion=None))

    runtime = _resolve(workflows)

    assert runtime.state is ImageBuildState.BUILD_PENDING
    assert workflows.dispatch_calls == []


def test_skipped_build_job_reuses_the_dev_image() -> None:
    workflows = FakeWorkflowRuns(
        run=WorkflowRun(run_id=7, status="completed", conclusion="success"),
        job_conclusions={BUILD_JOB_NAME: "skipped"},
    )

    runtime = _resolve(workflows)

    assert runtime == CandidateRuntime(
        state=ImageBuildState.READY,
        image_ref=DEV_FEAST_IMAGE,
        code_archive_sha=CANDIDATE_SHA,
    )


def test_successful_build_job_uses_the_experiment_tag() -> None:
    workflows = FakeWorkflowRuns(
        run=WorkflowRun(run_id=7, status="completed", conclusion="success"),
        job_conclusions={BUILD_JOB_NAME: "success"},
    )

    runtime = _resolve(workflows)

    assert runtime == CandidateRuntime(
        state=ImageBuildState.READY,
        image_ref=f"{FEAST_IMAGE_URI}:exp-{CANDIDATE_SHA}",
        code_archive_sha=CANDIDATE_SHA,
    )


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_failed_run_reports_build_failed(conclusion: str) -> None:
    workflows = FakeWorkflowRuns(
        run=WorkflowRun(run_id=7, status="completed", conclusion=conclusion)
    )

    runtime = _resolve(workflows)

    assert runtime == CandidateRuntime(state=ImageBuildState.BUILD_FAILED)


def test_successful_run_without_the_build_job_fails_closed() -> None:
    workflows = FakeWorkflowRuns(
        run=WorkflowRun(run_id=7, status="completed", conclusion="success"),
        job_conclusions={},
    )

    with pytest.raises(ExperimentBuildError, match=BUILD_JOB_NAME):
        _resolve(workflows)


@pytest.mark.parametrize(
    ("candidate", "base"),
    [
        ("C" * 40, BASE_DEV_SHA),
        ("c" * 39, BASE_DEV_SHA),
        (CANDIDATE_SHA, "not-a-sha"),
    ],
)
def test_invalid_sha_is_rejected_before_any_call(candidate: str, base: str) -> None:
    workflows = FakeWorkflowRuns()

    with pytest.raises(ValueError, match="sha"):
        asyncio.run(
            resolve_candidate_runtime(
                candidate,
                base,
                workflows=workflows,
                settings=_settings(),
                token="token",
            )
        )

    assert workflows.find_calls == []
    assert workflows.dispatch_calls == []
```

- [ ] **Step 2: 실패를 확인한다**

Run: `uv run python -m pytest tests/test_experiment_build.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named
'agent_orchestration.experiment_build.service'`

- [ ] **Step 3: `workflows.py`의 값 타입과 Protocol을 만든다**

이 step에서는 `GitHubWorkflowRuns` 구현을 넣지 않는다 (Task 3).

```python
"""GitHub Actions workflow run·job 조회와 dispatch REST 경계.

[파이프라인] 실험 이미지 워크플로우를 실행시키고 그 run과 job의 종료 상태를 읽어오는
구간을 담당한다.

[기능] run 식별에 필요한 값 타입과, 한 판정에 필요한 연산 3개(run 조회, dispatch,
job conclusion 조회)를 프로토콜로 정의한다.

[비책임] installation token 발급(`github_app`), 상태 판정 규칙(`service`), 워크플로우
자체의 동작은 담당하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class WorkflowRun:
    """실험 이미지 워크플로우 run 한 건의 종료 판단에 필요한 최소 상태."""

    run_id: int
    status: str
    conclusion: str | None


class WorkflowRunClient(Protocol):
    """한 판정에 필요한 GitHub Actions 연산."""

    async def find_run(
        self,
        *,
        repository: str,
        workflow_file: str,
        display_title: str,
        token: str,
    ) -> WorkflowRun | None: ...

    async def dispatch(
        self,
        *,
        repository: str,
        workflow_file: str,
        ref: str,
        inputs: dict[str, str],
        token: str,
    ) -> None: ...

    async def job_conclusion(
        self,
        *,
        repository: str,
        run_id: int,
        job_name: str,
        token: str,
    ) -> str | None: ...
```

- [ ] **Step 4: `service.py`를 만든다**

```python
"""실험 candidate 런타임 판정 규칙.

[파이프라인] ②candidate Job을 만들기 직전 — 실험 이미지 워크플로우를 필요하면
실행시키고, 그 결과로부터 쓸 이미지 참조와 코드 아카이브 SHA를 확정하는 구간을
담당한다.

[기능] 두 SHA를 검증하고, 같은 candidate에 대한 기존 run을 먼저 찾아 중복 dispatch를
막으며, run과 build job의 conclusion 조합을 준비 상태로 옮긴다.

[비책임] 의존성 diff 판단·코드 아카이브 업로드·이미지 빌드(`experiment-image.yml`),
③baseline 파드의 이미지 결정, `BUILD_FAILED`의 Experiment 상태 매핑(호출자)은 담당하지
않는다.
"""

from __future__ import annotations

import re

from agent_orchestration.experiment_build.config import ExperimentBuildSettings
from agent_orchestration.experiment_build.contracts import (
    CandidateRuntime,
    ExperimentBuildError,
    ImageBuildState,
)
from agent_orchestration.experiment_build.workflows import WorkflowRunClient


DECIDE_JOB_NAME = "decide"
BUILD_JOB_NAME = "build-experiment-feast-image"
RUN_NAME_PREFIX = "experiment-image "

_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_TERMINAL_RUN_STATUS = "completed"
_SUCCESS = "success"
_SKIPPED = "skipped"


def run_display_title(candidate_sha: str) -> str:
    """워크플로우 `run-name`과 같은 문자열을 만든다."""
    return f"{RUN_NAME_PREFIX}{candidate_sha}"


def _validate_sha(name: str, value: str) -> None:
    """40자 소문자 hex가 아니면 호출 전에 거부한다."""
    if _SHA_PATTERN.fullmatch(value) is None:
        raise ValueError(f"invalid {name}: must be a 40-character lowercase sha")


async def resolve_candidate_runtime(
    candidate_sha: str,
    base_dev_sha: str,
    *,
    workflows: WorkflowRunClient,
    settings: ExperimentBuildSettings,
    token: str,
) -> CandidateRuntime:
    """②candidate 파드가 쓸 이미지 참조와 코드 아카이브 SHA를 판정한다.

    같은 `candidate_sha`의 run이 이미 있으면 다시 dispatch하지 않는다. run이 성공했을
    때 이미지를 실제로 구웠는지는 build job의 conclusion(`skipped` 대 `success`)으로
    읽으므로 레지스트리 조회 권한이 필요 없다.
    """
    _validate_sha("candidate_sha", candidate_sha)
    _validate_sha("base_dev_sha", base_dev_sha)

    run = await workflows.find_run(
        repository=settings.github_repository,
        workflow_file=settings.workflow_file,
        display_title=run_display_title(candidate_sha),
        token=token,
    )
    if run is None:
        await workflows.dispatch(
            repository=settings.github_repository,
            workflow_file=settings.workflow_file,
            ref=settings.workflow_ref,
            inputs={
                "base_dev_sha": base_dev_sha,
                "candidate_sha": candidate_sha,
            },
            token=token,
        )
        return CandidateRuntime(state=ImageBuildState.BUILD_PENDING)

    if run.status != _TERMINAL_RUN_STATUS:
        return CandidateRuntime(state=ImageBuildState.BUILD_PENDING)
    if run.conclusion != _SUCCESS:
        return CandidateRuntime(state=ImageBuildState.BUILD_FAILED)

    conclusion = await workflows.job_conclusion(
        repository=settings.github_repository,
        run_id=run.run_id,
        job_name=BUILD_JOB_NAME,
        token=token,
    )
    if conclusion == _SKIPPED:
        image_ref = settings.dev_feast_image
    elif conclusion == _SUCCESS:
        image_ref = f"{settings.feast_image_uri}:exp-{candidate_sha}"
    else:
        raise ExperimentBuildError(
            f"run {run.run_id} succeeded but {BUILD_JOB_NAME} conclusion is {conclusion}"
        )
    return CandidateRuntime(
        state=ImageBuildState.READY,
        image_ref=image_ref,
        code_archive_sha=candidate_sha,
    )
```

- [ ] **Step 5: `__init__.py`에 재노출을 추가한다**

`__init__.py`의 import 블록과 `__all__`에 다음을 더한다.

```python
from agent_orchestration.experiment_build.service import (
    BUILD_JOB_NAME,
    DECIDE_JOB_NAME,
    resolve_candidate_runtime,
    run_display_title,
)
from agent_orchestration.experiment_build.workflows import (
    WorkflowRun,
    WorkflowRunClient,
)
```

`__all__`에 `"BUILD_JOB_NAME"`, `"DECIDE_JOB_NAME"`, `"WorkflowRun"`,
`"WorkflowRunClient"`, `"resolve_candidate_runtime"`, `"run_display_title"`를 추가하고
알파벳순을 유지한다.

- [ ] **Step 6: 테스트 통과를 확인한다**

Run: `uv run python -m pytest tests/test_experiment_build.py -v`
Expected: PASS (24 passed)

- [ ] **Step 7: 린트**

Run: `uv run --no-sync ruff check agent_orchestration tests`
Expected: `All checks passed!`

- [ ] **Step 8: 커밋**

```bash
git add agent_orchestration/experiment_build tests/test_experiment_build.py
git commit -m "feat: run·job conclusion 기반 candidate 런타임 판정 (#560)"
```

---

## Task 3: GitHub Actions REST 구현

**Files:**
- Modify: `agent_orchestration/experiment_build/workflows.py` (`GitHubWorkflowRuns` 추가)
- Test: `tests/test_experiment_build.py` (추가)

**Interfaces:**
- Consumes: Task 2의 `WorkflowRun`, `WorkflowRunClient`
- Produces: `GitHubWorkflowRuns(transport: httpx.AsyncBaseTransport | None = None)` —
  `WorkflowRunClient`를 만족하는 구현. `WorkflowRunError(RuntimeError)`.

`agent_orchestration/github_refs.py`와 같은 방식으로 쓴다: 토큰은 인자로 받고, 자격이나
응답 본문을 예외 메시지에 담지 않는다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_experiment_build.py`에 추가한다. 상단 import에 `import httpx`와 다음을
더한다.

```python
from agent_orchestration.experiment_build.workflows import (
    GitHubWorkflowRuns,
    WorkflowRunError,
)
```

```python
def _run_payload(display_title: str, run_id: int, created_at: str) -> dict:
    """workflow runs 목록 응답의 run 한 건을 만든다."""
    return {
        "id": run_id,
        "display_title": display_title,
        "status": "completed",
        "conclusion": "success",
        "created_at": created_at,
    }


def test_find_run_matches_display_title_and_prefers_the_newest() -> None:
    title = run_display_title(CANDIDATE_SHA)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "workflow_runs": [
                    _run_payload("experiment-image " + "a" * 40, 1, "2026-08-06T00:00:00Z"),
                    _run_payload(title, 2, "2026-08-06T01:00:00Z"),
                    _run_payload(title, 3, "2026-08-06T03:00:00Z"),
                ]
            },
        )

    client = GitHubWorkflowRuns(transport=httpx.MockTransport(handler))

    result = asyncio.run(
        client.find_run(
            repository="SKYAHO/Autoresearch",
            workflow_file="experiment-image.yml",
            display_title=title,
            token="token",
        )
    )

    assert result == WorkflowRun(run_id=3, status="completed", conclusion="success")
    assert requests[0].url.path == (
        "/repos/SKYAHO/Autoresearch/actions/workflows/experiment-image.yml/runs"
    )
    assert requests[0].url.params["event"] == "workflow_dispatch"
    assert requests[0].headers["Authorization"] == "Bearer token"


def test_find_run_returns_none_when_no_title_matches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"workflow_runs": []})

    client = GitHubWorkflowRuns(transport=httpx.MockTransport(handler))

    result = asyncio.run(
        client.find_run(
            repository="SKYAHO/Autoresearch",
            workflow_file="experiment-image.yml",
            display_title=run_display_title(CANDIDATE_SHA),
            token="token",
        )
    )

    assert result is None


def test_dispatch_posts_inputs_and_ref() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(204)

    client = GitHubWorkflowRuns(transport=httpx.MockTransport(handler))

    asyncio.run(
        client.dispatch(
            repository="SKYAHO/Autoresearch",
            workflow_file="experiment-image.yml",
            ref="main",
            inputs={"base_dev_sha": BASE_DEV_SHA, "candidate_sha": CANDIDATE_SHA},
            token="token",
        )
    )

    assert captured[0].url.path == (
        "/repos/SKYAHO/Autoresearch/actions/workflows/experiment-image.yml/dispatches"
    )
    assert json.loads(captured[0].content) == {
        "ref": "main",
        "inputs": {"base_dev_sha": BASE_DEV_SHA, "candidate_sha": CANDIDATE_SHA},
    }


def test_dispatch_raises_on_unexpected_status() -> None:
    client = GitHubWorkflowRuns(
        transport=httpx.MockTransport(lambda request: httpx.Response(422))
    )

    with pytest.raises(WorkflowRunError, match="dispatch_failed"):
        asyncio.run(
            client.dispatch(
                repository="SKYAHO/Autoresearch",
                workflow_file="experiment-image.yml",
                ref="main",
                inputs={"candidate_sha": CANDIDATE_SHA},
                token="token",
            )
        )


def test_job_conclusion_reads_the_named_job() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {"name": "decide", "conclusion": "success"},
                    {"name": BUILD_JOB_NAME, "conclusion": "skipped"},
                ]
            },
        )

    client = GitHubWorkflowRuns(transport=httpx.MockTransport(handler))

    result = asyncio.run(
        client.job_conclusion(
            repository="SKYAHO/Autoresearch",
            run_id=7,
            job_name=BUILD_JOB_NAME,
            token="token",
        )
    )

    assert result == "skipped"


def test_job_conclusion_returns_none_for_a_missing_job() -> None:
    client = GitHubWorkflowRuns(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"jobs": []})
        )
    )

    result = asyncio.run(
        client.job_conclusion(
            repository="SKYAHO/Autoresearch",
            run_id=7,
            job_name=BUILD_JOB_NAME,
            token="token",
        )
    )

    assert result is None
```

상단 import에 `import json`을 추가한다.

- [ ] **Step 2: 실패를 확인한다**

Run: `uv run python -m pytest tests/test_experiment_build.py -k "find_run or dispatch or job_conclusion" -v`
Expected: FAIL — `ImportError: cannot import name 'GitHubWorkflowRuns'`

- [ ] **Step 3: `GitHubWorkflowRuns`를 구현한다**

`workflows.py`의 import 블록에 `import httpx`를 추가하고, 모듈 상수와 클래스를 더한다.

```python
_GITHUB_API_URL = "https://api.github.com"
_API_VERSION = "2022-11-28"
_REQUEST_TIMEOUT_SEC = 30
_RUNS_PER_PAGE = 100


class WorkflowRunError(RuntimeError):
    """workflow run REST 호출이 실패했거나 응답을 신뢰할 수 없다."""

    def __init__(self, reason: str, *, status_code: int | None = None) -> None:
        self.reason = reason
        self.status_code = status_code
        suffix = f" (status={status_code})" if status_code is not None else ""
        super().__init__(f"{reason}{suffix}")


class GitHubWorkflowRuns:
    """installation token으로 GitHub Actions workflow run API를 호출한다."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def _request(
        self,
        method: str,
        path: str,
        token: str,
        *,
        params: dict[str, str | int] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> httpx.Response:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": _API_VERSION,
        }
        try:
            async with httpx.AsyncClient(
                base_url=_GITHUB_API_URL,
                headers=headers,
                timeout=_REQUEST_TIMEOUT_SEC,
                transport=self._transport,
            ) as client:
                return await client.request(
                    method, path, params=params, json=json_body
                )
        except httpx.HTTPError as error:
            raise WorkflowRunError("request_failed") from error

    async def find_run(
        self,
        *,
        repository: str,
        workflow_file: str,
        display_title: str,
        token: str,
    ) -> WorkflowRun | None:
        """`run-name`이 정확히 일치하는 run 중 가장 최근 것을 반환한다."""
        response = await self._request(
            "GET",
            f"/repos/{repository}/actions/workflows/{workflow_file}/runs",
            token,
            params={"event": "workflow_dispatch", "per_page": _RUNS_PER_PAGE},
        )
        if response.status_code != 200:
            raise WorkflowRunError("list_failed", status_code=response.status_code)
        try:
            payload = response.json()
        except ValueError as error:
            raise WorkflowRunError("invalid_response") from error
        runs = payload.get("workflow_runs") if isinstance(payload, dict) else None
        if not isinstance(runs, list):
            raise WorkflowRunError("invalid_response")
        matched = [
            run
            for run in runs
            if isinstance(run, dict) and run.get("display_title") == display_title
        ]
        if not matched:
            return None
        newest = max(matched, key=lambda run: str(run.get("created_at", "")))
        run_id = newest.get("id")
        status = newest.get("status")
        if not isinstance(run_id, int) or not isinstance(status, str):
            raise WorkflowRunError("invalid_response")
        conclusion = newest.get("conclusion")
        return WorkflowRun(
            run_id=run_id,
            status=status,
            conclusion=conclusion if isinstance(conclusion, str) else None,
        )

    async def dispatch(
        self,
        *,
        repository: str,
        workflow_file: str,
        ref: str,
        inputs: dict[str, str],
        token: str,
    ) -> None:
        """워크플로우를 실행시키고 204 외의 응답을 실패로 본다."""
        response = await self._request(
            "POST",
            f"/repos/{repository}/actions/workflows/{workflow_file}/dispatches",
            token,
            json_body={"ref": ref, "inputs": dict(inputs)},
        )
        if response.status_code != 204:
            raise WorkflowRunError("dispatch_failed", status_code=response.status_code)

    async def job_conclusion(
        self,
        *,
        repository: str,
        run_id: int,
        job_name: str,
        token: str,
    ) -> str | None:
        """run의 job 중 이름이 일치하는 것의 conclusion을 반환한다."""
        response = await self._request(
            "GET",
            f"/repos/{repository}/actions/runs/{run_id}/jobs",
            token,
            params={"per_page": _RUNS_PER_PAGE},
        )
        if response.status_code != 200:
            raise WorkflowRunError("jobs_failed", status_code=response.status_code)
        try:
            payload = response.json()
        except ValueError as error:
            raise WorkflowRunError("invalid_response") from error
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            raise WorkflowRunError("invalid_response")
        for job in jobs:
            if isinstance(job, dict) and job.get("name") == job_name:
                conclusion = job.get("conclusion")
                return conclusion if isinstance(conclusion, str) else None
        return None
```

`workflows.py`의 모듈 docstring [기능] 절에 "REST 구현은 목록 응답에서 `run-name`이
정확히 일치하는 가장 최근 run만 고른다"를 덧붙인다.

- [ ] **Step 4: 테스트 통과를 확인한다**

Run: `uv run python -m pytest tests/test_experiment_build.py -v`
Expected: PASS (30 passed)

- [ ] **Step 5: 린트**

Run: `uv run --no-sync ruff check agent_orchestration tests`
Expected: `All checks passed!`

- [ ] **Step 6: 커밋**

```bash
git add agent_orchestration/experiment_build/workflows.py tests/test_experiment_build.py
git commit -m "feat: GitHub Actions run·job 조회와 dispatch REST 구현 (#560)"
```

---

## Task 4: 워크플로우 `decide` job

**Files:**
- Create: `.github/workflows/experiment-image.yml`
- Test: `tests/test_experiment_build_workflow.py`

**Interfaces:**
- Consumes: Task 2의 `DECIDE_JOB_NAME`, `run_display_title`
- Produces: `decide` job — output `dependencies_changed` (`"true"` | `"false"`)

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_experiment_build_workflow.py`를 새로 만든다.

```python
"""실험 이미지 워크플로우와 Python 판정 계약의 일치를 검증한다.

[파이프라인] ②candidate Job을 만들기 직전 — GitHub Actions 러너에서 의존성 diff를
판단하고 코드 아카이브를 올리며 필요할 때만 실험 이미지를 굽는 구간의 계약을 검증한다.

[기능] run-name·job 이름·diff 대상 경로·태그 네임스페이스처럼 러너에서만 드러나는
계약을 워크플로우 텍스트에서 직접 꺼내 Python 상수와 대조한다.

[비책임] 실제 러너 실행(diff 판정·GCS 업로드·GAR push)과 판정 로직 자체는 각각 실제
워크플로우 실행과 ``tests/test_experiment_build.py``의 검증 범위다.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from agent_orchestration.experiment_build.service import (
    BUILD_JOB_NAME,
    DECIDE_JOB_NAME,
    RUN_NAME_PREFIX,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "experiment-image.yml"

DIFF_PATHS = (
    "pyproject.toml",
    "uv.lock",
    "Dockerfile.feast",
    "scripts/gcs_code_bootstrap.sh",
)


def _workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _workflow() -> dict:
    return yaml.safe_load(_workflow_text())


def _steps(job_id: str) -> list[dict]:
    return _workflow()["jobs"][job_id]["steps"]


def _step_run(job_id: str, name_fragment: str) -> str:
    for step in _steps(job_id):
        if name_fragment in step.get("name", ""):
            return step["run"]
    raise AssertionError(f"{job_id}에 '{name_fragment}' 스텝이 없습니다")


def test_run_name_matches_the_python_lookup_key() -> None:
    assert _workflow()["run-name"] == (
        RUN_NAME_PREFIX + "${{ inputs.candidate_sha }}"
    )


def test_job_names_are_pinned_to_their_ids() -> None:
    jobs = _workflow()["jobs"]

    assert set(jobs) == {DECIDE_JOB_NAME, BUILD_JOB_NAME}
    for job_id, job in jobs.items():
        assert job["name"] == job_id


def test_workflow_is_dispatch_only_with_both_shas_required() -> None:
    triggers = _workflow()[True]

    assert set(triggers) == {"workflow_dispatch"}
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"base_dev_sha", "candidate_sha"}
    for definition in inputs.values():
        assert definition["required"] is True
        assert definition["type"] == "string"


def test_concurrency_is_scoped_per_candidate_and_never_cancels() -> None:
    concurrency = _workflow()["concurrency"]

    assert concurrency["group"] == "experiment-image-${{ inputs.candidate_sha }}"
    assert concurrency["cancel-in-progress"] is False


def test_dev_and_exp_refs_are_fetched_explicitly() -> None:
    fetch = _step_run(DECIDE_JOB_NAME, "remote-tracking refs")

    assert "+refs/heads/dev:refs/remotes/origin/dev" in fetch
    assert "+refs/heads/exp/*:refs/remotes/origin/exp/*" in fetch


def test_provenance_guard_checks_dev_ancestry_and_exp_reachability() -> None:
    guard = _step_run(DECIDE_JOB_NAME, "provenance")

    assert 'merge-base --is-ancestor "$BASE_DEV_SHA" origin/dev' in guard
    assert 'git branch -r --contains "$CANDIDATE_SHA"' in guard
    assert "origin/exp/" in guard


def test_diff_compares_exactly_the_baked_paths() -> None:
    diff = _step_run(DECIDE_JOB_NAME, "rebuilt")

    assert "git diff --quiet" in diff
    for path in DIFF_PATHS:
        assert path in diff


def test_diff_treats_unexpected_exit_status_as_failure() -> None:
    diff = _step_run(DECIDE_JOB_NAME, "rebuilt")

    assert "0) changed=false" in diff
    assert "1) changed=true" in diff
    assert "*)" in diff
    assert "exit 1" in diff


def test_code_archive_upload_never_updates_latest() -> None:
    assert "--update-latest" not in _workflow_text()
    upload = _step_run(DECIDE_JOB_NAME, "code archive")
    assert "scripts/upload_code_archive.sh" in upload


def test_decide_job_publishes_the_dependency_decision() -> None:
    outputs = _workflow()["jobs"][DECIDE_JOB_NAME]["outputs"]

    assert outputs["dependencies_changed"] == (
        "${{ steps.diff.outputs.dependencies_changed }}"
    )
```

`yaml.safe_load`는 YAML 1.1 규칙으로 최상위 `on:` 키를 불리언 `True`로 읽는다.
`_workflow()[True]`는 오타가 아니다.

- [ ] **Step 2: 실패를 확인한다**

Run: `uv run python -m pytest tests/test_experiment_build_workflow.py -v`
Expected: FAIL — `FileNotFoundError` (`experiment-image.yml`이 없다)

- [ ] **Step 3: `experiment-image.yml`의 `decide` job을 만든다**

```yaml
name: Experiment candidate image

on:
  workflow_dispatch:
    inputs:
      base_dev_sha:
        description: Full 40-character base dev commit SHA
        required: true
        type: string
      candidate_sha:
        description: Full 40-character candidate commit SHA
        required: true
        type: string

run-name: experiment-image ${{ inputs.candidate_sha }}

permissions:
  contents: read

# 같은 candidate에 대한 중복 dispatch를 직렬화한다. 취소하지 않는 이유는 앞선 run이
# 코드 아카이브 업로드를 이미 끝냈을 수 있기 때문이다.
concurrency:
  group: experiment-image-${{ inputs.candidate_sha }}
  cancel-in-progress: false

jobs:
  decide:
    name: decide
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    outputs:
      dependencies_changed: ${{ steps.diff.outputs.dependencies_changed }}

    steps:
      - name: Validate input SHAs
        env:
          BASE_DEV_SHA: ${{ inputs.base_dev_sha }}
          CANDIDATE_SHA: ${{ inputs.candidate_sha }}
        shell: bash
        run: |
          for variable_name in BASE_DEV_SHA CANDIDATE_SHA; do
            if [[ ! "${!variable_name}" =~ ^[0-9a-f]{40}$ ]]; then
              echo "::error::$variable_name must be a 40-character lowercase SHA"
              exit 1
            fi
          done

      - name: Checkout candidate source
        uses: actions/checkout@v6
        with:
          ref: ${{ inputs.candidate_sha }}
          fetch-depth: 0

      - name: Fetch dev and exp remote-tracking refs
        shell: bash
        run: |
          git fetch --no-tags origin \
            '+refs/heads/dev:refs/remotes/origin/dev' \
            '+refs/heads/exp/*:refs/remotes/origin/exp/*'

      - name: Verify experiment provenance
        env:
          BASE_DEV_SHA: ${{ inputs.base_dev_sha }}
          CANDIDATE_SHA: ${{ inputs.candidate_sha }}
        shell: bash
        run: |
          if ! git merge-base --is-ancestor "$BASE_DEV_SHA" origin/dev; then
            echo "::error::base_dev_sha must be an ancestor of origin/dev"
            exit 1
          fi
          if ! git branch -r --contains "$CANDIDATE_SHA" \
            | grep -qE '^[[:space:]]*origin/exp/'; then
            echo "::error::candidate_sha must be reachable from an origin/exp/* branch"
            exit 1
          fi

      - name: Decide whether the image must be rebuilt
        id: diff
        env:
          BASE_DEV_SHA: ${{ inputs.base_dev_sha }}
          CANDIDATE_SHA: ${{ inputs.candidate_sha }}
        shell: bash
        run: |
          set +e
          git diff --quiet "$BASE_DEV_SHA" "$CANDIDATE_SHA" -- \
            pyproject.toml uv.lock Dockerfile.feast scripts/gcs_code_bootstrap.sh
          diff_status=$?
          set -e
          case "$diff_status" in
            0) changed=false ;;
            1) changed=true ;;
            *)
              echo "::error::git diff failed with status $diff_status"
              exit 1
              ;;
          esac
          echo "dependencies_changed=$changed" >> "$GITHUB_OUTPUT"
          echo "::notice::dependencies_changed=$changed"

      - name: dev 환경 좌표 확인
        uses: ./.github/actions/resolve-dev-environment
        with:
          configured_project_id: ${{ vars.GCP_PROJECT_ID }}
          configured_region: ${{ vars.GCP_REGION }}

      - name: Authenticate to GCP with Workload Identity Federation
        uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: ${{ vars.WIF_PROVIDER_ID }}
          service_account: ${{ secrets.GCS_CODE_UPLOADER_SA }}

      - name: Set up Cloud SDK
        uses: google-github-actions/setup-gcloud@v2

      - name: Upload candidate code archive
        env:
          CODE_ARTIFACTS_BUCKET: ${{ secrets.CODE_ARTIFACTS_BUCKET }}
          CANDIDATE_SHA: ${{ inputs.candidate_sha }}
        shell: bash
        run: scripts/upload_code_archive.sh "$CANDIDATE_SHA"
```

- [ ] **Step 4: `test_job_names_are_pinned_to_their_ids`만 아직 실패함을 확인한다**

Run: `uv run python -m pytest tests/test_experiment_build_workflow.py -v`
Expected: `test_job_names_are_pinned_to_their_ids` 1건 FAIL (build job이 아직 없다),
나머지 PASS

- [ ] **Step 5: 커밋**

```bash
git add .github/workflows/experiment-image.yml tests/test_experiment_build_workflow.py
git commit -m "feat: 실험 이미지 워크플로우의 diff 판단과 아카이브 업로드 (#560)"
```

---

## Task 5: 워크플로우 `build-experiment-feast-image` job

**Files:**
- Modify: `.github/workflows/experiment-image.yml`
- Test: `tests/test_experiment_build_workflow.py` (추가)

**Interfaces:**
- Consumes: Task 4의 `decide` job output `dependencies_changed`
- Produces: `build-experiment-feast-image` job — `dependencies_changed == 'true'`일 때만
  실행되고, `exp-<candidate_sha>` 태그를 push한다

- [ ] **Step 1: 실패하는 테스트를 쓴다**

`tests/test_experiment_build_workflow.py`에 추가한다.

```python
def test_build_job_runs_only_when_dependencies_changed() -> None:
    job = _workflow()["jobs"][BUILD_JOB_NAME]

    assert job["needs"] == DECIDE_JOB_NAME
    assert job["if"] == (
        f"needs.{DECIDE_JOB_NAME}.outputs.dependencies_changed == 'true'"
    )


def test_build_job_uses_the_feast_dockerfile() -> None:
    build = next(
        step
        for step in _steps(BUILD_JOB_NAME)
        if step.get("uses", "").startswith("docker/build-push-action")
    )

    assert build["with"]["file"] == "Dockerfile.feast"
    assert build["with"]["push"] is True
    assert "VCS_REF=${{ inputs.candidate_sha }}" in build["with"]["build-args"]


def test_experiment_tag_never_collides_with_the_prod_namespace() -> None:
    text = _workflow_text()

    assert "exp-${CANDIDATE_SHA}" in text
    assert "sha-${SOURCE_SHA}" not in text
    assert "sha-${CANDIDATE_SHA}" not in text


def test_existing_tag_is_never_overwritten() -> None:
    guard = _step_run(BUILD_JOB_NAME, "Refuse to overwrite")

    assert "gcloud artifacts docker images describe" in guard
    assert "exists=true" in guard
    assert "exists=false" in guard

    for step in _steps(BUILD_JOB_NAME):
        if step.get("uses", "").startswith("docker/build-push-action"):
            assert step["if"] == "steps.existing.outputs.exists == 'false'"


def test_build_job_pushes_with_the_gar_pusher_identity() -> None:
    auth = next(
        step
        for step in _steps(BUILD_JOB_NAME)
        if step.get("uses", "").startswith("google-github-actions/auth")
    )

    assert auth["with"]["service_account"] == "${{ secrets.GAR_PUSHER_SA }}"


def test_no_promotion_job_can_leak_the_experiment_image_to_prod() -> None:
    text = _workflow_text()

    assert "promote" not in text
    assert "Autoresearch-airflow" not in text
    assert "values.yaml" not in text


def test_image_verification_matches_the_release_feast_contract() -> None:
    verify = _step_run(BUILD_JOB_NAME, "Verify experiment image")

    assert "sha256:[0-9a-f]{64}" in verify
    assert "org.opencontainers.image.revision" in verify
    assert "must run as a non-root user" in verify
    for module in (
        "feast",
        "pyarrow",
        "lightgbm",
        "onnxmltools",
        "onnxruntime",
        "joblib",
        "mlflow",
    ):
        assert module in verify
```

- [ ] **Step 2: 실패를 확인한다**

Run: `uv run python -m pytest tests/test_experiment_build_workflow.py -v`
Expected: FAIL — `KeyError: 'build-experiment-feast-image'`

- [ ] **Step 3: build job을 추가한다**

`experiment-image.yml`의 `jobs:` 아래, `decide` job 다음에 이어 붙인다.

```yaml
  build-experiment-feast-image:
    name: build-experiment-feast-image
    needs: decide
    if: needs.decide.outputs.dependencies_changed == 'true'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write

    steps:
      - name: Checkout candidate source
        uses: actions/checkout@v6
        with:
          ref: ${{ inputs.candidate_sha }}
          fetch-depth: 1

      - name: Validate build configuration
        env:
          GCP_PROJECT_ID: ${{ vars.GCP_PROJECT_ID }}
          GCP_REGION: ${{ vars.GCP_REGION }}
          GAR_REPOSITORY: ${{ vars.GAR_REPOSITORY }}
          WIF_PROVIDER_ID: ${{ vars.WIF_PROVIDER_ID }}
          GAR_PUSHER_SA: ${{ secrets.GAR_PUSHER_SA }}
        shell: bash
        run: |
          for variable_name in \
            GCP_PROJECT_ID GCP_REGION GAR_REPOSITORY \
            WIF_PROVIDER_ID GAR_PUSHER_SA
          do
            if [[ -z "${!variable_name}" ]]; then
              echo "::error::$variable_name is not configured"
              exit 1
            fi
          done

      - name: dev 환경 좌표 확인
        uses: ./.github/actions/resolve-dev-environment
        with:
          configured_project_id: ${{ vars.GCP_PROJECT_ID }}
          configured_region: ${{ vars.GCP_REGION }}

      - name: Authenticate to GCP with Workload Identity Federation
        uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: ${{ vars.WIF_PROVIDER_ID }}
          service_account: ${{ secrets.GAR_PUSHER_SA }}

      - name: Set up Cloud SDK
        uses: google-github-actions/setup-gcloud@v2

      - name: Configure Docker for GAR
        env:
          GCP_REGION: ${{ vars.GCP_REGION }}
        run: gcloud auth configure-docker "${GCP_REGION}-docker.pkg.dev" --quiet

      - name: Resolve experiment image reference
        id: image
        env:
          GCP_PROJECT_ID: ${{ vars.GCP_PROJECT_ID }}
          GCP_REGION: ${{ vars.GCP_REGION }}
          GAR_REPOSITORY: ${{ vars.GAR_REPOSITORY }}
          CANDIDATE_SHA: ${{ inputs.candidate_sha }}
        shell: bash
        run: |
          image_uri="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${GAR_REPOSITORY}/autoresearch-feast"
          {
            echo "uri=$image_uri"
            echo "tag=${image_uri}:exp-${CANDIDATE_SHA}"
          } >> "$GITHUB_OUTPUT"

      - name: Refuse to overwrite an existing experiment tag
        id: existing
        env:
          IMAGE_TAG: ${{ steps.image.outputs.tag }}
        shell: bash
        run: |
          if gcloud artifacts docker images describe "$IMAGE_TAG" --quiet >/dev/null 2>&1
          then
            echo "::notice::$IMAGE_TAG already exists; reusing it without rebuilding"
            echo "exists=true" >> "$GITHUB_OUTPUT"
          else
            echo "exists=false" >> "$GITHUB_OUTPUT"
          fi

      - name: Set up Docker Buildx
        if: steps.existing.outputs.exists == 'false'
        uses: docker/setup-buildx-action@v3

      - name: Build and push experiment feast image
        id: build
        if: steps.existing.outputs.exists == 'false'
        uses: docker/build-push-action@v6
        with:
          context: .
          file: Dockerfile.feast
          push: true
          build-args: |
            VCS_REF=${{ inputs.candidate_sha }}
          tags: ${{ steps.image.outputs.tag }}

      - name: Verify experiment image
        if: steps.existing.outputs.exists == 'false'
        env:
          DIGEST: ${{ steps.build.outputs.digest }}
          IMAGE_URI: ${{ steps.image.outputs.uri }}
          CANDIDATE_SHA: ${{ inputs.candidate_sha }}
        shell: bash
        run: |
          if [[ ! "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]; then
            echo "::error::Build did not return a valid registry digest"
            exit 1
          fi

          digest_ref="${IMAGE_URI}@${DIGEST}"
          docker pull "$digest_ref"

          image_revision="$(docker image inspect "$digest_ref" \
            --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
          if [[ "$image_revision" != "$CANDIDATE_SHA" ]]; then
            echo "::error::OCI revision does not match candidate_sha"
            exit 1
          fi

          image_user="$(docker image inspect "$digest_ref" --format '{{ .Config.User }}')"
          if [[ -z "$image_user" || "$image_user" == "0" || "$image_user" == "root" ]]; then
            echo "::error::Experiment feast image must run as a non-root user"
            exit 1
          fi

          # release.yml의 feast verify와 같은 계약이다. 코드는 이미지에 없고 ENTRYPOINT가
          # GCS 부트스트랩이므로 엔트리포인트를 우회해 baked venv만 검증한다.
          docker run --rm --entrypoint python "$digest_ref" \
            -c "import feast, pyarrow, lightgbm, onnxmltools, onnxruntime, joblib, mlflow"

      - name: Write experiment image summary
        env:
          CANDIDATE_SHA: ${{ inputs.candidate_sha }}
          IMAGE_TAG: ${{ steps.image.outputs.tag }}
          REUSED: ${{ steps.existing.outputs.exists }}
        shell: bash
        run: |
          {
            echo "## Experiment feast image"
            echo
            echo "- Candidate SHA: \`$CANDIDATE_SHA\`"
            echo "- Experiment tag: \`$IMAGE_TAG\`"
            echo "- Reused existing tag: \`$REUSED\`"
            echo
            echo "Experiment images never enter the prod namespace (\`sha-*\`)."
          } >> "$GITHUB_STEP_SUMMARY"
```

- [ ] **Step 4: 테스트 통과를 확인한다**

Run: `uv run python -m pytest tests/test_experiment_build_workflow.py -v`
Expected: PASS (17 passed)

- [ ] **Step 5: 워크플로우 문법을 검사한다**

Run: `git diff --check`
Expected: 출력 없음

Run: `actionlint .github/workflows/experiment-image.yml`
Expected: 출력 없음. `actionlint`가 없으면 건너뛰고 그 사실을 커밋 메시지가 아니라
작업 보고에 남긴다.

- [ ] **Step 6: 커밋**

```bash
git add .github/workflows/experiment-image.yml tests/test_experiment_build_workflow.py
git commit -m "feat: 의존성 diff가 있을 때만 도는 실험 feast 이미지 빌드 (#560)"
```

---

## Task 6: 측정 스크립트, 환경 변수, 전체 검증

**Files:**
- Create: `scripts/bench/measure_experiment_image_build.sh`
- Modify: `.env.example`
- Test: 전체 스위트

**Interfaces:**
- Consumes: Task 1의 `ExperimentBuildSettings.from_environment()`가 읽는 변수명,
  Task 2의 `BUILD_JOB_NAME`
- Produces: 없음 (검증·문서 산출물)

- [ ] **Step 1: `.env.example`에 새 변수를 추가한다**

`.env.example`에서 다른 `ORCH_` 변수가 모여 있는 구획을 찾아 그 아래에 이어 붙인다.
구획이 없으면 파일 끝에 붙인다.

```bash
# 실험 candidate 이미지 조건부 빌드(#560) — agent_orchestration.experiment_build
# 실험 전용 feast 이미지를 올릴 GAR 경로 (태그 없이 저장소 경로까지)
ORCH_EXPERIMENT_FEAST_IMAGE_URI=asia-northeast3-docker.pkg.dev/PROJECT/REPO/autoresearch-feast
# 의존성 diff가 없을 때 그대로 재사용할 dev feast 이미지 참조
ORCH_DEV_FEAST_IMAGE=asia-northeast3-docker.pkg.dev/PROJECT/REPO/autoresearch-feast:sha-DEADBEEF
```

`ORCH_GITHUB_REPOSITORY`가 이미 있으면 다시 추가하지 않는다.

- [ ] **Step 2: 측정 스크립트를 만든다**

```bash
#!/usr/bin/env bash
# 실험 이미지 조건부 빌드의 재빌드 회피 효과를 측정한다.
# 기록 위치: experiments/2026-08-06_experiment-conditional-image-build/notes.md
set -euo pipefail

REPOSITORY="${REPOSITORY:-SKYAHO/Autoresearch}"
BUILD_JOB_NAME="build-experiment-feast-image"

usage() {
  cat <<'USAGE'
사용법: scripts/bench/measure_experiment_image_build.sh [--before|--after] [건수]

  --before  release.yml의 feast 이미지 빌드 job 소요 시간 (회피되는 비용)
  --after   experiment-image.yml의 재빌드 회피율
  건수      조회할 최근 run 수 (기본 5)
USAGE
}

mode=""
limit=5
for arg in "$@"; do
  case "$arg" in
    --before) mode="before" ;;
    --after) mode="after" ;;
    -h|--help) usage; exit 0 ;;
    *[!0-9]*)
      echo "오류: 알 수 없는 인자: $arg" >&2
      usage >&2
      exit 2
      ;;
    *) limit="$arg" ;;
  esac
done

if [[ -z "$mode" ]]; then
  usage >&2
  exit 2
fi

elapsed_seconds() {
  python - "$1" "$2" <<'PY'
import sys
from datetime import datetime

started, completed = sys.argv[1], sys.argv[2]
fmt = "%Y-%m-%dT%H:%M:%SZ"
print(int((datetime.strptime(completed, fmt) - datetime.strptime(started, fmt)).total_seconds()))
PY
}

if [[ "$mode" == "before" ]]; then
  echo "# release.yml feast 이미지 빌드 소요 (최근 ${limit}건)"
  gh run list --repo "$REPOSITORY" --workflow release.yml --limit "$limit" \
    --json databaseId -q '.[].databaseId' \
  | while read -r run_id; do
      gh api "repos/${REPOSITORY}/actions/runs/${run_id}/jobs" \
        --jq '.jobs[] | select(.name | test("feast image")) | "\(.started_at) \(.completed_at)"' \
      | while read -r started completed; do
          printf '%s\t%ss\n' "$run_id" "$(elapsed_seconds "$started" "$completed")"
        done
    done
  exit 0
fi

echo "# experiment-image.yml 재빌드 회피율 (최근 ${limit}건)"
total=0
skipped=0
while read -r run_id; do
  conclusion="$(gh api "repos/${REPOSITORY}/actions/runs/${run_id}/jobs" \
    --jq ".jobs[] | select(.name == \"${BUILD_JOB_NAME}\") | .conclusion")"
  if [[ -z "$conclusion" ]]; then
    conclusion="skipped"
  fi
  total=$((total + 1))
  if [[ "$conclusion" == "skipped" ]]; then
    skipped=$((skipped + 1))
  fi
  printf '%s\t%s\n' "$run_id" "$conclusion"
done < <(gh run list --repo "$REPOSITORY" --workflow experiment-image.yml \
  --limit "$limit" --json databaseId -q '.[].databaseId')

if (( total > 0 )); then
  printf '회피율: %d/%d (%d%%)\n' "$skipped" "$total" $(( skipped * 100 / total ))
else
  echo "run이 없습니다"
fi
```

- [ ] **Step 3: 스크립트를 실행 가능하게 만들고 `--before`로 동작을 확인한다**

Run:
```bash
git update-index --chmod=+x scripts/bench/measure_experiment_image_build.sh
bash scripts/bench/measure_experiment_image_build.sh --before 3
```
Expected: run id와 초 단위 소요가 3줄 (baseline 기록의 3:22 / 3:14 / 3:20에 해당하는
200초 안팎). `gh` 인증이 없으면 이 step은 건너뛰고 작업 보고에 남긴다.

- [ ] **Step 4: 전체 스위트로 회귀를 확인한다**

Run: `uv run python -m pytest -q`
Expected: **68 failed / 2135 passed + 신규 통과분 / 23 skipped.**
실패 수가 68을 넘으면 신규 회귀이므로 멈추고 원인을 찾는다.

- [ ] **Step 5: 린트**

Run: `uv run --no-sync ruff check agent_orchestration autoresearch tests tools`
Expected: `All checks passed!`

- [ ] **Step 6: 커밋**

```bash
git add scripts/bench/measure_experiment_image_build.sh .env.example
git commit -m "chore: 실험 이미지 재빌드 회피 측정 스크립트와 환경 변수 (#560)"
```

---

## 실행 후 남는 수동 검증

머지 전에 러너에서만 드러나는 것 3가지를 실제 dispatch로 확인한다.
`experiment-image.yml`은 `main`에 있어야 `workflow_dispatch`가 뜨므로, 이 검증은 PR
머지 직후 또는 브랜치를 `workflow_dispatch`의 ref로 지정해 수행한다.

1. **`origin/dev`·`origin/exp/*` ref 존재** — `Verify experiment provenance` 스텝이
   통과하는가. `actions/checkout@v6`의 `fetch-depth: 0`이 모든 브랜치를 받아온다는
   문서 서술이 `ref:`를 SHA로 준 경우에도 성립하는지는 여기서만 확정된다. 명시적 fetch를
   넣었으므로 어느 쪽이든 통과해야 한다.
2. **diff 양쪽 경로** — 의존성을 안 바꾼 exp 브랜치 SHA로 한 번(빌드 job `skipped`),
   `pyproject.toml`을 바꾼 SHA로 한 번(빌드 job `success`) 돌린다.
3. **태그 덮어쓰기 거부** — 2의 두 번째를 같은 `candidate_sha`로 다시 dispatch해
   `Refuse to overwrite an existing experiment tag`가 `exists=true`를 내고 빌드가
   건너뛰어지는지 본다.

각 run에서 `experiments/2026-08-06_experiment-conditional-image-build/`에 raw 출력을
`raw_*.txt`로 남기고 notes.md의 **After** 절을 채운다.
