from pathlib import Path

import yaml


WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml"
)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
APPLICATION_DOCKERFILE = REPOSITORY_ROOT / "Dockerfile.app"


def _load_workflow() -> dict:
    return yaml.load(WORKFLOW_PATH.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_release_workflow_publishes_application_image_directly():
    workflow = _load_workflow()
    triggers = workflow["on"]
    job = workflow["jobs"]["publish-application-image"]
    steps = job["steps"]

    assert "release" in triggers
    assert triggers["workflow_dispatch"]["inputs"]["source_sha"]["required"] == "true"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}

    build_step = next(
        step for step in steps if step.get("uses") == "docker/build-push-action@v6"
    )
    assert build_step["with"]["context"] == "."
    assert build_step["with"]["file"] == "Dockerfile.app"
    assert build_step["with"]["push"] == "true"
    assert "VCS_REF=${{ steps.source.outputs.sha }}" in build_step["with"][
        "build-args"
    ]


def test_release_workflow_is_independent_from_airflow_repository():
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "Autoresearch-airflow" not in workflow_text
    assert "repository_dispatch" not in workflow_text
    assert "actions/create-github-app-token" not in workflow_text


def test_release_workflow_verifies_all_public_batch_commands():
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")

    for module in (
        "autoresearch.jobs.youtube_trending",
        "autoresearch.jobs.action_log",
        "autoresearch.jobs.action_log_quality",
    ):
        assert module in workflow_text
    assert "org.opencontainers.image.revision" in workflow_text
    assert ".application_revision" in workflow_text
    assert ".contract_version" in workflow_text


def test_github_wif_credentials_are_excluded_from_repository_and_build_context():
    for ignore_file in (".gitignore", ".dockerignore"):
        ignore_rules = (REPOSITORY_ROOT / ignore_file).read_text(encoding="utf-8")
        assert "gha-creds-*.json" in ignore_rules


def test_application_image_uses_the_locked_dependency_source():
    dockerfile = APPLICATION_DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert '"/uv", "export", "--frozen", "--no-dev", "--no-hashes"' in dockerfile
    assert "COPY requirements.txt" not in dockerfile


def test_repository_does_not_keep_legacy_airflow_runtime_surface():
    legacy_files = (
        "airflow_settings.yaml",
        "Dockerfile",
        "packages.txt",
        "requirements.txt",
    )

    assert not list((REPOSITORY_ROOT / "dags").rglob("*.py"))
    assert all(not (REPOSITORY_ROOT / path).exists() for path in legacy_files)
