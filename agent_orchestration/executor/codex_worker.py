"""Executor Pod에서 credential 없이 Codex의 workspace 파일 수정을 실행한다.

[파이프라인]
workspace-preparer가 봉인 이슈와 exp branch checkout을 검증한 뒤, candidate-verifier가
실제 diff를 검사하기 전 Codex가 workspace 파일만 수정하는 구간을 담당한다.

[기능]
read-only auth source의 regular `auth.json`만 per-run writable scratch `CODEX_HOME`으로
복사한 뒤 `codex exec --ephemeral`을 실행한다. 명시적 환경 allowlist와 전용 process
group으로 noninteractive Codex를 실행하고, timeout·취소 시 child process까지 회수한다.
원격 tip이 base와 다르면 기존 candidate 채택 경로로 넘기기 위해 Codex 실행을 생략한다.

[비책임]
이슈·ref·workspace 검증(`workspace.py`), candidate 범위와 테스트 승인(Stage 4), Git
commit·push와 candidate API 보고(Stage 5), Pod Secret·volume 정책(Autoresearch-infra)은
담당하지 않는다.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
from tempfile import TemporaryDirectory
import threading
import time
from typing import BinaryIO

from agent_orchestration.executor.prompt import build_codex_prompt
from agent_orchestration.executor.state import ExecutorWorkspaceState


_TERMINATION_GRACE_SECONDS = 5.0
_PIPE_RING_BUFFER_BYTES = 64 * 1024
_FIXED_UV_PROJECT_ENVIRONMENT = "/opt/autoresearch-venv"
_CODEX_AUTH_FILENAME = "auth.json"


class CodexWorkerError(RuntimeError):
    """Codex worker가 정제된 실행 실패 사유를 반환한다."""


@dataclass(frozen=True)
class CodexRunInput:
    """검증된 workspace에서 Codex를 실행할 최소 입력이다."""

    repository: Path
    issue_body: str
    allowed_scope: tuple[str, ...]
    codex_home: Path
    timeout_seconds: int


@dataclass(frozen=True)
class CodexRunResult:
    """원문 출력 없이 Codex 종료 결과만 전달한다."""

    exit_code: int
    duration_ms: int


class _RingBuffer:
    """pipe 원문을 영속화하지 않고 제한된 크기로만 소비한다."""

    def __init__(self, capacity: int) -> None:
        self._capacity = capacity
        self._chunks: deque[bytes] = deque()
        self._size = 0

    def append(self, data: bytes) -> None:
        """가장 최근의 제한된 bytes만 유지한다."""
        if not data:
            return
        if len(data) >= self._capacity:
            self._chunks.clear()
            self._chunks.append(data[-self._capacity :])
            self._size = self._capacity
            return
        self._chunks.append(data)
        self._size += len(data)
        while self._size > self._capacity:
            removed = self._chunks.popleft()
            self._size -= len(removed)


def _prepare_runtime_codex_home(source_home: Path, temporary_root: Path) -> Path:
    """source의 regular auth.json만 실행별 writable CODEX_HOME으로 복사한다."""
    source_auth = source_home / _CODEX_AUTH_FILENAME
    try:
        source_status = source_auth.lstat()
        if not stat.S_ISREG(source_status.st_mode):
            raise CodexWorkerError("codex_auth_invalid")

        runtime_home = temporary_root / "codex-home"
        runtime_home.mkdir(mode=0o700)
        runtime_home.chmod(0o700)
        runtime_auth = runtime_home / _CODEX_AUTH_FILENAME
        descriptor = os.open(
            runtime_auth,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o400,
        )
        with source_auth.open("rb") as source, os.fdopen(descriptor, "wb") as destination:
            os.fchmod(destination.fileno(), 0o400)
            while chunk := source.read(8192):
                destination.write(chunk)
    except OSError as error:
        raise CodexWorkerError("codex_auth_invalid") from error
    return runtime_home


def _drain_pipe(pipe: BinaryIO, buffer: _RingBuffer) -> None:
    """child pipe를 끝까지 소비해 큰 출력이 Codex를 block하지 않게 한다."""
    try:
        while chunk := pipe.read(8192):
            buffer.append(chunk)
    finally:
        pipe.close()


def _temporary_environment(codex_home: Path, temporary_root: Path) -> dict[str, str]:
    """상위 환경을 상속하지 않는 Codex subprocess allowlist를 만든다."""
    home = temporary_root / "home"
    config_home = temporary_root / "config"
    cache_home = temporary_root / "cache"
    tmpdir = temporary_root / "tmp"
    for directory in (home, config_home, cache_home, tmpdir):
        directory.mkdir()
    return {
        "CODEX_HOME": str(codex_home),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_CACHE_HOME": str(cache_home),
        "TMPDIR": str(tmpdir),
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "UV_PROJECT_ENVIRONMENT": _FIXED_UV_PROJECT_ENVIRONMENT,
    }


def _validate_run(run: CodexRunInput) -> None:
    """외부 entrypoint가 잘못된 filesystem·timeout 입력으로 실행하지 않게 한다."""
    if not run.repository.is_absolute() or not run.repository.is_dir():
        raise CodexWorkerError("repository_invalid")
    if not run.codex_home.is_absolute() or not run.codex_home.is_dir():
        raise CodexWorkerError("codex_home_invalid")
    if not isinstance(run.issue_body, str) or not run.issue_body:
        raise CodexWorkerError("issue_body_invalid")
    if not isinstance(run.timeout_seconds, int) or run.timeout_seconds <= 0:
        raise CodexWorkerError("timeout_invalid")


def _git_directory(repository: Path) -> Path:
    """일반 clone의 `.git` directory만 worker의 immutable metadata 경계로 인정한다."""
    git_directory = repository / ".git"
    if not git_directory.is_dir() or git_directory.is_symlink():
        raise CodexWorkerError("git_directory_invalid")
    return git_directory.resolve()


def _mountinfo_lines() -> tuple[str, ...]:
    """현재 Linux mount namespace의 mountinfo를 안전하게 읽는다."""
    try:
        return tuple(Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines())
    except OSError as error:
        raise CodexWorkerError("mountinfo_unavailable") from error


def _unescape_mount_path(value: str) -> str:
    """mountinfo의 octal escape를 POSIX 경로 문자열로 되돌린다."""
    return re.sub(
        r"\\([0-7]{3})",
        lambda match: chr(int(match.group(1), 8)),
        value,
    )


def _git_directory_is_read_only(git_directory: Path) -> bool:
    """`.git` 자체가 현재 namespace의 별도 read-only mount일 때만 참을 반환한다."""
    if os.name != "posix":
        return False
    expected = str(git_directory.resolve())
    for line in _mountinfo_lines():
        left, separator, _right = line.partition(" - ")
        if not separator:
            continue
        fields = left.split()
        if len(fields) < 6:
            continue
        mount_path = _unescape_mount_path(fields[4])
        mount_options = set(fields[5].split(","))
        if mount_path == expected:
            return "ro" in mount_options
    return False


def _git_metadata_digest(git_directory: Path) -> str:
    """HEAD·refs·config·hooks를 포함한 Git metadata tree의 내용 digest를 계산한다."""
    digest = sha256()
    try:
        for directory, directories, filenames in os.walk(git_directory, followlinks=False):
            current = Path(directory)
            directories.sort()
            filenames.sort()
            for name in [*directories, *filenames]:
                path = current / name
                relative = path.relative_to(git_directory).as_posix()
                status = path.lstat()
                if path.is_symlink() or not (path.is_dir() or path.is_file()):
                    raise CodexWorkerError("git_metadata_invalid")
                digest.update(relative.encode("utf-8"))
                digest.update(b"\0")
                digest.update(oct(status.st_mode).encode("ascii"))
                digest.update(b"\0")
                if path.is_file():
                    with path.open("rb") as handle:
                        while chunk := handle.read(8192):
                            digest.update(chunk)
    except OSError as error:
        raise CodexWorkerError("git_metadata_unavailable") from error
    return digest.hexdigest()


def _capture_protected_git_metadata(repository: Path) -> tuple[Path, str]:
    """Codex 시작 전 `.git`이 kernel read-only mount이며 봉인 상태인지 확인한다."""
    git_directory = _git_directory(repository)
    if not _git_directory_is_read_only(git_directory):
        raise CodexWorkerError("git_metadata_unprotected")
    return git_directory, _git_metadata_digest(git_directory)


def _send_process_group_signal(process: subprocess.Popen[bytes], signal_number: int) -> None:
    """새 세션의 Codex와 child process에 같은 종료 신호를 보낸다."""
    if os.name == "posix" and process.pid is not None:
        try:
            os.killpg(process.pid, signal_number)
        except ProcessLookupError:
            return
        return
    if signal_number == signal.SIGTERM:
        process.terminate()
    else:
        process.kill()


def _process_group_is_alive(process: subprocess.Popen[bytes]) -> bool:
    """parent가 먼저 끝난 뒤 남은 child process가 있는지 확인한다."""
    if os.name != "posix" or process.pid is None:
        return process.poll() is None
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    return True


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    """timeout·취소 시 TERM grace 뒤 KILL로 Codex process group을 회수한다."""
    _send_process_group_signal(process, signal.SIGTERM)
    deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
    while _process_group_is_alive(process) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _process_group_is_alive(process):
        _send_process_group_signal(process, signal.SIGKILL)
    try:
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _send_process_group_signal(process, signal.SIGKILL)
        process.wait()


def _join_pipe_readers(
    readers: tuple[threading.Thread, threading.Thread],
    pipes: tuple[BinaryIO, BinaryIO],
) -> None:
    """pipe reader를 정리하되 child가 pipe를 붙잡아도 worker가 무한 대기하지 않는다."""
    for reader in readers:
        reader.join(timeout=1)
    if any(reader.is_alive() for reader in readers):
        for pipe in pipes:
            pipe.close()
        for reader in readers:
            reader.join(timeout=1)


def run_codex(run: CodexRunInput) -> CodexRunResult:
    """Codex를 workspace-write sandbox로 실행하고 원문 출력 없이 종료 결과를 반환한다."""
    _validate_run(run)
    prompt = build_codex_prompt(run)
    git_directory, sealed_git_metadata = _capture_protected_git_metadata(run.repository)
    argv = (
        "codex",
        "exec",
        "--ephemeral",
        "--sandbox",
        "workspace-write",
        "-C",
        str(run.repository),
        prompt,
    )
    started_at = time.monotonic()
    with TemporaryDirectory(prefix="executor-codex-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        runtime_codex_home = _prepare_runtime_codex_home(run.codex_home, temporary_root)
        environment = _temporary_environment(runtime_codex_home, temporary_root)
        try:
            process = subprocess.Popen(
                argv,
                cwd=run.repository,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                start_new_session=True,
            )
        except OSError as error:
            raise CodexWorkerError("codex_unavailable") from error

        assert process.stdout is not None
        assert process.stderr is not None
        stdout_buffer = _RingBuffer(_PIPE_RING_BUFFER_BYTES)
        stderr_buffer = _RingBuffer(_PIPE_RING_BUFFER_BYTES)
        pipes = (process.stdout, process.stderr)
        readers = (
            threading.Thread(
                target=_drain_pipe, args=(process.stdout, stdout_buffer), daemon=True
            ),
            threading.Thread(
                target=_drain_pipe, args=(process.stderr, stderr_buffer), daemon=True
            ),
        )
        for reader in readers:
            reader.start()
        try:
            exit_code = process.wait(timeout=run.timeout_seconds)
            if _process_group_is_alive(process):
                _terminate_process_group(process)
                raise CodexWorkerError("codex_child_process_leaked")
        except subprocess.TimeoutExpired:
            _terminate_process_group(process)
            raise CodexWorkerError("codex_timeout") from None
        except BaseException:
            _terminate_process_group(process)
            raise
        finally:
            _join_pipe_readers(readers, pipes)

    if _git_metadata_digest(git_directory) != sealed_git_metadata:
        raise CodexWorkerError("git_metadata_changed")

    duration_ms = int((time.monotonic() - started_at) * 1000)
    return CodexRunResult(exit_code=exit_code, duration_ms=duration_ms)


def run_codex_for_workspace(
    state: ExecutorWorkspaceState,
    *,
    codex_home: Path,
    timeout_seconds: int,
) -> CodexRunResult:
    """state가 base checkout일 때만 Codex를 실행하고 기존 candidate 경로는 생략한다."""
    if state.remote_tip != state.base_dev_sha:
        return CodexRunResult(exit_code=0, duration_ms=0)
    return run_codex(
        CodexRunInput(
            repository=state.repository,
            issue_body=state.issue_body,
            allowed_scope=state.allowed_scope,
            codex_home=codex_home,
            timeout_seconds=timeout_seconds,
        )
    )
