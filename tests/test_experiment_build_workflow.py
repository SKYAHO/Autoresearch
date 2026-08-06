"""실험 이미지 워크플로우와 Python 판정 계약의 일치를 검증한다.

[파이프라인] ②candidate Job을 만들기 직전 — GitHub Actions 러너에서 의존성 diff를
판단하고 코드 아카이브를 올리며 필요할 때만 실험 이미지를 굽는 구간의 계약을 검증한다.

[기능] run-name·job 이름·diff 대상 경로·태그 네임스페이스처럼 러너에서만 드러나는
계약을 워크플로우 텍스트에서 직접 꺼내 Python 상수와 대조한다.

[비책임] 실제 러너 실행(diff 판정·GCS 업로드·GAR push)과 판정 로직 자체는 각각 실제
워크플로우 실행과 ``tests/test_experiment_build.py``의 검증 범위다.
"""

from __future__ import annotations

from pathlib import Path

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


def _step_run(job_id: str, name_fragment: str) -> str:
    for step in _steps(job_id):
        if name_fragment in step.get("name", ""):
            return step["run"]
    raise AssertionError(f"{job_id}에 '{name_fragment}' 스텝이 없습니다")


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
