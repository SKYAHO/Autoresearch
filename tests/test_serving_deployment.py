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


def test_serving_image_copies_src_feature_repo_and_bootstrap_package() -> None:
    dockerfile = SERVING_DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY autoresearch ./autoresearch" in dockerfile
    assert "COPY feature_repo ./feature_repo" in dockerfile
    assert "COPY src ./src" in dockerfile


def test_ci_builds_serving_image_and_runs_import_smoke() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "-f deploy/serving/Dockerfile" in workflow
    assert "--tag autoresearch-serving:ci" in workflow
    assert "import feast, fastapi, feature_repo.redis_iam, src.serving.app" in workflow
    assert "tests/test_serving_feast_reader.py" in workflow
    assert "tests/test_serving_api.py" in workflow
    assert "tests/test_serving_deployment.py" in workflow


def test_ci_checks_serving_image_dependencies_and_feature_store_bootstrap() -> None:
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    assert "python -m pip check" in workflow
    assert "from feature_repo.bootstrap import load_feature_store" in workflow
    assert "load_feature_store('/app/feature_repo')" in workflow
