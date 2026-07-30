"""Agent Orchestration LLM 백엔드 경계.

[파이프라인]
오케스트레이션 실험 API가 프롬프트를 받고 영속화하기 전의 LLM 추론 구간을
담당한다. Codex CLI 공용 계정과 향후 OpenAI API 전환을 같은 응답 계약으로
정규화한다.

[기능]
읽기 전용·일회성 Codex CLI 실행과 OpenAI Responses API 호출을 수행하고,
텍스트·모델명·토큰 사용량을 API 계층이 저장 가능한 결과로 반환한다. Codex의
캐시·임시 파일은 요청별 작업 디렉터리에만 기록한다.

[비책임]
HTTP 라우팅과 상태 코드 변환(main.py), PostgreSQL 스키마·저장(db.py),
사용자 인증·OAuth 로그인 및 자격 증명 저장.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import signal
from tempfile import TemporaryDirectory

from openai import AsyncOpenAI, OpenAIError

from agent_orchestration.app.config import ServiceSettings


logger = logging.getLogger(__name__)

class LLMBackendError(RuntimeError):
    """외부 LLM 백엔드 호출을 안전하게 API 계층으로 전달하는 오류."""


@dataclass(frozen=True)
class LLMResult:
    """LLM 백엔드가 반환한 저장용 최종 응답."""

    text: str
    model: str
    token_count: int | None


async def generate_response(settings: ServiceSettings, prompt: str) -> LLMResult:
    """선택한 백엔드로 프롬프트를 전송해 저장 가능한 최종 응답을 반환한다."""
    if settings.llm_backend == "codex_cli":
        return await _generate_codex_cli(settings, prompt)
    if settings.llm_backend == "openai":
        return await _generate_openai(settings, prompt)
    raise LLMBackendError("Unsupported LLM backend.")


async def _generate_codex_cli(settings: ServiceSettings, prompt: str) -> LLMResult:
    """전용 Codex 홈과 최소 환경으로 로그인된 Codex CLI를 격리 실행한다."""
    with TemporaryDirectory(prefix="agent-orchestration-codex-") as workdir:
        output_path = Path(workdir) / "last_message.txt"
        command = [
            settings.codex_cli_path,
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
        if settings.codex_model:
            command.extend(["-m", settings.codex_model])
        command.append("-")

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env=_codex_environment(settings.codex_home, workdir),
                start_new_session=True,
            )
        except OSError as error:
            raise LLMBackendError("Failed to start Codex CLI.") from error

        communicate_task = asyncio.create_task(process.communicate(prompt.encode("utf-8")))
        try:
            await asyncio.wait_for(
                asyncio.shield(communicate_task),
                timeout=settings.codex_timeout_sec,
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
        model=settings.codex_model or "codex-cli",
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


async def _generate_openai(settings: ServiceSettings, prompt: str) -> LLMResult:
    """향후 API 전환을 위해 OpenAI Responses API를 호출한다."""
    if not settings.openai_api_key:
        raise LLMBackendError("OpenAI API key is not configured.")

    client = AsyncOpenAI(
        api_key=settings.openai_api_key,
        timeout=settings.openai_timeout_sec,
    )
    try:
        response = await client.responses.create(
            model=settings.openai_model,
            input=prompt,
            max_output_tokens=settings.openai_max_tokens,
        )
    except OpenAIError as error:
        raise LLMBackendError("OpenAI API call failed.") from error
    finally:
        await client.close()

    if response.status != "completed":
        logger.warning("OpenAI Responses API returned incomplete status=%s", response.status)
        raise LLMBackendError("OpenAI API response did not complete.")
    if not response.output_text:
        raise LLMBackendError("OpenAI API returned empty output.")
    return LLMResult(
        text=response.output_text,
        model=settings.openai_model,
        token_count=response.usage.total_tokens if response.usage else None,
    )
