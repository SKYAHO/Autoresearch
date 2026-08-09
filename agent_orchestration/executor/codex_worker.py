"""Executor Pod에서 Codex 프로세스 한 번을 격리 실행하는 경계.

[파이프라인]
workspace-preparer가 봉인 이슈와 exp branch checkout을 검증한 뒤 candidate-verifier가
실제 diff를 검사하기 전 Codex가 workspace 파일만 수정하는 구간(Codex #1)과, 채점이 끝난
뒤 candidate-finalizer가 `report.md`를 받는 구간(Codex #2)을 담당한다.

[기능]
read-only auth source의 regular `auth.json`만 per-run writable scratch `CODEX_HOME`으로
복사한 뒤 고정 모델·추론 강도로 `codex exec --ephemeral`을 실행한다. 명시적 환경
allowlist와 전용 process group으로 noninteractive Codex를 실행하고, timeout·취소 시
child process까지 회수한다. 성공·실패 어느 경로에서든 진단용 출력 tail을 호출자에게
돌려준다. 원격 tip이 base와 다르면 기존 candidate 채택 경로로 넘기기 위해 Codex 실행을
생략한다. 코드 수정 실행(Codex #1)에서는 `.git` 봉인을 확인하고, clone의 `AGENTS.md`를
실행 동안만 executor 전용 하네스 지침으로 바꿨다가 **반드시 되돌린다**. 프롬프트를 직접
주입하는 실행 경로도 함께 제공해, 리포트 작성처럼 이슈 본문에서 나오지 않는 지시도
같은 격리로 돌게 한다.

[비책임]
이슈·ref·workspace 검증(`workspace.py`), 지시문 문안 조립(`prompt.py`), candidate 범위와
테스트 승인(`verifier.py`), Git commit·push와 candidate API 보고(`finalizer.py`), 리포트
입력 수집과 형식 확인(`report.py`), Pod Secret·volume 정책(Autoresearch-infra)은
담당하지 않는다. 돌려준 출력을 **로그로 내보내는 것도 담당하지 않는다** — stage 경계인
`phase2`가 한다.
"""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
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
from typing import BinaryIO, Iterator

from agent_orchestration.executor.prompt import (
    HARNESS_FILENAME,
    build_codex_prompt,
    build_harness_instructions,
)
from agent_orchestration.executor.state import ExecutorWorkspaceState


_TERMINATION_GRACE_SECONDS = 5.0
_PIPE_RING_BUFFER_BYTES = 64 * 1024
_FIXED_UV_PROJECT_ENVIRONMENT = "/opt/autoresearch-venv"
_CODEX_AUTH_FILENAME = "auth.json"
# 실험 간 비교 가능성을 위해 모델과 추론 강도를 argv로 고정한다(#612). Codex CLI는
# `--model`에 임의 문자열을 받아 런타임에서야 실패하므로, 슬러그를 바꿀 때는
# `codex doctor` 또는 실제 Job 로그로 수용 여부를 확인해야 한다.
_CODEX_MODEL = "gpt-5.6-luna"
_CODEX_REASONING_EFFORT = "max"
# executor Pod는 PodSecurity restricted라 capability가 전부 드롭되어 있고,
# `workspace-write`가 쓰는 bubblewrap이 비특권 user namespace를 만들지 못한다. bwrap은
# 필터가 아니라 프로세스를 감싸는 껍데기라 뜨지 못하면 파일 읽기·쓰기가 함께 막히고,
# Codex는 그것을 "변경 없음"으로 보고하며 exit 0으로 끝난다(#612).
#
# 경계가 사라지는 것은 아니다. 루트 FS 읽기 전용, `.git` 커널 read-only 마운트와 전후
# 다이제스트 대조, verifier의 사후 경로 검사, credential 미마운트, NetworkPolicy가
# 그대로 남는다. 이 container에서 쓰기 가능한 곳은 `/workspace`(단 `.git` 제외)와
# `/tmp` 두 곳뿐이며, 그것이 Codex가 수정해야 하는 대상과 정확히 일치한다.
_CODEX_SANDBOX_MODE = "danger-full-access"


