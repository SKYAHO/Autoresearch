from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SERVING_DOCKERFILE = REPOSITORY_ROOT / "deploy" / "serving" / "Dockerfile"
CI_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"


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
