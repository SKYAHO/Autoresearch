"""Agent Orchestration API·Runner 이미지 경계 계약."""

import ast
from pathlib import Path

import pytest
import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
API_DOCKERFILE = REPOSITORY_ROOT / "deploy" / "agent_orchestration" / "api.Dockerfile"
RUNNER_DOCKERFILE = (
    REPOSITORY_ROOT / "deploy" / "agent_orchestration" / "runner.Dockerfile"
)
LAUNCHER_DOCKERFILE = (
    REPOSITORY_ROOT / "deploy" / "agent_orchestration" / "launcher.Dockerfile"
)
EXECUTOR_DOCKERFILE = (
    REPOSITORY_ROOT / "deploy" / "agent_orchestration" / "executor.Dockerfile"
)
DOCKERIGNORE = REPOSITORY_ROOT / ".dockerignore"
RELEASE_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"
CI_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
API_LLM_MODULE = REPOSITORY_ROOT / "agent_orchestration" / "app" / "llm.py"


def test_api_image_excludes_codex_and_runner_image_pins_codex() -> None:
    """API에는 Codex 실행 표면이 없고 Runner만 검증된 CLI를 설치한다."""
    api_dockerfile = API_DOCKERFILE.read_text(encoding="utf-8")
    runner_dockerfile = RUNNER_DOCKERFILE.read_text(encoding="utf-8")

    assert "@openai/codex" not in api_dockerfile
    assert "node:" not in api_dockerfile
    assert "CODEX_HOME" not in api_dockerfile
    assert "@openai/codex@0.146.0" in runner_dockerfile
    assert "CODEX_HOME=/var/lib/codex" in runner_dockerfile
    assert "TMPDIR=/tmp" in runner_dockerfile
    assert "COPY --from=codex-cli /usr/local/bin/codex /usr/local/bin/codex" not in (
        runner_dockerfile
    )
    assert (
        "ln -s /usr/local/lib/node_modules/@openai/codex/bin/codex.js "
        "/usr/local/bin/codex"
    ) in runner_dockerfile


def test_orchestration_images_install_only_runtime_group_and_run_as_fixed_user() -> None:
    """두 역할 이미지는 같은 최소 Python 의존성과 비루트 UID/GID를 사용한다."""
    for dockerfile_path in (API_DOCKERFILE, RUNNER_DOCKERFILE):
        dockerfile = dockerfile_path.read_text(encoding="utf-8")

        assert "FROM ghcr.io/astral-sh/uv:0.11.26 AS lock-export" in dockerfile
        assert '"--only-group", "orchestration"' in dockerfile
        assert '"--no-dev", "--group", "orchestration"' not in dockerfile
        assert "addgroup --gid 10001 appuser" in dockerfile
        assert "adduser --uid 10001 --gid 10001" in dockerfile
        assert "USER appuser" in dockerfile


def test_revision_label_preserves_runtime_dependency_cache() -> None:
    """소스 revision 라벨은 대용량 런타임 의존성 설치 이후에 설정한다."""
    label = 'LABEL org.opencontainers.image.revision="${VCS_REF}"'

    for dockerfile_path in (API_DOCKERFILE, RUNNER_DOCKERFILE):
        dockerfile = dockerfile_path.read_text(encoding="utf-8")

        assert dockerfile.index(label) > dockerfile.index(
            "RUN python -m pip install --no-cache-dir --no-deps -r requirements.lock"
        )


def test_orchestration_images_do_not_embed_runtime_secrets() -> None:
    """빌드 문맥의 인증·환경·DB 값을 이미지 명령으로 반입하지 않는다."""
    forbidden_values = ("auth.json", ".env", "DATABASE_URL", "ORCH_DATABASE_URL")

    for dockerfile_path in (API_DOCKERFILE, RUNNER_DOCKERFILE):
        dockerfile = dockerfile_path.read_text(encoding="utf-8")
        assert not any(value in dockerfile for value in forbidden_values)

    dockerignore = DOCKERIGNORE.read_text(encoding="utf-8")
    assert ".codex" in dockerignore
    assert ".env" in dockerignore
    assert "**/auth.json" in dockerignore


