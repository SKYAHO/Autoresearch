"""Agent Orchestration의 격리 Codex CLI 실행 경계.

[파이프라인]
오케스트레이션 API 또는 비공개 Runner가 프롬프트를 Codex로 추론하는 구간을
담당하며, 결과는 상위 계층에서 HTTP 응답과 PostgreSQL 저장으로 이어진다.

[기능]
읽기 전용·일회성 Codex CLI를 요청별 임시 작업 디렉터리에서 실행하고, 시간
초과·취소 시 프로세스 그룹을 회수한 뒤 공통 LLM 결과 계약으로 정규화한다.

[비책임]
API 백엔드 선택(applications.experiment_platform.api.llm), Runner 동시성 제어와 HTTP
라우팅(applications.experiment_platform.runner.app), OAuth 자격 증명 주입·저장.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import signal
from tempfile import TemporaryDirectory

from applications.experiment_platform.shared.contracts import LLMBackendError, LLMResult


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodexSettings:
    """Codex CLI 프로세스 실행에 필요한 안전한 설정 값."""

    cli_path: str
    home: str
    model: str | None
    timeout_sec: int


async def generate_codex_response(settings: CodexSettings, prompt: str) -> LLMResult:
    """전용 Codex 홈과 최소 환경으로 로그인된 Codex CLI를 격리 실행한다."""
    with TemporaryDirectory(prefix="agent-orchestration-codex-") as workdir:
        output_path = Path(workdir) / "last_message.txt"
        command = [
            settings.cli_path,
            "exec",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--skip-git-repo-check",
            "--color",
            "never",
            "-C",
            workdir,
            "-o",
            str(output_path),
        ]
        if settings.model:
            command.extend(["-m", settings.model])
        command.append("-")

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                env=_codex_environment(settings.home, workdir),
                start_new_session=True,
            )
        except OSError as error:
            raise LLMBackendError("Failed to start Codex CLI.") from error

        communicate_task = asyncio.create_task(process.communicate(prompt.encode("utf-8")))
        try:
            await asyncio.wait_for(
                asyncio.shield(communicate_task),
                timeout=settings.timeout_sec,
            )
        except TimeoutError as error:
            await _terminate_and_wait(process, communicate_task)
            logger.warning("Codex CLI timed out")
            raise LLMBackendError("Codex CLI timed out.") from error
        except asyncio.CancelledError:
            await _terminate_and_wait(process, communicate_task)
            raise
        except OSError as error:
            raise LLMBackendError("Codex CLI execution failed.") from error

        if process.returncode != 0:
            logger.warning("Codex CLI exited with returncode=%s", process.returncode)
            raise LLMBackendError("Codex CLI failed.")
        if not output_path.exists():
            raise LLMBackendError("Codex CLI returned no output.")

        text = output_path.read_text(encoding="utf-8").strip()
        if not text:
            raise LLMBackendError("Codex CLI returned empty output.")

    return LLMResult(
        text=text,
        model=settings.model or "codex-cli",
        token_count=None,
    )


def _codex_environment(codex_home: str, workdir: str) -> dict[str, str]:
    """Codex 자격 증명과 요청별 쓰기 경로만 하위 프로세스에 전달한다."""
    return {
        "CODEX_HOME": codex_home,
        "HOME": workdir,
        "TMPDIR": workdir,
        "XDG_CACHE_HOME": workdir,
        "XDG_STATE_HOME": workdir,
        "PATH": os.environ.get("PATH", ""),
    }


def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """Codex와 같은 세션의 하위 프로세스를 함께 종료한다."""
    if process.returncode is not None:
        return
    if os.name == "posix" and process.pid is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
    process.kill()


async def _terminate_and_wait(
    process: asyncio.subprocess.Process,
    communicate_task: asyncio.Task[tuple[bytes, bytes]],
) -> None:
    """프로세스 그룹 종료 뒤 파이프를 닫고 하위 프로세스를 회수한다."""
    _terminate_process_group(process)
    try:
        await asyncio.wait_for(asyncio.shield(communicate_task), timeout=5)
    except (OSError, TimeoutError):
        communicate_task.cancel()
        await asyncio.gather(communicate_task, return_exceptions=True)
