"""Executor Pod에서 Codex 프로세스 한 번을 격리 실행하는 경계.

[파이프라인]
workspace-preparer가 봉인 이슈와 exp branch checkout을 검증한 뒤 candidate-verifier가
실제 diff를 검사하기 전 Codex가 workspace 파일만 수정하는 구간(Codex #1)과, 채점이 끝난
뒤 candidate-finalizer가 `report.md`를 받는 구간(Codex #2)을 담당한다.

[기능]
read-only auth source의 regular `auth.json`만 per-run writable scratch `CODEX_HOME`으로
복사한 뒤 고정 모델·추론 강도로 `codex exec --ephemeral --json`을 실행한다. 실행 중
스트림에서 turn별 토큰 사용량을 누적해 input·cached·output 분해로 돌려준다(#742) —
tail 링버퍼가 앞을 버리므로 파이프를 읽는 자리에서 함께 훑는다. 명시적 환경
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
import json
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

from applications.experiment_platform.executor.prompt import (
    HARNESS_FILENAME,
    ResourceBudget,
    build_codex_prompt,
    build_harness_instructions,
)
from applications.experiment_platform.executor.state import ExecutorWorkspaceState


_TERMINATION_GRACE_SECONDS = 5.0
_PIPE_RING_BUFFER_BYTES = 64 * 1024
_FIXED_UV_PROJECT_ENVIRONMENT = "/opt/autoresearch-venv"
_CODEX_AUTH_FILENAME = "auth.json"
# clone에 `AGENTS.md`가 없던 경우 하네스 파일을 만들 mode. Codex가 읽기만 하면 된다.
_HARNESS_DEFAULT_MODE = 0o644
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
    usage: "CodexTokenUsage | None" = None


@dataclass(frozen=True)
class CodexTokenUsage:
    """Codex 실행 한 번이 쓴 토큰을 과금 구분대로 나눠 담는다(#742).

    `input_tokens`는 `cached_input_tokens`를 **포함한** 값이다 — Codex CLI가 그렇게
    싣는다. 캐시 적중분과 신규 입력분은 단가가 다르므로, 종량제 환산을 하려면 이
    포함 관계를 그대로 지켜야 한다. 빼는 일은 `fresh_input_tokens`가 한 번만 한다.
    """

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    turns: int = 0

    @property
    def fresh_input_tokens(self) -> int:
        """캐시에 걸리지 않아 정가로 과금되는 입력 토큰."""
        return max(0, self.input_tokens - self.cached_input_tokens)

    @property
    def total_tokens(self) -> int:
        """입력과 출력을 합한 총량. `reasoning`은 `output`에 포함된다."""
        return self.input_tokens + self.output_tokens


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
    # Codex CLI는 git repository가 아닌 디렉터리에서 `--skip-git-repo-check` 없이
    # 실행되면 `Not inside a trusted directory`로 거부한다(#642 실측). 코드 수정
    # 실행은 clone 안에서 도는 것이 계약이라 검사를 그대로 두고, 리포트 작성처럼
    # **clone 밖에서 도는 것이 계약인** 실행만 이 값을 켠다.
    skip_git_repo_check: bool = False


@dataclass(frozen=True)
class CodexRunResult:
    """Codex 종료 결과와 진단용 출력 tail을 전달한다.

    `stdout`·`stderr`는 각각 `_PIPE_RING_BUFFER_BYTES` 상한의 **최근 구간**이며 전체
    출력이 아니다. 이 저장소는 외부 문자열을 로그로 내보내지 않는 것을 원칙으로 하지만
    (`phase2._safe_failure_reason`), Codex 실행은 여기서 예외를 둔다 — Codex가 실패를
    exit 0으로 보고하는 구조라 종료 코드만으로는 진단이 불가능하다(#612).

    **노출 범위는 호출 지점에 따라 다르다.** codex-worker container에는 token이
    마운트되지 않고 환경 allowlist에도 secret이 없어 저장소 소스에 한정된다. 반면
    리포트 작성이 도는 candidate-finalizer에는 push token과 API token이 파일로
    마운트돼 있어, Codex가 그것을 출력하면 이 tail에 실린다. 환경 변수로는 넘어가지
    않지만(`_temporary_environment`) 파일은 읽을 수 있고, 막는 것은 코드가 아니라
    지시문이다(`prompt.build_report_prompt`의 credential 규칙).
    """

    exit_code: int
    duration_ms: int
    stdout: str = ""
    stderr: str = ""
    # `--json` 스트림에서 뽑은 토큰 사용량. 이벤트를 한 번도 보지 못했으면 `None`이다
    # (구버전 CLI·조기 종료). "0 토큰"과 "모른다"는 다르므로 0으로 채우지 않는다.
    usage: CodexTokenUsage | None = None


class _UsageCollector:
    """`codex exec --json` 스트림에서 turn별 사용량을 누적한다(#742).

    tail이 아니라 **스트림**을 보는 이유는 `_RingBuffer`가 앞을 버리기 때문이다. 긴
    실행에서는 사용량 이벤트가 tail 밖으로 밀려날 수 있고, 그러면 값이 통째로 사라진다.
    파이프를 읽는 그 자리에서 한 번 훑으면 잘림과 무관해진다.

    한 줄이 상한을 넘도록 개행이 오지 않으면 그 줄을 버린다. Codex가 싣는 이벤트는
    한 줄이 짧고, 개행 없이 무한히 늘어나는 출력에 메모리를 내주지 않기 위함이다.
    """

    _MAX_PENDING_BYTES = 1024 * 1024
    _USAGE_EVENT_TYPE = "turn.completed"

    def __init__(self) -> None:
        self._pending = bytearray()
        self._input = 0
        self._cached_input = 0
        self._output = 0
        self._reasoning = 0
        self._turns = 0

    def feed(self, data: bytes) -> None:
        """파이프에서 읽은 청크를 줄 단위로 소비한다.

        청크 경계는 줄 경계와 무관하므로 남은 조각을 다음 청크와 이어 붙인다.
        """
        if not data:
            return
        self._pending.extend(data)
        while (index := self._pending.find(b"\n")) != -1:
            line = bytes(self._pending[:index])
            del self._pending[: index + 1]
            self._consume(line)
        if len(self._pending) > self._MAX_PENDING_BYTES:
            self._pending.clear()

    def _consume(self, line: bytes) -> None:
        """JSONL 한 줄에서 사용량 이벤트만 골라 누적한다."""
        stripped = line.strip()
        if not stripped.startswith(b"{"):
            return
        try:
            event = json.loads(stripped)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(event, dict) or event.get("type") != self._USAGE_EVENT_TYPE:
            return
        usage = event.get("usage")
        if not isinstance(usage, dict):
            return
        self._input += _non_negative_int(usage.get("input_tokens"))
        self._cached_input += _non_negative_int(usage.get("cached_input_tokens"))
        self._output += _non_negative_int(usage.get("output_tokens"))
        self._reasoning += _non_negative_int(usage.get("reasoning_output_tokens"))
        self._turns += 1

    def result(self) -> CodexTokenUsage | None:
        """사용량 이벤트를 한 번이라도 봤을 때만 누적값을 돌려준다."""
        if self._turns == 0:
            return None
        return CodexTokenUsage(
            input_tokens=self._input,
            cached_input_tokens=self._cached_input,
            output_tokens=self._output,
            reasoning_output_tokens=self._reasoning,
            turns=self._turns,
        )


def _non_negative_int(value: object) -> int:
    """사용량 필드를 음수 없는 정수로 읽는다. 해석할 수 없으면 0이다."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


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
    error: CodexWorkerError,
    stdout: _RingBuffer,
    stderr: _RingBuffer,
    usage: CodexTokenUsage | None = None,
) -> CodexWorkerError:
    """실패 사유는 그대로 두고 진단용 출력 tail과 사용량만 예외에 붙인다."""
    error.stdout = stdout.decode()
    error.stderr = stderr.decode()
    error.usage = usage
    return error


def _drain_pipe(
    pipe: BinaryIO, buffer: _RingBuffer, collector: _UsageCollector | None = None
) -> None:
    """child pipe를 끝까지 소비해 큰 출력이 Codex를 block하지 않게 한다.

    `collector`를 주면 같은 청크를 사용량 파서에도 흘린다. 파이프를 두 번 읽을 수 없어
    보관과 파싱이 같은 자리에서 일어나야 한다.
    """
    try:
        while chunk := pipe.read(8192):
            buffer.append(chunk)
            if collector is not None:
                collector.feed(chunk)
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


def _replace_regular_file(path: Path, content: bytes, mode: int) -> None:
    """경로에 있던 것을 지우고 새 regular file로만 쓴다.

    truncate 후 write가 아니라 unlink 후 `O_CREAT | O_EXCL`인 이유는 **symlink를 절대
    따라가지 않기 위해서다.** Codex는 `danger-full-access`로 돌아 실행 중 이 경로를
    다른 파일을 가리키는 symlink로 바꿔 놓을 수 있고, 그때 `write_bytes`는 링크 대상을
    덮어쓴다. 링크 자체를 먼저 지우면 그 경로가 사라지고, `O_EXCL`은 그 사이에 다시
    만들어진 경우까지 fail-closed로 끊는다.
    """
    path.unlink(missing_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "wb") as handle:
        os.fchmod(handle.fileno(), mode)
        handle.write(content)


def _restore_regular_file(path: Path, original: bytes | None, mode: int) -> None:
    """원본을 그대로 되돌리거나, 원본이 없었으면 경로를 비운다."""
    if original is None:
        path.unlink(missing_ok=True)
        return
    _replace_regular_file(path, original, mode)


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

    교체·복원 모두 `_replace_regular_file`을 쓴다. 실행 **전** 파일 종류 검사만으로는
    부족하기 때문이다 — Codex가 실행 도중 이 경로를 다른 파일을 가리키는 symlink로
    바꾸면, 뒤이은 복원이 그 링크 대상을 원본 `AGENTS.md` 내용으로 덮어쓴다.
    """
    path = repository / HARNESS_FILENAME
    try:
        status: os.stat_result | None = path.lstat()
    except FileNotFoundError:
        status = None
    except OSError as error:
        raise CodexWorkerError("harness_path_invalid") from error
    if status is not None and not stat.S_ISREG(status.st_mode):
        raise CodexWorkerError("harness_path_invalid")
    original = None if status is None else path.read_bytes()
    mode = _HARNESS_DEFAULT_MODE if status is None else stat.S_IMODE(status.st_mode)
    try:
        _replace_regular_file(path, content.encode("utf-8"), mode)
    except OSError as error:
        # 교체 도중 실패하면 원본이 이미 사라진 뒤일 수 있다. 되돌릴 수 있으면 되돌리고,
        # 그것마저 실패하면 아래 `harness_install_failed`가 stage를 끊는다.
        _restore_regular_file(path, original, mode)
        raise CodexWorkerError("harness_install_failed") from error
    try:
        yield
    finally:
        try:
            _restore_regular_file(path, original, mode)
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
        # 사람이 읽는 stdout은 총량 한 줄(`tokens used`)만 실어 input/cached/output
        # 분해가 없다(#742). `--json`은 turn마다 `usage`를 싣는 대신 출력을 JSONL로
        # 바꾸므로, 진단용으로 읽던 서술은 사용량 요약 로그가 대신한다(`phase2`).
        "--json",
        *(("--skip-git-repo-check",) if execution.skip_git_repo_check else ()),
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
                # Codex는 상속된 stdin을 "추가 입력"으로 읽는다(`Reading additional
                # input from stdin...`). container에서는 즉시 EOF지만, stdin이 열린
                # 채로 남는 환경에서는 timeout까지 매달린다. 지시문은 argv로만 준다.
                stdin=subprocess.DEVNULL,
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
        usage_collector = _UsageCollector()
        pipes = (process.stdout, process.stderr)
        readers = (
            threading.Thread(
                target=_drain_pipe,
                args=(process.stdout, stdout_buffer, usage_collector),
                daemon=True,
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
            _with_output(error, stdout_buffer, stderr_buffer, usage_collector.result())
            raise

    duration_ms = int((time.monotonic() - started_at) * 1000)
    return CodexRunResult(
        exit_code=exit_code,
        duration_ms=duration_ms,
        stdout=stdout_buffer.decode(),
        stderr=stderr_buffer.decode(),
        usage=usage_collector.result(),
    )


def run_codex_execution(execution: CodexExecution) -> CodexRunResult:
    """이미 정해진 지시문으로 Codex를 실행하고 종료 결과와 출력 tail을 반환한다."""
    _validate_execution(execution)
    return _execute_codex(execution)


def run_codex(
    run: CodexRunInput, *, budget: ResourceBudget | None = None
) -> CodexRunResult:
    """봉인된 clone에서 Codex 코드 수정 실행 하나를 수행한다.

    `.git` 봉인을 실행 전후로 대조하고, 실행 동안만 clone의 `AGENTS.md`를 executor 전용
    하네스 지침으로 바꾼다. 교체는 Codex 시작 직전, 복원은 `finally`다 — 그 시점의
    workspace가 verifier의 검증 baseline이므로 복원하지 않으면 하네스 파일이 candidate
    변경으로 잡혀 commit·push된다.
    """
    _validate_run(run)
    git_directory, sealed_git_metadata = _capture_protected_git_metadata(run.repository)
    with _harness_instructions(
        run.repository, build_harness_instructions(run.allowed_scope, budget)
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
        error.usage = result.usage
        raise error
    return result


def run_codex_for_workspace(
    state: ExecutorWorkspaceState,
    *,
    codex_home: Path,
    timeout_seconds: int,
    budget: ResourceBudget | None = None,
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
        ),
        budget=budget,
    )
