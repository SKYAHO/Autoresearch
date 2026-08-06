"""실험 candidate 이미지·코드 참조 결정의 계약을 검증한다.

[파이프라인] ②candidate Job을 만들기 직전 — 의존성 diff에 따라 실험 이미지를 굽거나
기존 dev 이미지를 재사용하기로 결정하는 경계를 검증한다.

[기능] 결정 결과 타입의 불변식, 환경 설정 검증, 워크플로우 run·job conclusion 조합에
대한 판정과 dispatch 멱등성을 검증한다.

[비책임] GitHub Actions 러너에서 도는 diff 판단·아카이브 업로드·이미지 빌드는
``tests/test_experiment_build_workflow.py``와 실제 워크플로우 실행의 검증 범위다.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field

import httpx
import pytest

import agent_orchestration.experiment_build as experiment_build
from agent_orchestration.experiment_build.config import (
    ExperimentBuildConfigError,
    ExperimentBuildSettings,
)
from agent_orchestration.experiment_build.contracts import (
    CandidateRuntime,
    ExperimentBuildError,
    ImageBuildState,
)
from agent_orchestration.experiment_build.service import (
    BUILD_JOB_NAME,
    resolve_candidate_runtime,
    run_display_title,
)
from agent_orchestration.experiment_build.workflows import (
    GitHubWorkflowRuns,
    WorkflowRun,
    WorkflowRunClient,
    WorkflowRunError,
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


@pytest.mark.parametrize(
    "tag_form",
    [
        f"{FEAST_IMAGE_URI}:sha-deadbeef",
        f"{FEAST_IMAGE_URI}:latest",
        FEAST_IMAGE_URI,
        f"{FEAST_IMAGE_URI}@sha256:{'e' * 63}",
        f"{FEAST_IMAGE_URI}@sha256:{'E' * 64}",
    ],
)
def test_dev_feast_image_must_be_digest_pinned(tag_form: str) -> None:
    """dev 이미지는 저장소 관례대로 digest 고정이다.

    태그는 가변이다 — release가 재실행되면 같은 `sha-<sha>` 태그가 다른 이미지에
    다시 붙는다. 태그 예외는 실험 이미지(`exp-<sha>`)에만 적용된다(spec §3.3).
    """
    with pytest.raises(ExperimentBuildConfigError, match="dev_feast_image"):
        _settings(dev_feast_image=tag_form)


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


def test_find_run_raises_on_non_200_list_response() -> None:
    client = GitHubWorkflowRuns(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    )

    with pytest.raises(WorkflowRunError, match="list_failed"):
        asyncio.run(
            client.find_run(
                repository="SKYAHO/Autoresearch",
                workflow_file="experiment-image.yml",
                display_title=run_display_title(CANDIDATE_SHA),
                token="token",
            )
        )


def test_find_run_raises_on_missing_workflow_runs_key() -> None:
    client = GitHubWorkflowRuns(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={})
        )
    )

    with pytest.raises(WorkflowRunError, match="invalid_response"):
        asyncio.run(
            client.find_run(
                repository="SKYAHO/Autoresearch",
                workflow_file="experiment-image.yml",
                display_title=run_display_title(CANDIDATE_SHA),
                token="token",
            )
        )


def test_find_run_raises_when_workflow_runs_is_not_a_list() -> None:
    client = GitHubWorkflowRuns(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"workflow_runs": "not-a-list"})
        )
    )

    with pytest.raises(WorkflowRunError, match="invalid_response"):
        asyncio.run(
            client.find_run(
                repository="SKYAHO/Autoresearch",
                workflow_file="experiment-image.yml",
                display_title=run_display_title(CANDIDATE_SHA),
                token="token",
            )
        )


def test_find_run_raises_on_mistyped_id_field() -> None:
    title = run_display_title(CANDIDATE_SHA)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "workflow_runs": [
                    {
                        "id": "not-an-int",
                        "display_title": title,
                        "status": "completed",
                        "conclusion": "success",
                        "created_at": "2026-08-06T00:00:00Z",
                    }
                ]
            },
        )

    client = GitHubWorkflowRuns(transport=httpx.MockTransport(handler))

    with pytest.raises(WorkflowRunError, match="invalid_response"):
        asyncio.run(
            client.find_run(
                repository="SKYAHO/Autoresearch",
                workflow_file="experiment-image.yml",
                display_title=title,
                token="token",
            )
        )


def test_job_conclusion_raises_on_non_200_jobs_response() -> None:
    client = GitHubWorkflowRuns(
        transport=httpx.MockTransport(lambda request: httpx.Response(403))
    )

    with pytest.raises(WorkflowRunError, match="jobs_failed"):
        asyncio.run(
            client.job_conclusion(
                repository="SKYAHO/Autoresearch",
                run_id=7,
                job_name=BUILD_JOB_NAME,
                token="token",
            )
        )


def _protocol_methods(protocol: type) -> dict[str, object]:
    """프로토콜이 요구하는 공개 메서드만 뽑는다."""
    return {
        name: member
        for name, member in vars(protocol).items()
        if not name.startswith("_") and inspect.isfunction(member)
    }


def test_protocol_pins_exactly_the_three_operations() -> None:
    assert set(_protocol_methods(WorkflowRunClient)) == {
        "find_run",
        "dispatch",
        "job_conclusion",
    }


def test_github_workflow_runs_conforms_to_the_client_protocol() -> None:
    """REST 구현이 `WorkflowRunClient` 계약과 실제로 맞는지 시그니처로 대조한다.

    이 저장소에는 타입 검사기가 없어 어노테이션만으로는 아무것도 강제되지 않는다.
    메서드 이름을 바꾸거나 키워드 전용 인자를 위치 인자로 바꾸면 테스트는 통과한 채
    첫 실제 호출에서 터지므로, 파라미터 이름과 종류를 여기서 고정한다.
    """
    for name, protocol_method in _protocol_methods(WorkflowRunClient).items():
        implementation = getattr(GitHubWorkflowRuns, name, None)
        assert implementation is not None, f"GitHubWorkflowRuns에 {name}이(가) 없습니다"
        assert inspect.iscoroutinefunction(implementation), f"{name}은(는) async여야 합니다"

        def _parameters(function: object) -> list[tuple[str, inspect._ParameterKind]]:
            return [
                (parameter.name, parameter.kind)
                for parameter in inspect.signature(function).parameters.values()
                if parameter.name != "self"
            ]

        assert _parameters(implementation) == _parameters(protocol_method), (
            f"{name}의 파라미터가 WorkflowRunClient와 어긋납니다"
        )


def test_package_exports_what_a_caller_needs() -> None:
    """호출자는 이 패키지만 import해 클라이언트를 만들고 전송 오류를 잡을 수 있어야 한다."""
    for name in ("GitHubWorkflowRuns", "WorkflowRunClient", "WorkflowRunError"):
        assert name in experiment_build.__all__
        assert hasattr(experiment_build, name)

    assert experiment_build.__all__ == sorted(experiment_build.__all__)
    assert experiment_build.GitHubWorkflowRuns is GitHubWorkflowRuns
    assert experiment_build.WorkflowRunError is WorkflowRunError


def test_job_conclusion_raises_when_jobs_is_not_a_list() -> None:
    client = GitHubWorkflowRuns(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"jobs": {}})
        )
    )

    with pytest.raises(WorkflowRunError, match="invalid_response"):
        asyncio.run(
            client.job_conclusion(
                repository="SKYAHO/Autoresearch",
                run_id=7,
                job_name=BUILD_JOB_NAME,
                token="token",
            )
        )
