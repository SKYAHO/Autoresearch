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


def test_release_workflow_opens_an_airflow_digest_promotion_pr():
    workflow = _load_workflow()
    job = workflow["jobs"]["promote-airflow-digest"]
    steps = job["steps"]

    assert job["needs"] == "publish-application-image"
    assert any(
        step.get("uses") == "actions/create-github-app-token@v2" for step in steps
    )
    checkout = next(
        step for step in steps if step.get("uses") == "actions/checkout@v6"
    )
    assert checkout["with"]["repository"] == "SKYAHO/Autoresearch-airflow"
    assert checkout["with"]["ref"] == "main"

    create_pr = next(
        step for step in steps if step.get("uses") == "peter-evans/create-pull-request@v8"
    )
    assert create_pr["with"]["base"] == "main"
    assert create_pr["with"]["add-paths"] == "deploy/airflow/values.yaml"
    assert create_pr["with"]["branch"].startswith("automation/batch-")

    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "repository_dispatch" not in workflow_text
    assert "scripts/promote_batch_image.py" in workflow_text


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

    assert "source=uv.lock,target=uv.lock" in dockerfile
    assert "source=pyproject.toml,target=pyproject.toml" in dockerfile
    assert "uv sync --locked --no-dev" in dockerfile
    assert "uv export" not in dockerfile
    assert "COPY requirements" not in dockerfile


def test_repository_does_not_keep_legacy_airflow_runtime_surface():
    legacy_files = (
        "airflow_settings.yaml",
        "Dockerfile",
        "packages.txt",
        "requirements.txt",
    )

    assert not list((REPOSITORY_ROOT / "dags").rglob("*.py"))
    assert all(not (REPOSITORY_ROOT / path).exists() for path in legacy_files)