class CodexWorkerError(RuntimeError):
    """Codex worker가 정제된 실행 실패 사유를 반환한다.

    실패 경로에서도 진단이 되도록 Codex 출력 tail을 함께 싣는다(#612). timeout처럼 결과가
    없는 경로가 오히려 원문이 가장 필요한 곳이다. 사유 코드는 `args`에 하나만 유지해야
    `phase2._safe_failure_reason`이 `redacted`로 바꾸지 않고 그대로 통과시킨다 — 출력은
    반드시 attribute로만 붙인다.
    """

    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class CodexRunInput:
    """검증된 workspace에서 Codex를 실행할 최소 입력이다."""

    repository: Path
    issue_body: str
    allowed_scope: tuple[str, ...]
    codex_home: Path
    timeout_seconds: int


@dataclass(frozen=True)
class CodexExecution:
    """작업 디렉터리와 지시문만으로 Codex 한 번을 실행하는 최소 입력이다.

    `CodexRunInput`이 이슈에서 지시문을 만드는 코드 수정 실행의 입력이라면, 이쪽은
    지시문이 이미 정해진 실행의 입력이다. 리포트 작성처럼 이슈 본문에서 나오지 않는
    지시도 같은 격리(환경 allowlist·process group 회수·출력 tail)로 돌리려는 것이다.
    """

    working_directory: Path
    prompt: str
    codex_home: Path
    timeout_seconds: int


@dataclass(frozen=True)
class CodexRunResult:
    """Codex 종료 결과와 진단용 출력 tail을 전달한다.

    `stdout`·`stderr`는 각각 `_PIPE_RING_BUFFER_BYTES` 상한의 **최근 구간**이며 전체
    출력이 아니다. 이 저장소는 외부 문자열을 로그로 내보내지 않는 것을 원칙으로 하지만
    (`phase2._safe_failure_reason`), codex-worker는 여기서 예외를 둔다 — Codex가 실패를
    exit 0으로 보고하는 구조라 종료 코드만으로는 진단이 불가능하다(#612). 이 container에는
    token이 마운트되지 않고 환경 allowlist에도 secret이 없어 노출 범위는 저장소 소스에
    한정된다.
    """

    exit_code: int
    duration_ms: int
    stdout: str = ""
    stderr: str = ""


class _RingBuffer:
    """pipe 원문을 제한된 크기의 최근 구간으로만 보관한다."""

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

    def decode(self) -> str:
        """보관 중인 bytes를 로그에 실을 수 있는 문자열로 만든다.

        용량 초과로 앞을 잘라내면 multi-byte 문자 경계가 깨질 수 있어 대체 문자로 복구한다.
        """
        return b"".join(self._chunks).decode("utf-8", errors="replace")


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


def _with_output(
    error: CodexWorkerError, stdout: _RingBuffer, stderr: _RingBuffer
) -> CodexWorkerError:
    """실패 사유는 그대로 두고 진단용 출력 tail만 예외에 붙인다."""
    error.stdout = stdout.decode()
    error.stderr = stderr.decode()
    return error


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


def _validate_execution(execution: CodexExecution) -> None:
    """프롬프트를 직접 주입하는 실행도 같은 filesystem·timeout 계약을 지키게 한다."""
    if (
        not execution.working_directory.is_absolute()
        or not execution.working_directory.is_dir()
    ):
        raise CodexWorkerError("working_directory_invalid")
    if not execution.codex_home.is_absolute() or not execution.codex_home.is_dir():
        raise CodexWorkerError("codex_home_invalid")
    if not isinstance(execution.prompt, str) or not execution.prompt:
        raise CodexWorkerError("prompt_invalid")
    if not isinstance(execution.timeout_seconds, int) or execution.timeout_seconds <= 0:
        raise CodexWorkerError("timeout_invalid")


