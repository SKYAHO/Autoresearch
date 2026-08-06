"""Executor Codex worker의 credential-free 수정 실행 경계를 검증한다.

전체 파이프라인에서 workspace-preparer가 봉인된 checkout을 만든 뒤 Codex가 파일을
수정하는 구간이다. 실제 Codex 인증·추론은 실행하지 않고, 임시 executable로 argv, 환경,
timeout process group 회수와 출력 비노출을 관찰한다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

import pytest

from agent_orchestration.executor.codex_worker import (
    CodexRunInput,
    CodexRunResult,
    CodexWorkerError,
    run_codex,
    run_codex_for_workspace,
)
from agent_orchestration.executor.prompt import build_codex_prompt
from agent_orchestration.executor.state import ExecutorWorkspaceState


_BASE_SHA = "a" * 40
_SENTINEL = "codex-output-must-not-be-logged"


def _run_input(tmp_path: Path, *, allowed_scope: tuple[str, ...] = ()) -> CodexRunInput:
    """실제 subprocess 실행에 사용할 최소한의 검증된 입력을 만든다."""
    repository = tmp_path / "workspace" / "repository"
    repository.mkdir(parents=True)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    return CodexRunInput(
        repository=repository,
        issue_body="<!-- experiment-id: 12345678-1234-5678-1234-567812345678 -->\nfix it",
        allowed_scope=allowed_scope,
        codex_home=codex_home,
        timeout_seconds=3,
    )


def _write_codex_executable(path: Path, body: str) -> None:
    """테스트별 관찰 코드를 가진 실제 ``codex`` executable을 만든다."""
    path.write_text(
        f"#!{sys.executable}\n"
        "from __future__ import annotations\n"
        f"{body}\n",
        encoding="utf-8",
    )
    path.chmod(0o700)


def test_prompt_contains_only_validated_work_contract() -> None:
    """prompt에는 검증된 이슈·scope·고정 검증 명령만 들어가야 한다."""
    run = CodexRunInput(
        repository=Path("/workspace/repository"),
        issue_body="검증된 이슈 본문",
        allowed_scope=("prod_model_contract", "feast_definition", "promotion"),
        codex_home=Path("/var/lib/codex"),
        timeout_seconds=60,
    )

    prompt = build_codex_prompt(run)

    assert "검증된 이슈 본문" in prompt
    assert "src/**" in prompt
    assert "src/features/model_contract.py" in prompt
    assert "autoresearch/**" in prompt
    assert "tests/**" in prompt
    assert "tools/**" in prompt
    assert "feature_repo/**" in prompt
    assert "uv run --no-sync ruff check agent_orchestration autoresearch tests tools" in prompt
    assert "uv run --no-sync python -m pytest" in prompt
    assert "agent_orchestration/**" in prompt
    assert ".github/**" in prompt
    assert "https://" not in prompt
    assert "/var/run" not in prompt
    assert "ORCH_" not in prompt
    assert "token" not in prompt.lower()
    assert "secret" not in prompt.lower()


def test_run_codex_uses_fixed_argv_and_allowlisted_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """상위 환경·Codex 원문 출력이 worker 경계를 넘으면 안 된다."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_path = tmp_path / "argv.json"
    environment_path = tmp_path / "environment.json"
    _write_codex_executable(
        bin_dir / "codex",
        "\n".join(
            [
                "import json",
                "import os",
                "from pathlib import Path",
                "import sys",
                f"Path({str(argv_path)!r}).write_text(json.dumps(sys.argv[1:]), encoding='utf-8')",
                f"Path({str(environment_path)!r}).write_text(json.dumps(dict(os.environ)), encoding='utf-8')",
                f"print({_SENTINEL!r})",
                f"print({_SENTINEL!r}, file=sys.stderr)",
            ]
        ),
    )
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("GITHUB_TOKEN", "github-token-must-not-pass")
    monkeypatch.setenv("ORCH_EXECUTOR_API_TOKEN", "executor-token-must-not-pass")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/gcp.json")
    run = _run_input(tmp_path, allowed_scope=("prod_model_contract",))

    result = run_codex(run)

    prompt = build_codex_prompt(run)
    assert result.exit_code == 0
    assert result.duration_ms >= 0
    assert json.loads(argv_path.read_text(encoding="utf-8")) == [
        "exec",
        "--sandbox",
        "workspace-write",
        "-C",
        str(run.repository),
        prompt,
    ]
    environment = json.loads(environment_path.read_text(encoding="utf-8"))
    assert set(environment) == {
        "CODEX_HOME",
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "TMPDIR",
        "PATH",
        "LANG",
        "LC_ALL",
        "UV_PROJECT_ENVIRONMENT",
    }
    assert environment["CODEX_HOME"] == str(run.codex_home)
    assert environment["PATH"] == str(bin_dir)
    assert environment["UV_PROJECT_ENVIRONMENT"] == "/opt/autoresearch-venv"
    assert "GITHUB_TOKEN" not in environment
    assert not any(
        key == "KUBERNETES_SERVICE_HOST"
        or key.startswith("GOOGLE_")
        or (key.startswith("ORCH_") and "TOKEN" in key)
        for key in environment
    )
    assert _SENTINEL not in caplog.text


@pytest.mark.skipif(os.name != "posix", reason="process group is POSIX-specific")
def test_timeout_terminates_the_codex_process_group_and_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """timeout은 Codex가 띄운 child까지 남기지 않아야 한다."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    child_pid_path = tmp_path / "child.pid"
    _write_codex_executable(
        bin_dir / "codex",
        "\n".join(
            [
                "import signal",
                "import subprocess",
                "import sys",
                "import time",
                "from pathlib import Path",
                "child = subprocess.Popen([sys.executable, '-c', 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])",
                f"Path({str(child_pid_path)!r}).write_text(str(child.pid), encoding='utf-8')",
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
                "while True:",
                "    time.sleep(0.1)",
            ]
        ),
    )
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(
        "agent_orchestration.executor.codex_worker._TERMINATION_GRACE_SECONDS", 0.1
    )
    run = _run_input(tmp_path)
    run = CodexRunInput(**{**run.__dict__, "timeout_seconds": 1})

    with pytest.raises(CodexWorkerError, match="codex_timeout"):
        run_codex(run)

    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not Path(f"/proc/{child_pid}").exists()


def test_existing_remote_tip_skips_codex_execution(tmp_path: Path) -> None:
    """기존 candidate 추정 tip은 Stage 5 채택 검증으로 넘기고 Codex를 실행하지 않는다."""
    run = _run_input(tmp_path)
    state = ExecutorWorkspaceState(
        schema_version=1,
        repository=run.repository,
        issue_body=run.issue_body,
        allowed_scope=run.allowed_scope,
        base_dev_sha=_BASE_SHA,
        remote_tip="b" * 40,
    )

    result = run_codex_for_workspace(
        state,
        codex_home=run.codex_home,
        timeout_seconds=run.timeout_seconds,
    )

    assert result == CodexRunResult(exit_code=0, duration_ms=0)
