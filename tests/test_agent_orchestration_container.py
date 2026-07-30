"""Agent Orchestration API·Runner 이미지 경계 계약."""

import ast
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
API_DOCKERFILE = REPOSITORY_ROOT / "deploy" / "agent_orchestration" / "api.Dockerfile"
RUNNER_DOCKERFILE = (
    REPOSITORY_ROOT / "deploy" / "agent_orchestration" / "runner.Dockerfile"
)
DOCKERIGNORE = REPOSITORY_ROOT / ".dockerignore"
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


def test_orchestration_images_install_only_runtime_group_and_run_as_fixed_user() -> None:
    """두 역할 이미지는 같은 최소 Python 의존성과 비루트 UID/GID를 사용한다."""
    for dockerfile_path in (API_DOCKERFILE, RUNNER_DOCKERFILE):
        dockerfile = dockerfile_path.read_text(encoding="utf-8")

        assert "FROM ghcr.io/astral-sh/uv:0.11.26 AS lock-export" in dockerfile
        assert '"--no-dev", "--group", "orchestration"' in dockerfile
        assert "addgroup --gid 10001 appuser" in dockerfile
        assert "adduser --uid 10001 --gid 10001" in dockerfile
        assert "USER appuser" in dockerfile


def test_orchestration_images_do_not_embed_runtime_secrets() -> None:
    """빌드 문맥의 인증·환경·DB 값을 이미지 명령으로 반입하지 않는다."""
    forbidden_values = ("auth.json", ".env", "DATABASE_URL", "ORCH_DATABASE_URL")

    for dockerfile_path in (API_DOCKERFILE, RUNNER_DOCKERFILE):
        dockerfile = dockerfile_path.read_text(encoding="utf-8")
        assert not any(value in dockerfile for value in forbidden_values)

    dockerignore = DOCKERIGNORE.read_text(encoding="utf-8")
    assert ".codex" in dockerignore
    assert ".env" in dockerignore
    assert "auth.json" in dockerignore


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


def test_ci_builds_and_smokes_both_orchestration_images() -> None:
    """CI가 분리된 이미지의 격리·import·Codex CLI 설치를 검증한다."""
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "-f deploy/agent_orchestration/api.Dockerfile" in workflow
    assert "--tag autoresearch-agent-orchestration-api:ci" in workflow
    assert 'import agent_orchestration.app.main' in workflow
    assert "! command -v codex" in workflow
    assert "! command -v node" in workflow
    assert 'test ! -e "${CODEX_HOME:-/var/lib/codex}/auth.json"' in workflow
    assert "test ! -e /var/lib/codex/auth.json" in workflow
    assert "-f deploy/agent_orchestration/runner.Dockerfile" in workflow
    assert "--tag autoresearch-agent-orchestration-runner:ci" in workflow
    assert 'codex_version="$(docker run --rm autoresearch-agent-orchestration-runner:ci codex --version)"' in workflow
    assert 'test "${codex_version}" = "codex-cli 0.146.0"' in workflow
    assert 'import agent_orchestration.runner.app' in workflow
