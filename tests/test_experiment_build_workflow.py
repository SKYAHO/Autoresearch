"""실험 이미지 워크플로우와 Python 판정 계약의 일치를 검증한다.

[파이프라인] ②candidate Job을 만들기 직전 — GitHub Actions 러너에서 의존성 diff를
판단하고 코드 아카이브를 올리며 필요할 때만 실험 이미지를 굽는 구간의 계약을 검증한다.

[기능] run-name·job 이름·diff 대상 경로·태그 네임스페이스처럼 러너에서만 드러나는
계약을 워크플로우 텍스트에서 직접 꺼내 Python 상수와 대조한다.

[비책임] 실제 러너 실행(diff 판정·GCS 업로드·GAR push)과 판정 로직 자체는 각각 실제
워크플로우 실행과 ``tests/test_experiment_build.py``의 검증 범위다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from agent_orchestration.experiment_build.service import (
    BUILD_JOB_NAME,
    DECIDE_JOB_NAME,
    RUN_NAME_PREFIX,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github" / "workflows" / "experiment-image.yml"

DIFF_PATHS = (
    "pyproject.toml",
    "uv.lock",
    "Dockerfile.feast",
    "scripts/gcs_code_bootstrap.sh",
)


def _workflow_text() -> str:
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def _workflow() -> dict:
    return yaml.safe_load(_workflow_text())


def _steps(job_id: str) -> list[dict]:
    return _workflow()["jobs"][job_id]["steps"]


def _step_index(job_id: str, name_fragment: str) -> int:
    """이름에 조각이 들어간 첫 스텝의 위치를 돌려준다."""
    for index, step in enumerate(_steps(job_id)):
        if name_fragment in step.get("name", ""):
            return index
    raise AssertionError(f"{job_id}에 '{name_fragment}' 스텝이 없습니다")


def _uses_index(job_id: str, uses_prefix: str) -> int:
    """`uses:`가 접두사로 시작하는 첫 스텝의 위치를 돌려준다."""
    for index, step in enumerate(_steps(job_id)):
        if step.get("uses", "").startswith(uses_prefix):
            return index
    raise AssertionError(f"{job_id}에 '{uses_prefix}' 스텝이 없습니다")


def _step_run(job_id: str, name_fragment: str) -> str:
    return _steps(job_id)[_step_index(job_id, name_fragment)]["run"]


def _diff_target_paths(diff: str) -> list[str]:
    """`git diff --quiet ... -- <paths>` 스텝에서 `--` 뒤 경로 토큰만 뽑아낸다.

    쉘 라인 연속(`\\` + 줄바꿈)을 먼저 이어붙여 하나의 논리 라인으로 만든 뒤,
    `git diff`가 있는 라인에서 pathspec 구분자인 독립 토큰 `--` 이후만 취한다.
    """
    joined = diff.replace("\\\n", " ")
    for line in joined.splitlines():
        tokens = line.split()
        if "git" in tokens and "diff" in tokens and "--" in tokens:
            separator_index = tokens.index("--")
            return tokens[separator_index + 1 :]
    raise AssertionError("git diff 스텝에서 '--' 뒤의 경로 목록을 찾지 못했습니다")


def test_run_name_matches_the_python_lookup_key() -> None:
    assert _workflow()["run-name"] == (
        RUN_NAME_PREFIX + "${{ inputs.candidate_sha }}"
    )


def test_job_names_are_pinned_to_their_ids() -> None:
    jobs = _workflow()["jobs"]

    assert set(jobs) == {DECIDE_JOB_NAME, BUILD_JOB_NAME}
    for job_id, job in jobs.items():
        assert job["name"] == job_id


def test_workflow_is_dispatch_only_with_both_shas_required() -> None:
    triggers = _workflow()[True]

    assert set(triggers) == {"workflow_dispatch"}
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"base_dev_sha", "candidate_sha"}
    for definition in inputs.values():
        assert definition["required"] is True
        assert definition["type"] == "string"


def test_concurrency_is_scoped_per_candidate_and_never_cancels() -> None:
    concurrency = _workflow()["concurrency"]

    assert concurrency["group"] == "experiment-image-${{ inputs.candidate_sha }}"
    assert concurrency["cancel-in-progress"] is False


def test_dev_and_exp_refs_are_fetched_explicitly() -> None:
    fetch = _step_run(DECIDE_JOB_NAME, "remote-tracking refs")

    assert "+refs/heads/dev:refs/remotes/origin/dev" in fetch
    assert "+refs/heads/exp/*:refs/remotes/origin/exp/*" in fetch
    assert "+refs/heads/main:refs/remotes/origin/main" in fetch


@pytest.mark.parametrize("job_id", [DECIDE_JOB_NAME, BUILD_JOB_NAME])
def test_trusted_files_are_pinned_from_main_in_every_job(job_id: str) -> None:
    """candidate가 쓴 prod 격리 장치가 러너에서 실행되지 않도록 main 판으로 되돌린다."""
    pin = _step_run(job_id, "Pin trusted files from main")

    assert "git checkout origin/main --" in pin
    for path in ("scripts/upload_code_archive.sh", ".github/actions", ".dockerignore"):
        assert path in pin
    # 이 기능 자체가 candidate의 `Dockerfile.feast` 변경을 굽는 것이므로 고정 대상이 아니다.
    assert "Dockerfile.feast" not in pin


@pytest.mark.parametrize("job_id", [DECIDE_JOB_NAME, BUILD_JOB_NAME])
def test_trusted_pin_runs_before_any_credential_reaches_the_runner(job_id: str) -> None:
    """고정이 실패하면 WIF 자격증명이 러너에 올라오기 전에 job이 끝나야 한다."""
    pin_index = _step_index(job_id, "Pin trusted files from main")
    checkout_index = _uses_index(job_id, "actions/checkout")
    auth_index = _uses_index(job_id, "google-github-actions/auth")
    local_action_index = _uses_index(job_id, "./.github/actions/")

    assert checkout_index < pin_index
    assert pin_index < auth_index
    assert pin_index < local_action_index


def test_build_job_fetches_main_before_pinning_from_its_shallow_clone() -> None:
    """빌드 job의 checkout은 `fetch-depth: 1`이라 main ref를 이 job이 직접 받아야 한다."""
    checkout = _steps(BUILD_JOB_NAME)[_uses_index(BUILD_JOB_NAME, "actions/checkout")]
    pin = _step_run(BUILD_JOB_NAME, "Pin trusted files from main")

    assert checkout["with"]["fetch-depth"] == 1
    assert "git fetch" in pin
    assert "+refs/heads/main:refs/remotes/origin/main" in pin
    assert pin.index("git fetch") < pin.index("git checkout origin/main")


def test_provenance_guard_checks_dev_ancestry_and_exp_reachability() -> None:
    guard = _step_run(DECIDE_JOB_NAME, "provenance")

    assert 'merge-base --is-ancestor "$BASE_DEV_SHA" origin/dev' in guard
    assert 'git branch -r --contains "$CANDIDATE_SHA"' in guard
    assert "origin/exp/" in guard


def test_diff_compares_exactly_the_baked_paths() -> None:
    diff = _step_run(DECIDE_JOB_NAME, "rebuilt")

    assert "git diff --quiet" in diff
    assert set(_diff_target_paths(diff)) == set(DIFF_PATHS)


def test_diff_treats_unexpected_exit_status_as_failure() -> None:
    diff = _step_run(DECIDE_JOB_NAME, "rebuilt")

    assert "0) changed=false" in diff
    assert "1) changed=true" in diff
    assert "*)" in diff
    assert "exit 1" in diff


def test_code_archive_upload_never_updates_latest() -> None:
    assert "--update-latest" not in _workflow_text()
    upload = _step_run(DECIDE_JOB_NAME, "code archive")
    assert "scripts/upload_code_archive.sh" in upload


def test_decide_job_publishes_the_dependency_decision() -> None:
    outputs = _workflow()["jobs"][DECIDE_JOB_NAME]["outputs"]

    assert outputs["dependencies_changed"] == (
        "${{ steps.diff.outputs.dependencies_changed }}"
    )


def test_build_job_runs_only_when_dependencies_changed() -> None:
    job = _workflow()["jobs"][BUILD_JOB_NAME]

    assert job["needs"] == DECIDE_JOB_NAME
    assert job["if"] == (
        f"needs.{DECIDE_JOB_NAME}.outputs.dependencies_changed == 'true'"
    )


def test_build_job_uses_the_feast_dockerfile() -> None:
    build = next(
        step
        for step in _steps(BUILD_JOB_NAME)
        if step.get("uses", "").startswith("docker/build-push-action")
    )

    assert build["with"]["file"] == "Dockerfile.feast"
    assert build["with"]["push"] is True
    assert "VCS_REF=${{ inputs.candidate_sha }}" in build["with"]["build-args"]


def test_experiment_tag_never_collides_with_the_prod_namespace() -> None:
    text = _workflow_text()

    assert "exp-${CANDIDATE_SHA}" in text
    # prod 릴리스 네임스페이스는 `sha-<sha>`다. 이 워크플로우에는 그 접두사로 태그를
    # 조립하는 표현이 어떤 형태로도 나타나면 안 된다.
    assert re.search(r":sha-", text) is None


def test_existing_tag_is_never_overwritten() -> None:
    guard = _step_run(BUILD_JOB_NAME, "Refuse to overwrite")

    assert "gcloud artifacts docker images list" in guard
    assert "--include-tags" in guard
    assert "tags:exp-${CANDIDATE_SHA}" in guard
    assert "exists=true" in guard
    assert "exists=false" in guard

    for step in _steps(BUILD_JOB_NAME):
        if step.get("uses", "").startswith("docker/build-push-action"):
            assert step["if"] == "steps.existing.outputs.exists == 'false'"


def test_existing_tag_check_is_decided_by_exit_code_not_by_gcloud_prose() -> None:
    """태그 유무 판별이 gcloud 오류 문구 매칭에 기대면 안 된다.

    `describe`의 실패 문구는 gcloud 판마다 다르고(`Image not found`, `NOT_FOUND`,
    `Failed to describe image` …), 매칭이 어긋나면 신규 candidate의 첫 빌드가 전부
    실패한다. 종료 코드와 출력 유무만으로 판별하도록 고정한다.
    """
    guard = _step_run(BUILD_JOB_NAME, "Refuse to overwrite")

    assert "gcloud artifacts docker images describe" not in guard
    assert "not found" not in guard.lower()
    assert "grep" not in guard

    # 종료 코드를 읽으려면 errexit를 잠시 꺼야 한다.
    assert "set +e" in guard
    assert "set -e" in guard
    assert "list_status=$?" in guard


def test_existing_tag_check_fails_closed_on_indeterminate_gcloud_error() -> None:
    """gcloud 자체가 실패하면(만료된 WIF 자격증명·권한 거부·네트워크 오류) exists를

    쓰지 않고 스텝을 실패시켜야 한다 (fail-closed). 판별 불가를 exists=false로
    흘리는 2분기 fail-open 형태로 되돌리면 이 테스트가 깨진다.
    """
    guard = _step_run(BUILD_JOB_NAME, "Refuse to overwrite")

    assert "elif" in guard
    assert "::error::" in guard
    assert "exit 1" in guard

    # 세 갈래여야 한다: 판별 불가(exit 1) / 태그 있음 / 태그 없음.
    assert guard.count("exists=true") == 1
    assert guard.count("exists=false") == 1

    status_index = guard.index('"$list_status" -ne 0')
    error_index = guard.index("::error::")
    exit_index = guard.index("exit 1")
    exists_true_index = guard.index("exists=true")
    exists_false_index = guard.index("exists=false")

    # 판별 불가 분기가 맨 앞에서 exists 출력 없이 실패하고, 나머지 두 분기만
    # exists를 남긴다.
    assert status_index < error_index < exit_index
    assert exit_index < exists_true_index < exists_false_index


def test_existing_tag_check_keeps_the_reuse_notice() -> None:
    guard = _step_run(BUILD_JOB_NAME, "Refuse to overwrite")

    assert "::notice::$IMAGE_TAG already exists; reusing it without rebuilding" in guard


def test_build_job_pushes_with_the_gar_pusher_identity() -> None:
    auth = next(
        step
        for step in _steps(BUILD_JOB_NAME)
        if step.get("uses", "").startswith("google-github-actions/auth")
    )

    assert auth["with"]["service_account"] == "${{ secrets.GAR_PUSHER_SA }}"


def test_no_promotion_job_can_leak_the_experiment_image_to_prod() -> None:
    text = _workflow_text()

    assert "promote" not in text
    assert "Autoresearch-airflow" not in text
    assert "values.yaml" not in text


def test_image_verification_matches_the_release_feast_contract() -> None:
    verify = _step_run(BUILD_JOB_NAME, "Verify experiment image")

    assert "sha256:[0-9a-f]{64}" in verify
    assert "org.opencontainers.image.revision" in verify
    assert "must run as a non-root user" in verify
    for module in (
        "feast",
        "pyarrow",
        "lightgbm",
        "onnxmltools",
        "onnxruntime",
        "joblib",
        "mlflow",
    ):
        assert module in verify
