"""``.github/workflows/ci.yml`` 구조 검증 테스트.

이 테스트는 CI 워크플로우가 아래 보안 스텝을 포함하는지 확인한다
(Task 10 / #47 — security 에이전트 M1 권고 반영).

- ``pip-audit``: 의존성 취약점 스캔(``requirements.txt`` 대상)
- ``gitleaks``: 시크릿 스캔(전체 히스토리 → ``fetch-depth: 0``)

워크플로우 YAML 의 *구조* 만 검증하므로 로컬/CI 어디서든 빠르게 실행 가능하다.
"""

from __future__ import annotations

from pathlib import Path

import yaml

CI_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def _load_workflow() -> dict:
    """ci.yml 을 파싱해서 반환. 파일이 없으면 테스트 자체가 실패한다."""
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def _step_names(job: dict) -> list[str]:
    return [str(step.get("name", "")) for step in job.get("steps", [])]


def test_workflow_has_security_job() -> None:
    workflow = _load_workflow()
    jobs = workflow.get("jobs", {})
    assert "security" in jobs, "security 잡이 ci.yml 에 정의되어야 한다"


def test_security_job_runs_on_ubuntulatest() -> None:
    security = _load_workflow()["jobs"]["security"]
    assert security.get("runs-on") == "ubuntu-latest"


def test_security_job_checks_out_full_history() -> None:
    """gitleaks 전체 히스토리 스캔을 위해 fetch-depth=0 필수."""
    security = _load_workflow()["jobs"]["security"]
    checkout = next(
        step for step in security["steps"] if "checkout" in str(step.get("uses", ""))
    )
    assert checkout.get("with", {}).get("fetch-depth") == 0


def test_security_job_runs_pip_audit_against_requirements() -> None:
    security = _load_workflow()["jobs"]["security"]
    pip_audit_step = next(
        step
        for step in security["steps"]
        if "pip-audit" in str(step.get("run", ""))
    )
    assert "requirements.txt" in pip_audit_step["run"], (
        "pip-audit 스텝은 requirements.txt 대상이어야 한다"
    )


def test_security_job_uses_gitleaks_action() -> None:
    security = _load_workflow()["jobs"]["security"]
    gitleaks_step = next(
        step
        for step in security["steps"]
        if "gitleaks/gitleaks-action" in str(step.get("uses", ""))
    )
    assert gitleaks_step["uses"].startswith("gitleaks/gitleaks-action@"), (
        "gitleaks 공식 액션을 사용해야 한다"
    )


def test_security_job_passes_github_token_to_gitleaks() -> None:
    security = _load_workflow()["jobs"]["security"]
    gitleaks_step = next(
        step
        for step in security["steps"]
        if "gitleaks/gitleaks-action" in str(step.get("uses", ""))
    )
    env = gitleaks_step.get("env", {})
    assert env.get("GITHUB_TOKEN") == "${{ secrets.GITHUB_TOKEN }}", (
        "gitleaks 액션에 GITHUB_TOKEN 전달 필요"
    )


def test_existing_pytest_and_docker_jobs_preserved() -> None:
    """보안 잡 추가가 기존 잡을 덮어쓰지 않았는지 회귀 체크."""
    jobs = _load_workflow()["jobs"]
    assert "pytest" in jobs
    assert "docker-build" in jobs
    # pytest 매트릭스도 그대로
    assert jobs["pytest"]["strategy"]["matrix"]["python-version"] == ["3.11", "3.12"]
