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


@pytest.mark.parametrize(
    ("dockerfile_path", "runtime_package", "command_module"),
    (
        (
            LAUNCHER_DOCKERFILE,
            "COPY agent_orchestration/launcher ./agent_orchestration/launcher",
            "agent_orchestration.launcher.main",
        ),
        (
            EXECUTOR_DOCKERFILE,
            "COPY agent_orchestration/executor ./agent_orchestration/executor",
            "agent_orchestration.executor.main",
        ),
    ),
)
def test_branch_job_images_are_locked_non_root_runtime_images(
    dockerfile_path: Path,
    runtime_package: str,
    command_module: str,
) -> None:
    """Branch Job 이미지는 고정 의존성·비루트 실행과 역할별 command를 제공한다."""
    assert dockerfile_path.is_file()
    dockerfile = dockerfile_path.read_text(encoding="utf-8")

    assert "FROM ghcr.io/astral-sh/uv:0.11.26 AS lock-export" in dockerfile
    assert '"--only-group", "orchestration"' in dockerfile
    assert "addgroup --gid 10001 appuser" in dockerfile
    assert "adduser --uid 10001 --gid 10001" in dockerfile
    assert "USER appuser" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert runtime_package in dockerfile
    assert f'CMD ["python", "-m", "{command_module}"]' in dockerfile
    assert "ARG VCS_REF=unknown" in dockerfile
    assert 'org.opencontainers.image.revision="${VCS_REF}"' in dockerfile
    assert "@openai/codex" not in dockerfile
    assert "node:" not in dockerfile


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
