import io
import subprocess
import tarfile
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


def test_release_workflow_requires_main_ancestor_for_source_sha():
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert (
        workflow_text.count(
            "git fetch --no-tags origin main:refs/remotes/origin/main"
        )
        == 4
    )
    assert workflow_text.count('git merge-base --is-ancestor "$source_sha" origin/main') == 3


def test_release_workflow_publishes_serving_image_with_immutable_verification():
    workflow = _load_workflow()
    job = workflow["jobs"]["publish-serving-image"]
    steps = job["steps"]

    assert job["needs"] == "publish-application-image"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    assert job["outputs"]["digest_ref"] == "${{ steps.verify.outputs.digest_ref }}"

    checkout = next(step for step in steps if step.get("uses") == "actions/checkout@v6")
    assert (
        checkout["with"]["ref"]
        == "${{ needs.publish-application-image.outputs.source_sha }}"
    )

    build_step = next(
        step for step in steps if step.get("uses") == "docker/build-push-action@v6"
    )
    assert build_step["with"]["context"] == "."
    assert build_step["with"]["file"] == "deploy/serving/Dockerfile"
    assert build_step["with"]["push"] == "true"
    assert "VCS_REF=${{ steps.source.outputs.sha }}" in build_step["with"][
        "build-args"
    ]

    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "autoresearch-serving" in workflow_text
    assert "^sha256:[0-9a-f]{64}$" in workflow_text
    assert "feature_repo.redis_iam, src.serving.app" in workflow_text
    assert "Serving digest_ref" in workflow_text
    assert "$GITHUB_STEP_SUMMARY" in workflow_text


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
        "autoresearch.jobs.youtube_backfill",
        "autoresearch.jobs.action_log",
        "autoresearch.jobs.action_log_quality",
        "autoresearch.jobs.feature_store_build",
        "autoresearch.recommendation.daily_recommendations",
    ):
        assert module in workflow_text
    assert "org.opencontainers.image.revision" in workflow_text
    assert ".application_revision" in workflow_text
    assert ".contract_version" in workflow_text


def test_release_workflow_injects_the_code_archive_before_verifying_commands():
    # #750: 이미지가 코드를 담지 않으므로, 계약 검증은 아카이브를 주입해
    # 실행해야 한다. 주입 없이 검증하면 부트스트랩이 exit 2로 죽는다.
    job = _load_workflow()["jobs"]["publish-application-image"]
    verify_step = next(step for step in job["steps"] if step.get("id") == "verify")
    script = verify_step["run"]

    assert "git archive" in script
    assert "CODE_ARCHIVE_LOCAL_PATH" in script


def test_release_workflow_still_opens_the_batch_digest_promotion_pr():
    # #750 결정 2: digest 정본은 Git(values.yaml)에 남긴다. 이 테스트가
    # 깨지면 전환이 범위를 벗어난 것이다.
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "promote_batch_image.py" in workflow_text
    assert "deploy/airflow/values.yaml" in workflow_text


def test_application_image_does_not_bake_source_code():
    # #750: 코드는 이미지가 아니라 GCS 아카이브로 배포한다. 소스를 구우면
    # 코드 한 줄 변경이 이미지 재빌드·digest 승격을 다시 요구한다.
    dockerfile = APPLICATION_DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY autoresearch" not in dockerfile
    assert "COPY src" not in dockerfile


def test_application_image_bootstraps_code_from_the_gcs_archive():
    # Dockerfile.feast·Dockerfile.train과 같은 부트스트랩 계약을 따른다.
    dockerfile = APPLICATION_DOCKERFILE.read_text(encoding="utf-8")

    assert (
        "COPY scripts/gcs_code_bootstrap.sh /usr/local/bin/gcs_code_bootstrap.sh"
        in dockerfile
    )
    assert 'ENTRYPOINT ["/usr/local/bin/gcs_code_bootstrap.sh"]' in dockerfile
    # 부트스트랩이 아카이브를 /app에 푼 뒤 python -m이 그것을 찾아야 한다.
    # feast·train은 WORKDIR에 의존하지만 배치 이미지는 명시한다(#750 spec).
    assert "PYTHONPATH=/app" in dockerfile


def test_code_archive_carries_both_batch_command_packages():
    # batch-contract-v1 6개 중 다섯은 autoresearch.jobs.*이지만 하나는
    # src.pipeline.daily_recommendations다. 이미지가 코드를 담지 않으므로
    # 두 패키지가 모두 아카이브에 들어가야 계약이 성립한다.
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        check=True,
    ).stdout

    with tarfile.open(fileobj=io.BytesIO(archive)) as archived:
        names = set(archived.getnames())

    assert "autoresearch/jobs/youtube_trending.py" in names
    assert "autoresearch/recommendation/daily_recommendations.py" in names


def test_application_image_installs_lightgbm_openmp_runtime():
    # lightgbm 모델 로드가 libgomp.so.1을 dlopen한다 — python:3.12-slim에는 없다.
    dockerfile = APPLICATION_DOCKERFILE.read_text(encoding="utf-8")
    assert "libgomp1" in dockerfile


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


def test_release_workflow_promotes_every_orchestration_image_including_executor():
    """executor를 빼면 실험을 실제로 실행하는 이미지만 옛 digest로 남는다.

    launcher는 Job을 조립해 던지고 학습·Codex·검증·채점·API 보고는 executor가 한다.
    자동 승격 로그를 보고 "배포됐다"고 판단하는데 실험은 옛 이미지로 도는 상태가
    조용히 성립하는 것을 막는다.
    """
    workflow = _load_workflow()
    job = workflow["jobs"]["promote-agent-orchestration-digests"]

    assert set(job["needs"]) == {
        "publish-agent-orchestration-api-image",
        "publish-agent-orchestration-executor-image",
        "publish-agent-orchestration-launcher-image",
        "publish-agent-orchestration-runner-image",
        "publish-agent-orchestration-ui-image",
    }

    promote_step = next(
        step for step in job["steps"] if "promote-agent-orchestration-digests.rb" in step.get("run", "")
    )
    assert promote_step["env"]["EXECUTOR_DIGEST_REF"] == (
        "${{ needs.publish-agent-orchestration-executor-image.outputs.digest_ref }}"
    )
    # 섞인 릴리스를 fail-closed로 막는 검사다. digest만 넘기고 SHA 비교에서 빼면
    # 다른 커밋에서 나온 executor가 조용히 승격된다.
    assert '"$API_SOURCE_SHA" != "$EXECUTOR_SOURCE_SHA"' in promote_step["run"]

    checkout = next(
        step for step in job["steps"] if step.get("uses") == "actions/checkout@v6"
    )
    assert checkout["with"]["repository"] == "SKYAHO/Autoresearch-infra"
    assert checkout["with"]["ref"] == "main"


def test_release_workflow_limits_orchestration_promotion_to_approved_manifests():
    """executor digest는 launcher CronJob 안에 있다 — 허용 경로가 그대로여야 한다."""
    workflow = _load_workflow()
    job = workflow["jobs"]["promote-agent-orchestration-digests"]

    scope_step = next(
        step for step in job["steps"] if step.get("id") == "changed"
    )
    assert "deploy/agent-orchestration/launcher-cronjob.yaml" in scope_step["run"]

    summary_step = next(
        step for step in job["steps"] if "Executor digest" in step.get("run", "")
    )
    assert summary_step["env"]["EXECUTOR_DIGEST_REF"] == (
        "${{ needs.publish-agent-orchestration-executor-image.outputs.digest_ref }}"
    )