@contextmanager
def _harness_instructions(repository: Path, content: str) -> Iterator[None]:
    """Codex 실행 동안만 clone의 `AGENTS.md`를 executor 전용 하네스 지침으로 바꾼다.

    저장소 원본은 사람과 로컬 에이전트를 위한 기여 가이드라 executor가 수행할 수 없는
    절차(이슈 발행, 브랜치 생성, `docs/specs/` 계획 작성)를 요구한다. 그대로 두면 최악의
    경우 Codex가 "규칙상 못 하겠다"며 아무것도 하지 않고, 그 결과는 `no_changes`로 나와
    실제 실패와 구분되지 않는다.

    **반드시 되돌린다.** verifier는 `git status`와 `ls-files --others`로 변경 파일을
    수집하므로(`verifier._collect_changes`), 교체한 채로 두면 하네스 파일이 Codex의
    변경으로 잡혀 commit·push된다. 복원 실패는 그 사고와 같은 결과이므로 실행이 성공했든
    아니든 fail-closed로 끊는다 — 진행 중이던 예외를 덮더라도 저장소에 하네스 파일을
    남기는 것보다 낫다.
    """
    path = repository / HARNESS_FILENAME
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise CodexWorkerError("harness_path_invalid")
    try:
        original = path.read_bytes() if path.is_file() else None
        mode = stat.S_IMODE(path.lstat().st_mode) if original is not None else None
        path.write_text(content, encoding="utf-8")
    except OSError as error:
        raise CodexWorkerError("harness_install_failed") from error
    try:
        yield
    finally:
        try:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
                if mode is not None:
                    path.chmod(mode)
        except OSError as error:
            raise CodexWorkerError("harness_restore_failed") from error


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


def _execute_codex(execution: CodexExecution) -> CodexRunResult:
    """주입된 지시문으로 Codex 프로세스 한 번을 격리 실행한다.

    `.git` 봉인과 하네스 교체는 여기서 하지 않는다 — 그 둘은 저장소 working tree를
    수정하는 실행(Codex #1)에만 해당하고, 리포트 작성은 clone 밖 디렉터리에서 돈다.
    """
    argv = (
        "codex",
        "exec",
        "--ephemeral",
        "--model",
        _CODEX_MODEL,
        "-c",
        f'model_reasoning_effort="{_CODEX_REASONING_EFFORT}"',
        "--sandbox",
        _CODEX_SANDBOX_MODE,
        "-C",
        str(execution.working_directory),
        execution.prompt,
    )
    started_at = time.monotonic()
    with TemporaryDirectory(prefix="executor-codex-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        runtime_codex_home = _prepare_runtime_codex_home(
            execution.codex_home, temporary_root
        )
        environment = _temporary_environment(runtime_codex_home, temporary_root)
        try:
            process = subprocess.Popen(
                argv,
                cwd=execution.working_directory,
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
        # 안쪽 try가 reader를 join한 **뒤에** 바깥 try가 buffer를 읽어야 in-flight 출력까지
        # 들어온다. 순서가 뒤집히면 timeout 경로에서 마지막 구간을 잃는다.
        try:
            try:
                exit_code = process.wait(timeout=execution.timeout_seconds)
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
        except CodexWorkerError as error:
            _with_output(error, stdout_buffer, stderr_buffer)
            raise

    duration_ms = int((time.monotonic() - started_at) * 1000)
    return CodexRunResult(
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout=stdout_buffer.decode(),
        stderr=stderr_buffer.decode(),
    )


def run_codex_execution(execution: CodexExecution) -> CodexRunResult:
    """이미 정해진 지시문으로 Codex를 실행하고 종료 결과와 출력 tail을 반환한다."""
    _validate_execution(execution)
    return _execute_codex(execution)


def run_codex(run: CodexRunInput) -> CodexRunResult:
    """봉인된 clone에서 Codex 코드 수정 실행 하나를 수행한다.

    `.git` 봉인을 실행 전후로 대조하고, 실행 동안만 clone의 `AGENTS.md`를 executor 전용
    하네스 지침으로 바꾼다. 교체는 Codex 시작 직전, 복원은 `finally`다 — 그 시점의
    workspace가 verifier의 검증 baseline이므로 복원하지 않으면 하네스 파일이 candidate
    변경으로 잡혀 commit·push된다.
    """
    _validate_run(run)
    git_directory, sealed_git_metadata = _capture_protected_git_metadata(run.repository)
    with _harness_instructions(
        run.repository, build_harness_instructions(run.allowed_scope)
    ):
        result = _execute_codex(
            CodexExecution(
                working_directory=run.repository,
                prompt=build_codex_prompt(run),
                codex_home=run.codex_home,
                timeout_seconds=run.timeout_seconds,
            )
        )
    if _git_metadata_digest(git_directory) != sealed_git_metadata:
        error = CodexWorkerError("git_metadata_changed")
        error.stdout = result.stdout
        error.stderr = result.stderr
        raise error
    return result


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
