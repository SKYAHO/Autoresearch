from pathlib import Path
import tomllib


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SERVING_DOCKERFILE = REPOSITORY_ROOT / "deploy" / "serving" / "Dockerfile"
CI_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPOSITORY_ROOT / "pyproject.toml"


def test_feast_group_requires_sdk_compatible_pyarrow() -> None:
    with PYPROJECT.open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    feast_dependencies = pyproject["dependency-groups"]["feast"]
    assert "pyarrow>=21.0.0,<22" in feast_dependencies


def test_serving_image_installs_feast_compatible_group() -> None:
    dockerfile = SERVING_DOCKERFILE.read_text(encoding="utf-8")

    assert '"--no-dev", "--group", "feast"' in dockerfile
    assert '"--group", "serving"' not in dockerfile


def test_serving_runtime_installs_lightgbm_native_dependency() -> None:
    # Given: the production serving image definition.
    dockerfile = SERVING_DOCKERFILE.read_text(encoding="utf-8")

    # When: its runtime package installation is inspected.
    runtime_stage = dockerfile.split("FROM python:3.12-slim", maxsplit=1)[1]

    # Then: LightGBM's OpenMP library is installed before dropping privileges.
    assert "apt-get update" in runtime_stage
    assert "apt-get install --no-install-recommends -y libgomp1" in runtime_stage
    assert "rm -rf /var/lib/apt/lists/*" in runtime_stage
    assert runtime_stage.index("libgomp1") < runtime_stage.index("USER appuser")


def test_serving_image_copies_src_feature_repo_and_bootstrap_package() -> None:
    dockerfile = SERVING_DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY autoresearch ./autoresearch" in dockerfile
    assert "COPY feature_repo ./feature_repo" in dockerfile
    assert "COPY src ./src" in dockerfile


def test_serving_image_embeds_source_revision_and_runs_non_root() -> None:
    dockerfile = SERVING_DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG VCS_REF=unknown" in dockerfile
    assert 'LABEL org.opencontainers.image.revision="${VCS_REF}"' in dockerfile
    assert "USER appuser" in dockerfile


def test_ci_builds_serving_image_and_runs_import_smoke() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "-f deploy/serving/Dockerfile" in workflow
    assert "--tag autoresearch-serving:ci" in workflow
    assert (
        "import lightgbm, feast, fastapi, feature_repo.redis_iam, src.serving.app"
        in workflow
    )
    assert "tests/test_serving_feast_reader.py" in workflow
    assert "tests/test_serving_feast_reader_feast.py" in workflow
    assert "tests/test_serving_api.py" in workflow
    assert "tests/test_serving_deployment.py" in workflow


def test_ci_checks_serving_image_dependencies_and_feature_store_bootstrap() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "python -m pip check" in workflow
    assert "from feature_repo.bootstrap import load_feature_store" in workflow
    assert "load_feature_store('/app/feature_repo')" in workflow


def test_ci_smokes_serving_http_contract_while_unready_and_cleans_up() -> None:
    # Given: the production serving-image CI workflow.
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    # When: its detached-container smoke contract is inspected.
    # Then: request validation precedes readiness and health remains fail-closed.
    assert "Run serving image fail-closed HTTP contract smoke" in workflow
    assert "docker run --detach" in workflow
    assert "trap cleanup EXIT" in workflow
    assert "curl --request POST" in workflow
    assert "Content-Type: application/json" in workflow
    assert "--data '{}'" in workflow
    assert "/rerank" in workflow
    assert '"${rerank_status_code}" = "422"' in workflow
    assert "/healthcheck" in workflow
    assert '"${healthcheck_status_code}" = "503"' in workflow