def test_api_and_runner_images_copy_only_their_runtime_modules() -> None:
    """각 이미지는 상대 역할의 애플리케이션 모듈을 포함하지 않는다."""
    api_dockerfile = API_DOCKERFILE.read_text(encoding="utf-8")
    runner_dockerfile = RUNNER_DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY agent_orchestration/app ./agent_orchestration/app" in api_dockerfile
    assert "COPY agent_orchestration/contracts.py ./agent_orchestration/" in api_dockerfile
    assert "COPY agent_orchestration/bootstrap_secrets.py ./agent_orchestration/" in api_dockerfile
    assert "COPY agent_orchestration/entrypoint.sh ./agent_orchestration/" in api_dockerfile
    assert "COPY agent_orchestration/runner" not in api_dockerfile
    assert "COPY agent_orchestration/codex.py" not in api_dockerfile

    assert "COPY agent_orchestration/runner ./agent_orchestration/runner" in runner_dockerfile
    assert "COPY agent_orchestration/codex.py ./agent_orchestration/" in runner_dockerfile
    assert "COPY agent_orchestration/contracts.py ./agent_orchestration/" in runner_dockerfile
    assert "COPY agent_orchestration/runner_entrypoint.sh ./agent_orchestration/" in runner_dockerfile
    assert "COPY agent_orchestration/app" not in runner_dockerfile


def test_api_llm_module_defers_codex_execution_import() -> None:
    """Runner 전용 실행 모듈 없이 API 앱을 import할 수 있어야 한다."""
    module = ast.parse(API_LLM_MODULE.read_text(encoding="utf-8"))
    top_level_imports = {
        node.module
        for node in module.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }

    assert "agent_orchestration.codex" not in top_level_imports


def test_release_workflow_publishes_api_and_runner_digests() -> None:
    """Release는 동일 source SHA의 API·Runner immutable digest를 각각 발행한다."""
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    api_dockerfile = API_DOCKERFILE.read_text(encoding="utf-8")
    runner_dockerfile = RUNNER_DOCKERFILE.read_text(encoding="utf-8")

    assert "publish-agent-orchestration-api-image:" in workflow
    assert "publish-agent-orchestration-runner-image:" in workflow
    assert "file: deploy/agent_orchestration/api.Dockerfile" in workflow
    assert "file: deploy/agent_orchestration/runner.Dockerfile" in workflow
    assert "autoresearch-agent-orchestration-api" in workflow
    assert "autoresearch-agent-orchestration-runner" in workflow
    assert workflow.count("needs: publish-application-image") >= 3
    assert workflow.count("org.opencontainers.image.revision") >= 4
    assert workflow.count("digest_ref=$digest_ref") >= 4

    for dockerfile in (api_dockerfile, runner_dockerfile):
        assert "ARG VCS_REF=unknown" in dockerfile
        assert 'org.opencontainers.image.revision="${VCS_REF}"' in dockerfile


def test_launcher_image_is_a_locked_non_root_runtime_image() -> None:
    """Launcher는 Phase 1의 최소 runtime과 역할별 command를 유지한다."""
    dockerfile = LAUNCHER_DOCKERFILE.read_text(encoding="utf-8")

    assert "FROM ghcr.io/astral-sh/uv:0.11.26 AS lock-export" in dockerfile
    assert '"--only-group", "orchestration"' in dockerfile
    assert "addgroup --gid 10001 appuser" in dockerfile
    assert "adduser --uid 10001 --gid 10001" in dockerfile
    assert "USER appuser" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "COPY agent_orchestration/launcher ./agent_orchestration/launcher" in dockerfile
    assert 'CMD ["python", "-m", "agent_orchestration.launcher.main"]' in dockerfile
    assert "ARG VCS_REF=unknown" in dockerfile
    assert 'org.opencontainers.image.revision="${VCS_REF}"' in dockerfile
    assert "@openai/codex" not in dockerfile
    assert "node:" not in dockerfile


