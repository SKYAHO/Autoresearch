"""Candidate verifier의 실제 Git 정책과 고정 명령 실행 경계를 검증한다.

전체 executor 파이프라인에서 Codex worker 다음, finalizer 이전의 검증 단계다.
임시 Git 저장소에서 변경 경로·파일 mode·diff 크기를 실제로 만들고, 느린 Ruff/pytest만
좁은 command runner double로 대체해 verifier의 승인·거부 계약을 관찰한다.
"""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from agent_orchestration.executor import verifier
from agent_orchestration.executor.verifier import (
    CandidatePolicy,
    CandidateVerificationError,
    verify_candidate,
)


_BASE_SHA = "a" * 40


def _git(repository: Path, *arguments: str) -> str:
    """테스트 fixture의 실제 Git 상태를 만들거나 읽는다."""
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    """기본 허용 경로 하나를 가진 독립 임시 Git repository를 준비한다."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Candidate verifier test")
    _git(repository, "config", "user.email", "candidate-verifier@example.invalid")
    allowed = repository / "autoresearch" / "candidate.py"
    allowed.parent.mkdir()
    allowed.write_text("BASE = 1\n", encoding="utf-8")
    _git(repository, "add", "autoresearch/candidate.py")
    _git(repository, "commit", "-m", "base")
    return repository, _git(repository, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def successful_fixed_commands(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple[str, ...], Path, dict[str, str]]]:
    """정책 테스트에서는 실제 Git 외의 고정 Ruff/pytest 실행만 성공시킨다."""
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []

    def fake_command(
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> int:
        calls.append((command, cwd, environment))
        return 0

    monkeypatch.setattr(verifier, "_run_fixed_command", fake_command)
    return calls


def _verify(repository: Path, base_sha: str, **policy: object) -> tuple[str, ...]:
    """기본 정책으로 working tree candidate를 검증한다."""
    result = verify_candidate(
        repository=repository,
        base_sha=base_sha,
        candidate_sha=None,
        policy=CandidatePolicy(**policy),
    )
    return result.changed_paths


def test_allowed_working_tree_change_is_approved_and_reports_path(
    tmp_path: Path,
) -> None:
    """`autoresearch/**` 변경을 누락하면 정상 candidate도 finalizer로 못 넘어간다."""
    repository, base_sha = _repository(tmp_path)
    (repository / "autoresearch" / "candidate.py").write_text("BASE = 2\n", encoding="utf-8")

    assert _verify(repository, base_sha) == ("autoresearch/candidate.py",)


@pytest.mark.parametrize(
    "path",
    ("docs/unsafe.md", "agent_orchestration/unsafe.py", ".env.example"),
)
def test_forbidden_path_rejects_the_whole_candidate(tmp_path: Path, path: str) -> None:
    """금지 경로를 허용하면 Codex가 verifier·배포·자격증명 경계를 바꿀 수 있다."""
    repository, base_sha = _repository(tmp_path)
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("unsafe\n", encoding="utf-8")

    with pytest.raises(CandidateVerificationError, match="forbidden_path"):
        _verify(repository, base_sha)


def test_model_contract_requires_its_explicit_scope(tmp_path: Path) -> None:
    """특별 모델 계약 파일을 기본 src allowlist가 열면 의도치 않은 계약 변경을 허용한다."""
    repository, base_sha = _repository(tmp_path)
    target = repository / "src" / "features" / "model_contract.py"
    target.parent.mkdir(parents=True)
    target.write_text("CONTRACT = 2\n", encoding="utf-8")

    with pytest.raises(CandidateVerificationError, match="forbidden_path"):
        _verify(repository, base_sha)

    assert _verify(repository, base_sha, allowed_scope=("prod_model_contract",)) == (
        "src/features/model_contract.py",
    )


def test_feast_definition_scope_opens_only_feature_repo(tmp_path: Path) -> None:
    """Feast scope를 무시하면 승인된 feature definition 변경도 candidate가 될 수 없다."""
    repository, base_sha = _repository(tmp_path)
    target = repository / "feature_repo" / "features.py"
    target.parent.mkdir()
    target.write_text("FEATURES = ()\n", encoding="utf-8")

    with pytest.raises(CandidateVerificationError, match="forbidden_path"):
        _verify(repository, base_sha)

    assert _verify(repository, base_sha, allowed_scope=("feast_definition",)) == (
        "feature_repo/features.py",
    )


def test_empty_candidate_is_rejected(tmp_path: Path) -> None:
    """변경 없는 실행을 통과시키면 finalizer가 빈 candidate commit을 만들 수 있다."""
    repository, base_sha = _repository(tmp_path)

    with pytest.raises(CandidateVerificationError, match="no_changes"):
        _verify(repository, base_sha)


@pytest.mark.parametrize(
    ("count", "expected"), ((50, None), (51, "too_many_paths")))
def test_changed_path_limit_counts_untracked_files(
    tmp_path: Path, count: int, expected: str | None
) -> None:
    """untracked 파일을 세지 않으면 대량 candidate가 path 상한을 우회할 수 있다."""
    repository, base_sha = _repository(tmp_path)
    target_directory = repository / "tools" / "generated"
    target_directory.mkdir(parents=True)
    for index in range(count):
        (target_directory / f"file-{index:02d}.py").write_text("VALUE = 1\n", encoding="utf-8")

    if expected is None:
        assert len(_verify(repository, base_sha)) == 50
    else:
        with pytest.raises(CandidateVerificationError, match=expected):
            _verify(repository, base_sha)


@pytest.mark.parametrize(
    ("size", "expected"),
    ((1024 * 1024 - 1, None), (1024 * 1024, "text_diff_too_large")),
)
def test_textual_diff_limit_is_enforced_at_one_mib(
    tmp_path: Path, size: int, expected: str | None
) -> None:
    """diff bytes 계산이 한 byte 느슨하면 검토·실행 비용 상한이 무너진다."""
    repository, base_sha = _repository(tmp_path)
    target = repository / "autoresearch" / "candidate.py"
    target.write_text("", encoding="utf-8")
    _git(repository, "add", "autoresearch/candidate.py")
    _git(repository, "commit", "-m", "empty text fixture")
    base_sha = _git(repository, "rev-parse", "HEAD")
    target.write_text("x" * size + "\n", encoding="utf-8")

    if expected is None:
        assert _verify(repository, base_sha) == ("autoresearch/candidate.py",)
    else:
        with pytest.raises(CandidateVerificationError, match=expected):
            _verify(repository, base_sha)


@pytest.mark.parametrize(
    ("size", "expected"),
    ((10 * 1024 * 1024, None), (10 * 1024 * 1024 + 1, "file_too_large")),
)
def test_regular_file_limit_is_enforced_at_ten_mib(
    tmp_path: Path, size: int, expected: str | None
) -> None:
    """일반 파일 크기를 확인하지 않으면 binary payload가 candidate로 들어갈 수 있다."""
    repository, base_sha = _repository(tmp_path)
    target = repository / "tools" / "payload.bin"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(b"\0" + b"x" * (size - 1))

    if expected is None:
        assert _verify(repository, base_sha) == ("tools/payload.bin",)
    else:
        with pytest.raises(CandidateVerificationError, match=expected):
            _verify(repository, base_sha)


def test_symlink_and_submodule_modes_are_rejected(tmp_path: Path) -> None:
    """symlink와 gitlink를 일반 파일로 취급하면 workspace 경계를 탈출할 수 있다."""
    repository, base_sha = _repository(tmp_path)
    link = repository / "tools" / "link"
    link.parent.mkdir(exist_ok=True)
    link.symlink_to("/etc/passwd")

    with pytest.raises(CandidateVerificationError, match="symlink"):
        _verify(repository, base_sha)

    link.unlink()
    _git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        "160000," + "b" * 40 + ",tools/submodule",
    )
    with pytest.raises(CandidateVerificationError, match="submodule"):
        _verify(repository, base_sha)


def test_lfs_pointer_is_rejected(tmp_path: Path) -> None:
    """LFS pointer를 보통 text로 허용하면 verifier image 밖의 fetch를 유도할 수 있다."""
    repository, base_sha = _repository(tmp_path)
    (repository / "autoresearch" / "candidate.py").write_text(
        "version https://git-lfs.github.com/spec/v1\noid sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )

    with pytest.raises(CandidateVerificationError, match="lfs_pointer"):
        _verify(repository, base_sha)


def test_rename_checks_the_previous_and_new_paths(tmp_path: Path) -> None:
    """rename의 이전 경로를 검사하지 않으면 금지 파일을 허용 위치로 숨길 수 있다."""
    repository, base_sha = _repository(tmp_path)
    source = repository / "docs" / "unsafe.md"
    source.parent.mkdir()
    source.write_text("unsafe\n", encoding="utf-8")
    _git(repository, "add", "docs/unsafe.md")
    _git(repository, "commit", "-m", "tracked unsafe fixture")
    rename_base = _git(repository, "rev-parse", "HEAD")
    (repository / "tools").mkdir()
    _git(repository, "mv", "docs/unsafe.md", "tools/renamed.py")

    with pytest.raises(CandidateVerificationError, match="forbidden_path"):
        _verify(repository, rename_base)


def test_committed_candidate_uses_its_diff_not_dirty_working_tree(tmp_path: Path) -> None:
    """재시도 때 working tree를 읽으면 이미 push된 candidate와 다른 파일을 승인할 수 있다."""
    repository, base_sha = _repository(tmp_path)
    candidate = repository / "autoresearch" / "candidate.py"
    candidate.write_text("BASE = 2\n", encoding="utf-8")
    _git(repository, "add", "autoresearch/candidate.py")
    _git(repository, "commit", "-m", "candidate")
    candidate_sha = _git(repository, "rev-parse", "HEAD")
    unsafe = repository / "docs" / "dirty-only.md"
    unsafe.parent.mkdir()
    unsafe.write_text("not candidate\n", encoding="utf-8")

    result = verify_candidate(
        repository=repository,
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        policy=CandidatePolicy(),
    )

    assert result.changed_paths == ("autoresearch/candidate.py",)


def test_fixed_commands_use_credential_free_environment_and_stop_at_first_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ruff 실패 뒤 pytest를 실행하거나 credential을 넘기면 verifier 경계가 깨진다."""
    repository, base_sha = _repository(tmp_path)
    (repository / "autoresearch" / "candidate.py").write_text("BASE = 2\n", encoding="utf-8")
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []

    def failing_runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> int:
        calls.append((command, cwd, environment))
        return 1 if "ruff" in command else 0

    monkeypatch.setenv("GITHUB_TOKEN", "must-not-pass")
    monkeypatch.setenv("ORCH_EXECUTOR_API_TOKEN", "must-not-pass")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/gcp.json")
    monkeypatch.setattr(verifier, "_run_fixed_command", failing_runner)

    with pytest.raises(CandidateVerificationError, match="ruff_failed"):
        _verify(repository, base_sha)

    assert [command for command, _cwd, _environment in calls] == [
        ("git", "diff", "--check", base_sha),
        ("uv", "run", "--no-sync", "ruff", "check", "agent_orchestration", "autoresearch", "tests", "tools"),
    ]
    assert all(cwd == repository for _command, cwd, _environment in calls)
    environment = calls[0][2]
    assert environment["UV_PROJECT_ENVIRONMENT"] == "/opt/autoresearch-venv"
    assert "GITHUB_TOKEN" not in environment
    assert "ORCH_EXECUTOR_API_TOKEN" not in environment
    assert "KUBERNETES_SERVICE_HOST" not in environment
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in environment
