"""feast-apply 워크플로우·Job 매니페스트의 prod/dev 배선 검증(#399).

bare ``feast`` CLI 는 ``feature_repo/bootstrap.py`` 를 거치지 않으므로, apply Job 은
``FEAST_ONLINE_FULL_SCAN_FOR_DELETION`` 을 워크플로우가 직접 주입받는다. 그 파생
규칙(prod=true, dev=false)이 워크플로우 bash 에 **손으로 복제**돼 있어
``feature_repo/env.py`` 와 조용히 어긋날 수 있다. 여기서는 워크플로우의 실제
bash 조각을 꺼내 실행해 env.py 와 같은 값을 내는지 대조한다.

feast 의존이 없어 기본 pytest 그룹에서 실행된다.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from feature_repo.env import online_full_scan_for_deletion

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
APPLY_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "feast-apply.yml"
APPLY_JOB_MANIFEST = REPOSITORY_ROOT / "deploy" / "feast" / "apply-job.yaml"

_DERIVATION_START = 'if [[ "$AUTORESEARCH_ENV" == "dev" ]]'


def _workflow_text() -> str:
    return APPLY_WORKFLOW.read_text(encoding="utf-8")


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


def test_workflow_defaults_to_prod_outside_manual_dispatch() -> None:
    # push(main) 에는 inputs 컨텍스트가 없다 — prod 로 떨어져야 회귀가 없다.
    assert "AUTORESEARCH_ENV: ${{ inputs.environment || 'prod' }}" in _workflow_text()


def test_workflow_renders_environment_into_job_manifest() -> None:
    workflow = _workflow_text()

    # envsubst allowlist 에서 빠지면 치환되지 않은 채 Job 이 뜬다.
    assert "${AUTORESEARCH_ENV} ${FEAST_ONLINE_FULL_SCAN_FOR_DELETION}" in workflow

    manifest = APPLY_JOB_MANIFEST.read_text(encoding="utf-8")
    assert "- name: AUTORESEARCH_ENV" in manifest
    assert "- name: FEAST_ONLINE_FULL_SCAN_FOR_DELETION" in manifest


def test_workflow_blocks_dev_dispatch_while_coordinates_are_prod() -> None:
    # #399: dev 좌표가 배선되기 전의 dev dispatch 는 prod registry 를 오염시키면서
    # prod 고아 키 GC 까지 끈다. 좌표 주입 PR 에서 이 가드를 제거한다.
    workflow = _workflow_text()

    assert "Guard against dev dispatch before dev coordinates are wired" in workflow
    assert '[[ "$AUTORESEARCH_ENV" != "prod" ]]' in workflow