def test_executor_image_seals_the_phase2_runtime_contract() -> None:
    """Executor는 clone source와 독립된 Git·uv·Codex 검증 runtime을 제공한다.

    이 테스트가 잡는 변경: executor가 dev 검증 도구, Codex 또는 image-봉인 issue
    parser 없이 빌드되어 Stage 6의 어느 container라도 실행하지 못하는 회귀.
    """
    dockerfile = EXECUTOR_DOCKERFILE.read_text(encoding="utf-8")

    assert "FROM ghcr.io/astral-sh/uv:0.11.26 AS lock-export" in dockerfile
    assert '"--group", "dev"' in dockerfile
    assert '"--no-group", "feast"' in dockerfile
    assert "FROM node:22.16.0-slim AS codex-cli" in dockerfile
    assert "@openai/codex@0.146.0" in dockerfile
    assert 'codex exec --help | grep --fixed-strings -- "danger-full-access"' in dockerfile
    assert "apt-get install --yes --no-install-recommends git" in dockerfile
    assert "COPY --from=lock-export /uv /usr/local/bin/uv" in dockerfile
    assert "UV_PROJECT_ENVIRONMENT=/opt/autoresearch-venv" in dockerfile
    assert "PATH=/opt/autoresearch-venv/bin:${PATH}" in dockerfile
    assert "uv venv /opt/autoresearch-venv" in dockerfile
    assert "uv pip install --python /opt/autoresearch-venv/bin/python" in dockerfile
    assert "COPY --from=codex-cli /usr/local/bin/node /usr/local/bin/node" in dockerfile
    assert "COPY --from=codex-cli /usr/local/lib/node_modules /usr/local/lib/node_modules" in dockerfile
    assert "COPY tools/__init__.py ./tools/" in dockerfile
    assert "COPY tools/auto_research_issue_branch.py ./tools/" in dockerfile
    assert "COPY agent_orchestration/executor ./agent_orchestration/executor" in dockerfile
    assert "COPY . ." not in dockerfile
    assert "COPY autoresearch" not in dockerfile
    assert "COPY src" not in dockerfile
    assert ".env" not in dockerfile
    assert "auth.json" not in dockerfile
    assert "addgroup --gid 10001 appuser" in dockerfile
    assert "adduser --uid 10001 --gid 10001" in dockerfile
    assert "USER appuser" in dockerfile


def test_executor_release_verification_runs_phase2_toolchain_and_entrypoints() -> None:
    """Release가 immutable executor digest에서 실제 Stage 6 runtime을 점검한다.

    이 테스트가 잡는 변경: digest는 발행하지만 Git·uv·Node·Codex 또는 Stage 6
    module import를 검증하지 않아 배포 후에만 executor 실패가 드러나는 회귀.
    """
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index("  publish-agent-orchestration-executor-image:")
    end = workflow.index("  promote-airflow-digest:", start)
    executor_job = workflow[start:end]

    for command in ("git --version", "uv --version", "node --version", "codex --version"):
        assert command in executor_job
    for module in (
        "agent_orchestration.executor.main",
        "agent_orchestration.executor.token_minter",
        "agent_orchestration.executor.workspace",
        "agent_orchestration.executor.codex_worker",
        "agent_orchestration.executor.verifier",
        "agent_orchestration.executor.finalizer",
        "agent_orchestration.executor.phase2",
    ):
        assert module in executor_job


