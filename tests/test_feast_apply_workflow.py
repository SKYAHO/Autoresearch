"""feast-apply 워크플로우·Job 매니페스트의 prod/dev 배선 검증(#399, #548).

bare ``feast`` CLI 는 ``feature_repo/bootstrap.py`` 를 거치지 않으므로, apply Job 은
``FEAST_ONLINE_FULL_SCAN_FOR_DELETION`` 을 워크플로우가 직접 주입받는다. 그 파생
규칙(prod=true, dev=false)이 워크플로우 bash 에 **손으로 복제**돼 있어
``feature_repo/env.py`` 와 조용히 어긋날 수 있다. 여기서는 워크플로우의 실제
bash 조각을 꺼내 실행해 env.py 와 같은 값을 내는지 대조한다.

같은 이유로 #548 의 두 회귀도 여기서 고정한다 — 잡이 뜨는 GitHub Environment
이름과, 코드 아카이브 대기 루프가 "객체 없음"과 "인증·권한 실패"를 구분하는지다.
둘 다 러너에서만 드러나는 실패라 워크플로우 텍스트를 꺼내 실행해야만 잡힌다.

feast 의존이 없어 기본 pytest 그룹에서 실행된다.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml

from feature_repo.env import ENV_DEV, ENV_PROD, online_full_scan_for_deletion

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
APPLY_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "feast-apply.yml"
CODE_ARCHIVE_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "code-archive.yml"
APPLY_JOB_MANIFEST = REPOSITORY_ROOT / "deploy" / "feast" / "apply-job.yaml"

_DERIVATION_START = 'if [[ "$AUTORESEARCH_ENV" == "dev" ]]'
_ARCHIVE_WAIT_STEP = "Wait for the code archive of this commit"

# `environment:` 키는 env 컨텍스트를 읽지 못해 같은 식을 잡 수준 env 에도 한 번 더
# 적어야 한다. 두 사본이 어긋나면 Job 매니페스트의 AUTORESEARCH_ENV 와 실제 잡이
# 뜬 Environment 가 달라지므로, 문자열 동일성을 계약으로 고정한다.
ENVIRONMENT_EXPRESSION = (
    "${{ github.event_name == 'workflow_dispatch' && inputs.environment "
    "|| (github.ref_name == 'main' && 'prod' || 'dev') }}"
)


def _workflow_text() -> str:
    return APPLY_WORKFLOW.read_text(encoding="utf-8")


def _apply_job() -> dict:
    workflow = yaml.safe_load(_workflow_text())
    return workflow["jobs"]["feast-apply"]


def _step_script(step_name: str) -> str:
    for step in _apply_job()["steps"]:
        if step.get("name") == step_name:
            return step["run"]
    raise AssertionError(f"step not found: {step_name}")


def _dispatch_environment_options() -> set[str]:
    """수동 dispatch 가 고를 수 있는 환경 이름 집합."""
    workflow = yaml.safe_load(_workflow_text())
    # YAML 1.1 은 따옴표 없는 `on:` 을 boolean True 로 읽는다.
    triggers = workflow[True]
    return set(triggers["workflow_dispatch"]["inputs"]["environment"]["options"])


def _extract_derivation_snippet(workflow: str) -> str:
    """렌더 스텝의 full_scan 파생 bash 블록만 잘라내 들여쓰기를 제거한다."""
    lines = workflow.splitlines()
    start = next(
        index for index, line in enumerate(lines) if _DERIVATION_START in line
    )
    end = next(
        index
        for index, line in enumerate(lines[start:], start=start)
        if line.strip() == "fi"
    )
    return "\n".join(line.strip() for line in lines[start : end + 1])


@pytest.mark.parametrize("environment", ["prod", "dev"])
def test_workflow_full_scan_derivation_matches_env_module(environment: str) -> None:
    # Given: 워크플로우가 Job 에 주입할 full_scan 값을 계산하는 실제 bash 블록.
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable")
    snippet = _extract_derivation_snippet(_workflow_text())

    # When: env.py 와 동일한 환경값으로 그 블록을 실행한다.
    completed = subprocess.run(
        [bash, "-c", f'{snippet}\nprintf "%s" "$FEAST_ONLINE_FULL_SCAN_FOR_DELETION"'],
        env={"AUTORESEARCH_ENV": environment, "PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=True,
    )

    # Then: 두 구현이 같은 결론에 도달해야 한다(드리프트 가드).
    expected = online_full_scan_for_deletion({"AUTORESEARCH_ENV": environment})
    assert completed.stdout == ("true" if expected else "false")


def test_workflow_routes_each_push_branch_to_its_own_environment() -> None:
    # main/dev push는 수동 입력이 없으므로 ref 에서 대상 환경을 골라야 한다. 다만
    # 고를 것은 브랜치 이름이 아니라 **Environment 이름**이다 — main 이라는
    # Environment 는 비어 있어서 좌표가 전부 저장소 수준으로 폴백된다(#548).
    workflow = _workflow_text()

    assert "branches: [main, dev]" in workflow
    assert ENVIRONMENT_EXPRESSION in workflow
    assert "|| github.ref_name }}" not in workflow


def test_environment_expression_only_yields_known_environment_names() -> None:
    # 표현식이 만들어낼 수 있는 값은 수동 dispatch 입력(prod|dev)과 아래 리터럴뿐이다.
    # 브랜치 이름이 그대로 새어 나가면 GitHub Environment 도, env.py 의
    # resolve_environment 도 함께 깨진다.
    dispatch_options = _dispatch_environment_options()
    literals = set(re.findall(r"'([^']*)'", ENVIRONMENT_EXPRESSION))
    produced = (literals - {"workflow_dispatch", "main"}) | dispatch_options

    assert produced == {ENV_PROD, ENV_DEV}


def test_job_environment_and_autoresearch_env_stay_in_sync() -> None:
    # `environment:` 는 env 컨텍스트를 읽지 못해 같은 식이 두 벌 존재한다. 한쪽만
    # 고치면 잡이 뜬 Environment 와 Job 에 주입되는 AUTORESEARCH_ENV 가 어긋난다.
    job = _apply_job()

    assert job["environment"] == ENVIRONMENT_EXPRESSION
    assert job["env"]["AUTORESEARCH_ENV"] == ENVIRONMENT_EXPRESSION


def test_code_archive_uploads_dev_commit_for_dev_feast_apply() -> None:
    # Feast apply는 현재 SHA의 immutable archive를 기다리므로 dev push도 archive를 만든다.
    archive_workflow = CODE_ARCHIVE_WORKFLOW.read_text(encoding="utf-8")

    assert "branches: [main, dev]" in archive_workflow


def test_workflow_renders_environment_into_job_manifest() -> None:
    workflow = _workflow_text()

    # envsubst allowlist 에서 빠지면 치환되지 않은 채 Job 이 뜬다.
    assert "${AUTORESEARCH_ENV} ${FEAST_ONLINE_FULL_SCAN_FOR_DELETION}" in workflow

    manifest = APPLY_JOB_MANIFEST.read_text(encoding="utf-8")
    assert "- name: AUTORESEARCH_ENV" in manifest
    assert "- name: FEAST_ONLINE_FULL_SCAN_FOR_DELETION" in manifest


def test_workflow_selects_environment_scoped_coordinates() -> None:
    # GitHub Environment 를 선택해야 같은 이름의 repo-level vars 보다
    # prod/dev Environment 좌표가 우선하며, 임시 dev 차단 가드는 더 이상 필요 없다.
    workflow = _workflow_text()

    assert f"    environment: {ENVIRONMENT_EXPRESSION}" in workflow
    assert "Guard against dev dispatch before dev coordinates are wired" not in workflow


def _run_archive_wait(
    gcloud_stderr: str, gcloud_exit_code: int, bin_dir: Path
) -> subprocess.CompletedProcess[str]:
    """대기 루프 bash 를 가짜 gcloud 로 실행한다.

    ``sleep`` 도 함께 가로채, 재시도 경로가 테스트를 10 분간 붙잡지 않게 한다.
    """
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash unavailable")

    gcloud_stub = bin_dir / "gcloud"
    gcloud_stub.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" {gcloud_stderr!r} >&2\nexit {gcloud_exit_code}\n',
        encoding="utf-8",
    )
    gcloud_stub.chmod(0o755)
    sleep_stub = bin_dir / "sleep"
    sleep_stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    sleep_stub.chmod(0o755)

    return subprocess.run(
        [bash, "-e", "-o", "pipefail", "-c", _step_script(_ARCHIVE_WAIT_STEP)],
        env={
            "CODE_ARTIFACTS_BUCKET": "bucket",
            "CODE_ARCHIVE_SHA": "0" * 40,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        capture_output=True,
        text=True,
    )


def test_archive_wait_fails_fast_when_the_lookup_is_not_a_missing_object(
    tmp_path: Path,
) -> None:
    # Given: 권한·가장 실패(#548 의 실제 실패 모드).
    denied = "ERROR: (gcloud.storage.objects.describe) HTTPError 403: Access denied."

    # When: 대기 루프가 그 gcloud 로 실행된다.
    completed = _run_archive_wait(denied, 1, tmp_path)

    # Then: 10 분을 기다리지 않고, gcloud 원문과 함께 즉시 실패해야 한다.
    assert completed.returncode == 1
    assert denied in completed.stdout
    assert "code archive lookup failed" in completed.stdout
    assert "code archive not found after 10m" not in completed.stdout


def test_archive_wait_keeps_retrying_while_the_object_is_merely_absent(
    tmp_path: Path,
) -> None:
    # Given: 아직 업로드 전(code-archive.yml 과 병렬 실행되므로 정상 경로다).
    absent = "ERROR: (gcloud.storage.objects.describe) gs://bucket/code/x.tar.gz not found: 404."

    # When: 대기 루프가 끝까지 재시도한다.
    completed = _run_archive_wait(absent, 1, tmp_path)

    # Then: fail-fast 로 새지 않고 기존 타임아웃 안내로 끝나야 한다.
    assert completed.returncode == 1
    assert "code archive not found after 10m" in completed.stdout
    assert "code archive lookup failed" not in completed.stdout


def test_archive_wait_succeeds_once_the_object_is_present(tmp_path: Path) -> None:
    # Given: 아카이브가 올라온 상태.
    completed = _run_archive_wait("", 0, tmp_path)

    # Then: 첫 조회에서 통과한다.
    assert completed.returncode == 0
    assert "code archive ready" in completed.stdout
