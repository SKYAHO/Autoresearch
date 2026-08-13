"""Executor candidate의 실제 Git 변경과 봉인 검증 명령을 승인한다.

[파이프라인]
workspace-preparer가 봉인 checkout을 만들고 Codex worker가 workspace 파일만 수정한 뒤,
finalizer가 candidate commit·push를 수행하기 전 변경 범위와 고정 검증을 판정하는 구간이다.

[기능]
working tree 또는 재시도 candidate commit의 실제 Git diff를 경로·mode·크기 정책으로
검사하고 candidate가 새로 도입한 credential·로컬 경로를 거부하며, 자격증명이 없는
allowlist 환경에서 diff check·Ruff·pytest를 순서대로 실행한다. **차단하는 것은 경로·크기
정책과 diff check·Ruff까지이고, pytest는 실행하되 거부 사유로 쓰지 않고 관측치로
반환한다**(#615).
working tree는 descriptor 기반 snapshot에서 검사하며 Stage 5가 재확인할 콘텐츠 지문과
staged tree 객체 ID를 함께 반환한다.

[비책임]
이슈/ref/workspace 준비(`workspace.py`), Codex 코드 수정(`codex_worker.py`), candidate의
commit·push·API 보고(Stage 5), Pod credential·volume 정책(Autoresearch-infra)은 담당하지
않는다.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import errno
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
import subprocess
from tempfile import TemporaryDirectory
from typing import Final

from applications.experiment_platform.executor.safety import contains_credential_value


_SHA_PATTERN: Final = re.compile(r"^[0-9a-f]{40}$")
_LFS_POINTER_PREFIX: Final = b"version https://git-lfs.github.com/spec/v1\n"
_FIXED_UV_PROJECT_ENVIRONMENT: Final = "/opt/autoresearch-venv"
_BASE_ALLOWED_PREFIXES: Final = ("autoresearch/", "tests/", "tools/")
# prod 모델 계약 파일의 경로. #754 재배치로 위치가 바뀌었는데, 이 검증은 저장소 트리가
# 아니라 **봉인된 base_dev_sha 워크스페이스**를 보므로 재배치 이전 실험은 여전히 옛
# 경로를 가진다. 둘 다 잡아야 어느 트리에서도 게이트가 산다.
_MODEL_CONTRACT_PATHS: Final = frozenset(
    {
        "src/features/model_contract.py",  # 봉인된 옛 트리
        "autoresearch/feature_engineering/model_contract.py",  # 재배치 후
    }
)
# 비차단 pytest의 출력 tail 상한. 실패한 pytest의 traceback은 수 MB까지 커질 수 있고,
# 이 값은 finalizer handoff JSON과 stage 로그에 그대로 실린다(#615).
_PYTEST_OUTPUT_TAIL_BYTES: Final = 64 * 1024
# 두 트리 세대를 모두 덮는다. 이 검사는 봉인된 워크스페이스에 적용되므로 #754 재배치
# 이전 트리는 `agent_orchestration/`·`proxy/`를, 이후 트리는 `applications/`를 가진다.
# 어느 쪽이든 **금지**이므로 양쪽을 나열하는 것이 안전하다 — 빠뜨린 쪽은 default-deny로
# 떨어지지만, 그러면 "왜 막혔는가"가 이 목록에서 읽히지 않는다.
_ALWAYS_FORBIDDEN_PREFIXES: Final = (
    ".git/",
    ".github/",
    ".claude/",
    "docs/",
    "deployment/",
    "applications/",  # 재배치 후 — 서빙·에이전트·proxy 전부
    "proxy/",  # 봉인된 옛 트리
    "agent_orchestration/",  # 봉인된 옛 트리
)
_ALLOWED_SCOPE_VALUES: Final = frozenset(
    {"prod_model_contract", "feast_definition", "promotion"}
)
_SYMLINK_MODE: Final = 0o120000
_SUBMODULE_MODE: Final = 0o160000
_GENERATED_DATA_SUFFIXES: Final = frozenset({".csv", ".pkl", ".parquet"})
_LOCAL_ABSOLUTE_PATH_PATTERNS: Final = (
    re.compile(r"(?<![A-Za-z0-9_])/(?:home|root|tmp|var|opt|mnt|workspace|Users)/[^\s'\"`]+"),
    re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:\\+(?:Users|home|tmp|workspace)\\+[^\s'\"`]+"),
)


class CandidateVerificationError(RuntimeError):
    """Candidate 전체를 거부하는 정제된 verifier 실패 사유다."""


@dataclass(frozen=True)
class CandidatePolicy:
    """봉인된 executor image가 소유하는 candidate 변경 상한과 조건부 scope다."""

    allowed_scope: tuple[str, ...] = ()
    max_changed_paths: int = 50
    max_text_diff_bytes: int = 1024 * 1024
    max_regular_file_bytes: int = 10 * 1024 * 1024


@dataclass(frozen=True)
class VerificationResult:
    """범위·차단 명령·finalizer handoff를 통과한 candidate 결과와 pytest 관측치다.

    `pytest_exit_code`·`pytest_output`은 **거부 사유가 아니라 기록**이다(#615). pytest는
    candidate와 무관한 실패로도 떨어지는데(환경 의존 테스트, baseline에서 이미 깨진 테스트),
    그것으로 candidate를 거부하면 실험이 학습·측정에 도달하지 못해 **아무 기록도 남지
    않는다.** 대신 결과를 그대로 실어 나른다 — 차단을 푸는 대신 관측을 남기지 않으면
    완화가 아니라 삭제가 된다.

    `pytest_output`은 `_PYTEST_OUTPUT_TAIL_BYTES` 상한의 최근 구간이며 전체가 아니다.
    """

    changed_paths: tuple[str, ...]
    content_fingerprint: str
    verified_tree_oid: str
    pytest_exit_code: int = 0
    pytest_output: str = ""


@dataclass(frozen=True)
class _TreeEntry:
    """정책 검사에 필요한 Git tree의 file mode와 blob object다."""

    mode: int
    object_id: str


@dataclass(frozen=True)
class _ChangedPath:
    """diff status가 보고한 현재·이전 경로 한 쌍이다."""

    current: str | None
    previous: str | None = None
    kind: str = "M"


def _verification_environment(temporary_root: Path) -> dict[str, str]:
    """Git·Ruff·pytest에 부모 credential을 상속하지 않는 실행 환경을 만든다."""
    home = temporary_root / "home"
    config_home = temporary_root / "config"
    cache_home = temporary_root / "cache"
    tmpdir = temporary_root / "tmp"
    for directory in (home, config_home, cache_home, tmpdir):
        directory.mkdir()
    return {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(config_home),
        "XDG_CACHE_HOME": str(cache_home),
        "TMPDIR": str(tmpdir),
        "PATH": os.environ.get("PATH", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "UV_PROJECT_ENVIRONMENT": _FIXED_UV_PROJECT_ENVIRONMENT,
    }


def _run_git(
    repository: Path,
    *arguments: str,
    environment: dict[str, str],
) -> bytes:
    """안전한 allowlist 환경에서 read-only Git 명령의 stdout만 반환한다."""
    try:
        return subprocess.run(
            ("git", "-C", str(repository), *arguments),
            check=True,
            capture_output=True,
            env=environment,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise CandidateVerificationError("git_invalid") from error


def _run_fixed_command(
    command: tuple[str, ...], *, cwd: Path, environment: dict[str, str]
) -> tuple[int, str]:
    """봉인된 verifier 명령 하나를 실행하고 종료 코드와 출력 tail을 함께 반환한다.

    차단 명령에서는 호출자가 출력을 버리지만, 비차단인 pytest는 이 출력이 유일한
    관측 수단이다(#615). 상한을 두는 이유는 실패한 pytest의 traceback이 수 MB까지
    커질 수 있어서다.
    """
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise CandidateVerificationError("verification_command_unavailable") from error
    merged = completed.stdout + completed.stderr
    tail = merged[-_PYTEST_OUTPUT_TAIL_BYTES:]
    return completed.returncode, tail.decode("utf-8", errors="replace")


def _decode_path(value: bytes) -> str:
    """Git의 NUL 구분 경로를 정책상 검증 가능한 UTF-8 경로로 바꾼다."""
    try:
        path = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise CandidateVerificationError("path_encoding") from error
    if not path or path.startswith("/") or "\\" in path:
        raise CandidateVerificationError("path_invalid")
    parts = Path(path).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise CandidateVerificationError("path_invalid")
    return path


def _validate_input(
    repository: Path, base_sha: str, candidate_sha: str | None, policy: CandidatePolicy
) -> None:
    """filesystem·SHA·정책 값을 command 실행 전 fail-closed로 검증한다."""
    if not repository.is_absolute() or not repository.is_dir() or repository.is_symlink():
        raise CandidateVerificationError("repository_invalid")
    if _SHA_PATTERN.fullmatch(base_sha) is None:
        raise CandidateVerificationError("base_sha_invalid")
    if candidate_sha is not None and _SHA_PATTERN.fullmatch(candidate_sha) is None:
        raise CandidateVerificationError("candidate_sha_invalid")
    if (
        len(set(policy.allowed_scope)) != len(policy.allowed_scope)
        or any(scope not in _ALLOWED_SCOPE_VALUES for scope in policy.allowed_scope)
    ):
        raise CandidateVerificationError("allowed_scope_invalid")
    if (
        type(policy.max_changed_paths) is not int
        or type(policy.max_text_diff_bytes) is not int
        or type(policy.max_regular_file_bytes) is not int
        or policy.max_changed_paths < 1
        or policy.max_text_diff_bytes < 0
        or policy.max_regular_file_bytes < 1
    ):
        raise CandidateVerificationError("policy_invalid")


def _tree_entries(
    repository: Path, revision: str, *, environment: dict[str, str]
) -> dict[str, _TreeEntry]:
    """revision 전체 tree를 읽어 삭제·rename도 file mode로 판단할 수 있게 한다."""
    raw = _run_git(repository, "ls-tree", "-r", "-z", revision, environment=environment)
    entries: dict[str, _TreeEntry] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise CandidateVerificationError("git_invalid")
        try:
            mode = int(fields[0], 8)
            object_id = fields[2].decode("ascii", errors="strict")
        except (UnicodeDecodeError, ValueError) as error:
            raise CandidateVerificationError("git_invalid") from error
        entries[_decode_path(raw_path)] = _TreeEntry(mode=mode, object_id=object_id)
    return entries


def _index_entries(
    repository: Path, *, environment: dict[str, str]
) -> dict[str, _TreeEntry]:
    """working tree의 staged gitlink·symlink도 놓치지 않도록 index mode를 읽는다."""
    raw = _run_git(repository, "ls-files", "-s", "-z", environment=environment)
    entries: dict[str, _TreeEntry] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            raise CandidateVerificationError("git_invalid")
        try:
            mode = int(fields[0], 8)
            object_id = fields[1].decode("ascii", errors="strict")
        except (UnicodeDecodeError, ValueError) as error:
            raise CandidateVerificationError("git_invalid") from error
        entries[_decode_path(raw_path)] = _TreeEntry(mode=mode, object_id=object_id)
    return entries


def _name_status_changes(
    repository: Path,
    base_sha: str,
    candidate_sha: str | None,
    *,
    environment: dict[str, str],
    staged_only: bool = False,
) -> list[_ChangedPath]:
    """Git diff와 untracked 목록을 합쳐 policy가 검사할 모든 경로를 수집한다."""
    revisions = (base_sha,) if candidate_sha is None else (base_sha, candidate_sha)
    raw_diffs: list[bytes] = []
    if not staged_only:
        raw_diffs.append(_run_git(
            repository,
            "diff",
            "--name-status",
            "-z",
            "-M",
            "-C",
            "--find-copies-harder",
            *revisions,
            environment=environment,
        ))
    if candidate_sha is None:
        raw_diffs.append(_run_git(
            repository,
            "diff",
            "--cached",
            "--name-status",
            "-z",
            "-M",
            "-C",
            "--find-copies-harder",
            base_sha,
            environment=environment,
        ))
    changes: list[_ChangedPath] = []
    for raw in raw_diffs:
        fields = [field for field in raw.split(b"\0") if field]
        index = 0
        while index < len(fields):
            status = fields[index].decode("ascii", errors="strict")
            index += 1
            if not status or status[0] not in {"A", "C", "D", "M", "R", "T"}:
                raise CandidateVerificationError("git_invalid")
            if status[0] in {"R", "C"}:
                if index + 1 >= len(fields):
                    raise CandidateVerificationError("git_invalid")
                previous = _decode_path(fields[index])
                current = _decode_path(fields[index + 1])
                index += 2
                changes.append(
                    _ChangedPath(current=current, previous=previous, kind=status[0])
                )
            else:
                if index >= len(fields):
                    raise CandidateVerificationError("git_invalid")
                path = _decode_path(fields[index])
                index += 1
                changes.append(
                    _ChangedPath(
                        current=None if status[0] == "D" else path,
                        previous=path if status[0] == "D" else None,
                        kind=status[0],
                    )
                )

    if candidate_sha is None and not staged_only:
        # status는 요구되는 working-tree 진단이고, ls-files는 directory로 축약되지 않은
        # untracked file 목록을 준다.
        _run_git(repository, "status", "--porcelain=v1", "-z", environment=environment)
        untracked = _run_git(
            repository,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            environment=environment,
        )
        tracked_paths = {change.current for change in changes if change.current is not None}
        for raw_path in untracked.split(b"\0"):
            if raw_path:
                path = _decode_path(raw_path)
                if path not in tracked_paths:
                    changes.append(_ChangedPath(current=path, kind="?"))
    return changes


def _tree_has(repository: Path, relative: str) -> bool:
    """봉인된 워크스페이스에 그 경로가 실제로 있는지 본다.

    #754 재배치는 여러 PR로 나뉘어 들어가므로 "재배치 이전/이후" 같은 **한 개의 세대
    플래그로 가르면 중간 SHA에서 틀린다.** 예를 들어 Task 1만 머지된 시점에 봉인된 트리는
    `autoresearch/cli.py`를 가지면서 `applications/`는 아직 없다. 그래서 각 판단은 프록시가
    아니라 **그 판단이 실제로 의존하는 경로**를 직접 본다.
    """
    return (repository / relative).exists()


def _path_is_allowed(path: str, policy: CandidatePolicy, *, legacy_tree: bool) -> bool:
    """기본 allowlist와 Issue Form의 조건부 scope를 path 하나에 적용한다."""
    if path in {".env", "pyproject.toml", "uv.lock"} or path.startswith(".env."):
        return False
    if path.startswith(_ALWAYS_FORBIDDEN_PREFIXES):
        return False
    if path in _MODEL_CONTRACT_PATHS:
        return "prod_model_contract" in policy.allowed_scope
    if legacy_tree and path.startswith("src/"):
        # 전환 기간 허용. 봉인된 옛 base_dev_sha 트리에서 만들어진 실험은 워크스페이스에
        # 여전히 src/ 를 가지므로, 여기서 막으면 진행 중 실험이 전부 forbidden_path 로
        # 거부된다. **옛 트리에서만** 연다 — 재배치 이후 트리에서 src/ 아래 파일이 새로
        # 생기는 것은 정상이 아니므로 그때는 막는 쪽이 맞다 (#754).
        return True
    if path.startswith(_BASE_ALLOWED_PREFIXES):
        return True
    return path.startswith("feature_repo/") and "feast_definition" in policy.allowed_scope


def _content_is_forbidden(content: bytes) -> bool:
    """텍스트 파일의 실제 credential 형식과 로컬 절대 경로만 값 없이 감지한다."""
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False
    return contains_credential_value(text) or any(
        pattern.search(text) is not None for pattern in _LOCAL_ABSOLUTE_PATH_PATTERNS
    )


def _introduced_content(base_content: bytes | None, candidate_content: bytes) -> bytes:
    """baseline에 없던 candidate line만 원래 순서대로 반환한다."""
    if base_content is None:
        return candidate_content
    remaining_base_lines = Counter(base_content.splitlines(keepends=True))
    introduced_lines: list[bytes] = []
    for line in candidate_content.splitlines(keepends=True):
        if remaining_base_lines[line] > 0:
            remaining_base_lines[line] -= 1
        else:
            introduced_lines.append(line)
    return b"".join(introduced_lines)


def _validate_file_content(
    path: str,
    content: bytes,
    *,
    base_content: bytes | None,
) -> None:
    """생성 데이터와 새 sensitive content를 path·bytes 계약으로 fail-closed 처리한다."""
    if Path(path).suffix.lower() in _GENERATED_DATA_SUFFIXES:
        raise CandidateVerificationError("generated_data")
    if _content_is_forbidden(_introduced_content(base_content, content)):
        raise CandidateVerificationError("content_forbidden")


def _read_blob(
    repository: Path, object_id: str, *, environment: dict[str, str]
) -> bytes:
    """tree가 가리키는 blob만 읽어 LFS pointer를 확인한다."""
    return _run_git(repository, "cat-file", "blob", object_id, environment=environment)


def _text_diff_size(
    repository: Path,
    base_sha: str,
    candidate_sha: str | None,
    untracked_paths: tuple[str, ...],
    *,
    environment: dict[str, str],
) -> int:
    """patch의 실제 추가·삭제 text bytes와 untracked text bytes를 계산한다."""
    revisions = (base_sha,) if candidate_sha is None else (base_sha, candidate_sha)
    patch = _run_git(
        repository,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--unified=0",
        *revisions,
        environment=environment,
    )
    total = 0
    for line in patch.splitlines(keepends=True):
        if line.startswith((b"+++", b"---")):
            continue
        if line.startswith((b"+", b"-")):
            total += len(line) - 1
    if candidate_sha is None:
        for path in untracked_paths:
            candidate_path = repository / path
            try:
                content = candidate_path.read_bytes()
            except OSError as error:
                raise CandidateVerificationError("file_unreadable") from error
            if b"\0" not in content:
                total += len(content)
    return total


def _validate_path_files(
    repository: Path,
    changes: list[_ChangedPath],
    base_sha: str,
    base_entries: dict[str, _TreeEntry],
    candidate_entries: dict[str, _TreeEntry],
    index_entries: dict[str, _TreeEntry] | None,
    candidate_sha: str | None,
    policy: CandidatePolicy,
    *,
    environment: dict[str, str],
) -> tuple[str, ...]:
    """경로·mode·LFS·파일 크기를 candidate 전체에 대해 fail-closed로 검사한다."""
    # `src/` 허용은 **봉인된 base 트리**에 그 경로가 있었을 때만 연다 (#754).
    # 워크스페이스를 보면 순환이 된다 — candidate 가 `src/foo.py` 를 만들면 그 사실만으로
    # 자기 허용을 열어버린다. base_entries 는 base_sha 시점의 트리라 candidate 가 바꿀 수 없다.
    legacy_tree = any(path.startswith("src/") for path in base_entries)
    policy_paths: list[str] = []
    untracked_paths: list[str] = []
    for change in changes:
        for path in (change.previous, change.current):
            if path is None:
                continue
            policy_paths.append(path)
            if not _path_is_allowed(path, policy, legacy_tree=legacy_tree):
                raise CandidateVerificationError("forbidden_path")
            if Path(path).suffix.lower() in _GENERATED_DATA_SUFFIXES:
                raise CandidateVerificationError("generated_data")
        if candidate_sha is None and change.previous is None and change.current is not None:
            if change.current not in (index_entries or {}):
                untracked_paths.append(change.current)

    unique_paths = tuple(sorted(set(policy_paths)))
    if not unique_paths:
        raise CandidateVerificationError("no_changes")
    if len(unique_paths) > policy.max_changed_paths:
        raise CandidateVerificationError("too_many_paths")

    for path in unique_paths:
        entries = [base_entries.get(path), candidate_entries.get(path)]
        if index_entries is not None:
            entries.append(index_entries.get(path))
        if any(entry is not None and entry.mode == _SYMLINK_MODE for entry in entries):
            raise CandidateVerificationError("symlink")
        if any(entry is not None and entry.mode == _SUBMODULE_MODE for entry in entries):
            raise CandidateVerificationError("submodule")

        if candidate_sha is None:
            current = repository / path
            base_entry = base_entries.get(path)
            base_content = None
            if base_entry is not None:
                base_content = _read_blob(
                    repository, base_entry.object_id, environment=environment
                )
                if base_content.startswith(_LFS_POINTER_PREFIX):
                    raise CandidateVerificationError("lfs_pointer")
            try:
                file_status = current.lstat()
            except FileNotFoundError:
                file_status = None
            except OSError as error:
                raise CandidateVerificationError("file_unreadable") from error
            if file_status is not None:
                if stat.S_ISLNK(file_status.st_mode):
                    raise CandidateVerificationError("symlink")
                if not stat.S_ISREG(file_status.st_mode):
                    raise CandidateVerificationError("file_type_invalid")
                if file_status.st_size > policy.max_regular_file_bytes:
                    raise CandidateVerificationError("file_too_large")
                try:
                    content = current.read_bytes()
                except OSError as error:
                    raise CandidateVerificationError("file_unreadable") from error
                if content.startswith(_LFS_POINTER_PREFIX):
                    raise CandidateVerificationError("lfs_pointer")
                _validate_file_content(
                    path,
                    content,
                    base_content=base_content,
                )
        else:
            base_entry, candidate_entry = entries[:2]
            base_content = None
            if base_entry is not None:
                base_content = _read_blob(
                    repository, base_entry.object_id, environment=environment
                )
                if base_content.startswith(_LFS_POINTER_PREFIX):
                    raise CandidateVerificationError("lfs_pointer")
            if candidate_entry is not None:
                content = _read_blob(
                    repository, candidate_entry.object_id, environment=environment
                )
                if content.startswith(_LFS_POINTER_PREFIX):
                    raise CandidateVerificationError("lfs_pointer")
                _validate_file_content(
                    path,
                    content,
                    base_content=base_content,
                )
                if len(content) > policy.max_regular_file_bytes:
                    raise CandidateVerificationError("file_too_large")

    text_diff_bytes = _text_diff_size(
        repository,
        base_sha,
        candidate_sha,
        tuple(untracked_paths),
        environment=environment,
    )
    if text_diff_bytes > policy.max_text_diff_bytes:
        raise CandidateVerificationError("text_diff_too_large")
    return unique_paths


def _ruff_targets(repository: Path) -> tuple[str, ...]:
    """이 워크스페이스에서 lint할 디렉터리를 고른다.

    ruff는 **없는 경로를 인자로 받으면 exit 1** 이므로, 목록이 트리와 어긋나면 모든
    candidate가 `ruff_failed`로 거부된다. 이 명령은 봉인된 `base_dev_sha` 워크스페이스에서
    돌고 이미지 버전과 트리가 어긋날 수 있으므로, 고정 목록을 쓸 수 없다.

    `applications/`의 존재를 직접 본다 — "재배치 이전/이후" 같은 세대 플래그로 가르면
    #754가 여러 PR로 나뉘어 들어가는 중간 SHA에서 틀린다.

    `applications/`를 가진 봉인 트리만 남으면 분기를 지우고 `applications`로 고정한다.
    """
    platform_target = (
        "applications" if _tree_has(repository, "applications") else "agent_orchestration"
    )
    return (platform_target, "autoresearch", "tests", "tools")


def _run_sealed_commands(
    repository: Path,
    base_sha: str,
    candidate_sha: str | None,
    *,
    environment: dict[str, str],
) -> tuple[int, str]:
    """diff check와 Ruff로 candidate를 차단하고, pytest는 관측치로만 돌려준다.

    pytest가 차단하지 않는 이유는 `VerificationResult` docstring에 있다(#615). 순서는
    유지한다 — 차단 명령이 먼저 실패하면 8분짜리 pytest를 돌릴 이유가 없다.
    """
    revisions = (base_sha,) if candidate_sha is None else (base_sha, candidate_sha)
    blocking_commands = (
        (("git", "diff", "--check", *revisions), "diff_check_failed"),
        (("uv", "run", "--no-sync", "ruff", "check", *_ruff_targets(repository)), "ruff_failed"),
    )
    for command, error_code in blocking_commands:
        exit_code, _ = _run_fixed_command(command, cwd=repository, environment=environment)
        if exit_code != 0:
            raise CandidateVerificationError(error_code)
    return _run_fixed_command(
        ("uv", "run", "--no-sync", "python", "-m", "pytest"),
        cwd=repository,
        environment=environment,
    )


def _materialize_candidate(
    repository: Path,
    candidate_sha: str,
    temporary_root: Path,
    *,
    environment: dict[str, str],
) -> Path:
    """재시도 candidate를 dirty source worktree와 분리한 임시 clone으로 검증한다."""
    checkout = temporary_root / "candidate-checkout"
    try:
        subprocess.run(
            (
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "clone",
                "--shared",
                "--no-checkout",
                str(repository),
                str(checkout),
            ),
            check=True,
            capture_output=True,
            env=environment,
        )
        _run_git(checkout, "checkout", "--detach", candidate_sha, environment=environment)
    except (OSError, subprocess.CalledProcessError) as error:
        raise CandidateVerificationError("candidate_checkout_invalid") from error
    return checkout


def _read_regular_file_no_follow(path: Path) -> tuple[int, bytes]:
    """O_NOFOLLOW descriptor에서 regular file의 mode·bytes를 함께 읽는다."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise CandidateVerificationError("file_missing") from error
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise CandidateVerificationError("symlink") from error
        raise CandidateVerificationError("file_unreadable") from error
    try:
        file_status = os.fstat(descriptor)
        if not stat.S_ISREG(file_status.st_mode):
            raise CandidateVerificationError("file_type_invalid")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 8192):
            chunks.append(chunk)
        return stat.S_IMODE(file_status.st_mode), b"".join(chunks)
    except OSError as error:
        raise CandidateVerificationError("file_unreadable") from error
    finally:
        os.close(descriptor)


def _working_tree_fingerprint(
    repository: Path, base_sha: str, changes: list[_ChangedPath]
) -> str:
    """Stage 5가 commit할 원본 candidate tree의 경로·mode·bytes digest를 계산한다."""
    digest = sha256()
    digest.update(b"autoresearch-executor-candidate-v1\0")
    digest.update(base_sha.encode("ascii"))
    digest.update(b"\0")
    for change in sorted(changes, key=lambda item: (item.kind, item.previous or "", item.current or "")):
        digest.update(change.kind.encode("ascii"))
        digest.update(b"\0")
        for path in (change.previous, change.current):
            digest.update((path or "").encode("utf-8"))
            digest.update(b"\0")
    paths = sorted(
        {path for change in changes for path in (change.previous, change.current) if path}
    )
    for path in paths:
        target = repository / path
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        try:
            mode, content = _read_regular_file_no_follow(target)
        except CandidateVerificationError as error:
            if str(error) != "file_missing":
                raise
            digest.update(b"missing\0")
            continue
        digest.update(b"regular\0")
        digest.update(oct(mode).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(len(content)).encode("ascii"))
        digest.update(b"\0")
        digest.update(content)
    return digest.hexdigest()


def _materialize_working_tree(
    repository: Path,
    base_sha: str,
    changes: list[_ChangedPath],
    temporary_root: Path,
    *,
    environment: dict[str, str],
) -> Path:
    """원본 working tree를 건드리지 않고 finalizer와 같은 candidate tree snapshot을 만든다."""
    snapshot = temporary_root / "working-tree-snapshot"
    try:
        subprocess.run(
            (
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "clone",
                "--shared",
                "--no-checkout",
                str(repository),
                str(snapshot),
            ),
            check=True,
            capture_output=True,
            env=environment,
        )
        _run_git(snapshot, "checkout", "--detach", base_sha, environment=environment)
        for change in changes:
            if change.previous is not None and change.kind in {"D", "R"}:
                (snapshot / change.previous).unlink(missing_ok=True)
            if change.current is None:
                continue
            source = repository / change.current
            target = snapshot / change.current
            mode, content = _read_regular_file_no_follow(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            target.chmod(mode)
    except (OSError, subprocess.CalledProcessError) as error:
        raise CandidateVerificationError("working_tree_snapshot_invalid") from error
    return snapshot


def _stage_snapshot_and_write_tree(
    repository: Path, *, environment: dict[str, str]
) -> str:
    """격리 snapshot에 finalizer와 같은 index를 만들고 tree 객체 ID를 반환한다."""
    _run_git(repository, "add", "--all", environment=environment)
    return _write_staged_tree_oid(repository, environment=environment)


def _write_staged_tree_oid(repository: Path, *, environment: dict[str, str]) -> str:
    """이미 stage된 index의 tree 객체 ID를 검증 가능한 SHA 형식으로 읽는다."""
    tree_oid = _run_git(repository, "write-tree", environment=environment).strip()
    try:
        value = tree_oid.decode("ascii", errors="strict")
    except UnicodeDecodeError as error:
        raise CandidateVerificationError("tree_oid_invalid") from error
    if _SHA_PATTERN.fullmatch(value) is None:
        raise CandidateVerificationError("tree_oid_invalid")
    return value


def current_working_tree_verification(
    repository: Path, base_sha: str
) -> tuple[tuple[str, ...], str]:
    """Stage 5가 Stage 4의 working-tree handoff를 같은 규칙으로 재계산한다.

    공개 finalizer는 이 helper가 반환한 경로 집합과 콘텐츠 지문을 `VerificationResult`와
    비교해 검증 뒤 변경된 working tree를 commit하지 않는다.
    """
    _validate_input(repository, base_sha, None, CandidatePolicy())
    with TemporaryDirectory(prefix="executor-verifier-handoff-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        environment = _verification_environment(temporary_root)
        working_tree_changes = _name_status_changes(
            repository, base_sha, None, environment=environment
        )
        snapshot = _materialize_working_tree(
            repository,
            base_sha,
            working_tree_changes,
            temporary_root,
            environment=environment,
        )
        _stage_snapshot_and_write_tree(snapshot, environment=environment)
        changes = _name_status_changes(
            snapshot,
            base_sha,
            None,
            environment=environment,
            staged_only=True,
        )
        changed_paths = tuple(
            sorted({path for change in changes for path in (change.previous, change.current) if path})
        )
        return changed_paths, _working_tree_fingerprint(snapshot, base_sha, changes)


def write_staged_tree_oid(repository: Path) -> str:
    """현재 Git index의 tree OID를 Stage 4와 동일한 SHA 검증으로 반환한다.

    `git add --all`의 실행 책임은 finalizer에 남겨, 해당 명령에만 hooks 차단 설정을
    적용할 수 있게 한다.
    """
    _validate_input(repository, "0" * 40, None, CandidatePolicy())
    with TemporaryDirectory(prefix="executor-verifier-tree-") as temporary_directory:
        return _write_staged_tree_oid(
            repository,
            environment=_verification_environment(Path(temporary_directory)),
        )


def _assert_original_working_tree_unchanged(
    repository: Path,
    expected_changes: list[_ChangedPath],
    expected_fingerprint: str,
    base_sha: str,
    *,
    environment: dict[str, str],
) -> None:
    """snapshot과 verifier 반환 시점 모두 original candidate tree가 같음을 증명한다."""
    current_changes = _name_status_changes(
        repository, base_sha, None, environment=environment
    )
    if current_changes != expected_changes or (
        _working_tree_fingerprint(repository, base_sha, current_changes) != expected_fingerprint
    ):
        raise CandidateVerificationError("working_tree_changed")


def verify_candidate(
    repository: Path,
    base_sha: str,
    candidate_sha: str | None,
    policy: CandidatePolicy,
) -> VerificationResult:
    """실제 Git candidate를 정책과 봉인 명령으로 검사해 finalizer 입력을 반환한다."""
    _validate_input(repository, base_sha, candidate_sha, policy)
    with TemporaryDirectory(prefix="executor-verifier-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        environment = _verification_environment(temporary_root)
        _run_git(
            repository,
            "rev-parse",
            "--verify",
            f"{base_sha}^{{commit}}",
            environment=environment,
        )
        if candidate_sha is not None:
            _run_git(
                repository,
                "rev-parse",
                "--verify",
                f"{candidate_sha}^{{commit}}",
                environment=environment,
            )
        if candidate_sha is None:
            original_changes = _name_status_changes(
                repository, base_sha, None, environment=environment
            )
            _validate_path_files(
                repository,
                original_changes,
                base_sha,
                _tree_entries(repository, base_sha, environment=environment),
                _tree_entries(repository, base_sha, environment=environment),
                _index_entries(repository, environment=environment),
                None,
                policy,
                environment=environment,
            )
            original_fingerprint = _working_tree_fingerprint(
                repository, base_sha, original_changes
            )
            verification_repository = _materialize_working_tree(
                repository,
                base_sha,
                original_changes,
                temporary_root,
                environment=environment,
            )
            _assert_original_working_tree_unchanged(
                repository,
                original_changes,
                original_fingerprint,
                base_sha,
                environment=environment,
            )
            verified_tree_oid = _stage_snapshot_and_write_tree(
                verification_repository, environment=environment
            )
            snapshot_changes = _name_status_changes(
                verification_repository,
                base_sha,
                None,
                environment=environment,
                staged_only=True,
            )
            changed_paths = _validate_path_files(
                verification_repository,
                snapshot_changes,
                base_sha,
                _tree_entries(
                    verification_repository, base_sha, environment=environment
                ),
                _tree_entries(
                    verification_repository, base_sha, environment=environment
                ),
                _index_entries(verification_repository, environment=environment),
                None,
                policy,
                environment=environment,
            )
            content_fingerprint = _working_tree_fingerprint(
                verification_repository, base_sha, snapshot_changes
            )
            _assert_original_working_tree_unchanged(
                repository,
                original_changes,
                original_fingerprint,
                base_sha,
                environment=environment,
            )
        else:
            verification_repository = _materialize_candidate(
                repository,
                candidate_sha,
                temporary_root,
                environment=environment,
            )
            changes = _name_status_changes(
                verification_repository,
                base_sha,
                candidate_sha,
                environment=environment,
            )
            changed_paths = _validate_path_files(
                verification_repository,
                changes,
                base_sha,
                _tree_entries(verification_repository, base_sha, environment=environment),
                _tree_entries(
                    verification_repository, candidate_sha, environment=environment
                ),
                None,
                candidate_sha,
                policy,
                environment=environment,
            )
            verified_tree_oid = _run_git(
                verification_repository,
                "rev-parse",
                f"{candidate_sha}^{{tree}}",
                environment=environment,
            ).decode("ascii").strip()
            if _stage_snapshot_and_write_tree(
                verification_repository, environment=environment
            ) != verified_tree_oid:
                raise CandidateVerificationError("candidate_tree_mismatch")
            content_fingerprint = _working_tree_fingerprint(
                verification_repository, base_sha, changes
            )
        pytest_exit_code, pytest_output = _run_sealed_commands(
            verification_repository,
            base_sha,
            candidate_sha,
            environment=environment,
        )
        if candidate_sha is None:
            _assert_original_working_tree_unchanged(
                repository,
                original_changes,
                original_fingerprint,
                base_sha,
                environment=environment,
            )
    return VerificationResult(
        changed_paths=changed_paths,
        content_fingerprint=content_fingerprint,
        verified_tree_oid=verified_tree_oid,
        pytest_exit_code=pytest_exit_code,
        pytest_output=pytest_output,
    )