def _load_ci_workflow() -> dict[str, object]:
    parsed = yaml.load(CI_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(parsed, dict)
    return parsed


def test_pr_ci_builds_and_smokes_the_executor_image_contract() -> None:
    """PR CI가 release 전에 executor runtime·sealed parser를 실제로 검증한다.

    이 테스트가 잡는 변경: agent orchestration 경로의 PR에서 executor Dockerfile이
    build되지 않거나 runtime clone의 tools mount가 image 봉인 parser를 가리는 회귀.
    """
    workflow = _load_ci_workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["docker-build-agent-orchestration"]
    assert isinstance(job, dict)
    assert job["needs"] == "changes"
    assert job["if"] == "needs.changes.outputs.agent_orchestration == 'true'"

    steps = job["steps"]
    assert isinstance(steps, list)
    build_step = next(step for step in steps if step["name"].startswith("Build Agent"))
    smoke_step = next(step for step in steps if step["name"].startswith("Run Agent"))
    build_script = build_step["run"]
    smoke_script = smoke_step["run"]
    assert isinstance(build_script, str)
    assert isinstance(smoke_script, str)

    assert "deploy/agent_orchestration/executor.Dockerfile" in build_script
    assert "autoresearch-agent-orchestration-executor:ci" in build_script
    assert "--read-only" in smoke_script
    assert 'test "$(id -u)" = "10001"' in smoke_script
    assert 'test "$(id -g)" = "10001"' in smoke_script
    assert 'test "$UV_PROJECT_ENVIRONMENT" = "/opt/autoresearch-venv"' in smoke_script
    for command in ("git --version", "uv --version", "node --version", "codex --version"):
        assert command in smoke_script
    for module in (
        "agent_orchestration.executor.main",
        "agent_orchestration.executor.token_minter",
        "agent_orchestration.executor.workspace",
        "agent_orchestration.executor.codex_worker",
        "agent_orchestration.executor.verifier",
        "agent_orchestration.executor.finalizer",
        "agent_orchestration.executor.phase2",
    ):
        assert module in smoke_script
    assert "/tmp/executor-runtime-clone/tools" in smoke_script
    assert "/workspace/repository/tools:ro" in smoke_script
    assert 'tools.__file__ == \\"/app/tools/__init__.py\\"' in smoke_script


def _load_release_workflow() -> dict[str, object]:
    parsed = yaml.load(
        RELEASE_WORKFLOW.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(parsed, dict)
    return parsed


@pytest.mark.parametrize(
    ("job_name", "dockerfile", "image_name", "import_modules"),
    (
        (
            "publish-agent-orchestration-launcher-image",
            "deploy/agent_orchestration/launcher.Dockerfile",
            "autoresearch-agent-orchestration-launcher",
            ("agent_orchestration.launcher.main",),
        ),
        (
            "publish-agent-orchestration-executor-image",
            "deploy/agent_orchestration/executor.Dockerfile",
            "autoresearch-agent-orchestration-executor",
            (
                "agent_orchestration.executor.main",
                "agent_orchestration.executor.token_minter",
            ),
        ),
    ),
)
def test_release_workflow_publishes_branch_job_runtime_digests(
    job_name: str,
    dockerfile: str,
    image_name: str,
    import_modules: tuple[str, ...],
) -> None:
    """Release의 역할별 job이 독립 image를 push하고 digest·module을 검증한다."""
    workflow = _load_release_workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert job_name in jobs

    job = jobs[job_name]
    assert isinstance(job, dict)
    assert job["needs"] == "publish-application-image"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert job["outputs"] == {
        "digest_ref": "${{ steps.verify.outputs.digest_ref }}",
        "source_sha": "${{ steps.source.outputs.sha }}",
    }

    steps = job["steps"]
    assert isinstance(steps, list)
    checkout = next(step for step in steps if step.get("uses") == "actions/checkout@v6")
    assert checkout["with"]["ref"] == (
        "${{ needs.publish-application-image.outputs.source_sha }}"
    )

    image_step = next(step for step in steps if step.get("id") == "image")
    image_script = image_step["run"]
    assert isinstance(image_script, str)
    assert f"/{image_name}" in image_script
    assert 'sha_ref="${image_uri}:sha-${SOURCE_SHA}"' in image_script

    build_step = next(
        step for step in steps if step.get("uses") == "docker/build-push-action@v6"
    )
    assert build_step["with"] == {
        "context": ".",
        "file": dockerfile,
        "push": "true",
        "build-args": "VCS_REF=${{ steps.source.outputs.sha }}\n",
        "tags": "${{ steps.image.outputs.tags }}",
    }

    verify_step = next(step for step in steps if step.get("id") == "verify")
    assert verify_step["env"] == {
        "DIGEST": "${{ steps.build.outputs.digest }}",
        "IMAGE_URI": "${{ steps.image.outputs.uri }}",
        "SOURCE_SHA": "${{ steps.source.outputs.sha }}",
    }
    verify_script = verify_step["run"]
    assert isinstance(verify_script, str)
    assert "^sha256:[0-9a-f]{64}$" in verify_script
    assert 'digest_ref="${IMAGE_URI}@${DIGEST}"' in verify_script
    assert "org.opencontainers.image.revision" in verify_script
    assert "image_user" in verify_script
    assert 'echo "digest_ref=$digest_ref" >> "$GITHUB_OUTPUT"' in verify_script
    for module in import_modules:
        assert module in verify_script
