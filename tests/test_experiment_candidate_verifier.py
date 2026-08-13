"""Candidate verifier의 실제 Git 정책과 고정 명령 실행 경계를 검증한다.

전체 executor 파이프라인에서 Codex worker 다음, finalizer 이전의 검증 단계다.
임시 Git 저장소에서 변경 경로·파일 mode·diff 크기를 실제로 만들고, 느린 Ruff/pytest만
좁은 command runner double로 대체해 verifier의 승인·거부 계약을 관찰한다.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from agent_orchestration.executor import verifier
from agent_orchestration.executor.verifier import (
    CandidatePolicy,
    CandidateVerificationError,
    current_working_tree_verification,
    verify_candidate,
)


_BASE_SHA = "a" * 40
# autouse fixture가 `_run_fixed_command`를 대체하므로, 실물 동작을 검사하려면 import
# 시점의 원본을 붙잡아 두어야 한다.
_REAL_RUN_FIXED_COMMAND = verifier._run_fixed_command


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
    ) -> tuple[int, str]:
        calls.append((command, cwd, environment))
        return 0, ""

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


def test_current_working_tree_verification_reuses_stage_four_fingerprint_contract(
    tmp_path: Path,
) -> None:
    """finalizer가 다른 digest 규칙을 쓰면 Stage 4 승인 tree를 다시 증명할 수 없다."""
    repository, base_sha = _repository(tmp_path)
    (repository / "autoresearch" / "candidate.py").write_text("BASE = 2\n", encoding="utf-8")

    approved = verify_candidate(
        repository=repository,
        base_sha=base_sha,
        candidate_sha=None,
        policy=CandidatePolicy(),
    )
    changed_paths, fingerprint = current_working_tree_verification(repository, base_sha)

    assert changed_paths == approved.changed_paths
    assert fingerprint == approved.content_fingerprint


def test_new_file_handoff_reuses_stage_four_fingerprint_contract(
    tmp_path: Path,
) -> None:
    """신규 파일의 unstaged `?`와 staged `A` 차이가 같은 tree의 handoff를 깨면 안 된다."""
    repository, base_sha = _repository(tmp_path)
    (repository / "autoresearch" / "new_candidate.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )

    approved = verify_candidate(
        repository=repository,
        base_sha=base_sha,
        candidate_sha=None,
        policy=CandidatePolicy(),
    )
    changed_paths, fingerprint = current_working_tree_verification(repository, base_sha)

    assert changed_paths == approved.changed_paths
    assert fingerprint == approved.content_fingerprint


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


@pytest.mark.parametrize(
    "contract_path",
    (
        "src/features/model_contract.py",
        "autoresearch/feature_engineering/model_contract.py",
    ),
)
def test_model_contract_requires_its_explicit_scope(
    tmp_path: Path, contract_path: str
) -> None:
    """기본 allowlist가 모델 계약 파일을 열면 의도치 않은 계약 변경을 허용한다.

    두 경로를 모두 검증한다. #754 재배치로 계약 파일이 옮겨졌지만, 이 게이트는 봉인된
    base_dev_sha 워크스페이스에 적용되므로 재배치 이전 실험은 여전히 옛 경로를 가진다.
    한쪽만 검증하면 다른 쪽 게이트가 죽어도 이 테스트는 계속 통과한다.
    """
    repository, base_sha = _repository(tmp_path)
    target = repository / contract_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("CONTRACT = 2\n", encoding="utf-8")

    with pytest.raises(CandidateVerificationError, match="forbidden_path"):
        _verify(repository, base_sha)

    assert _verify(repository, base_sha, allowed_scope=("prod_model_contract",)) == (
        contract_path,
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


@pytest.mark.parametrize(
    "content",
    (
        "TOKEN = 'ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'\n",
        "-----BEGIN PRIVATE KEY-----\nprivate-key-material\n-----END PRIVATE KEY-----\n",
        "GITHUB_TOKEN = 'real-assigned-credential-value'\n",
        "SOURCE = '/home/alice/training_dataset.csv'\n",
        r"SOURCE = 'C:\\Users\\alice\\training_dataset.csv'" + "\n",
    ),
    ids=("github-pat", "pem", "credential-assignment", "linux-path", "windows-path"),
)
def test_sensitive_content_in_an_allowed_source_file_is_rejected(
    tmp_path: Path, content: str
) -> None:
    """허용 경로만 확인하면 token·private key·로컬 데이터 경로가 candidate에 들어간다."""
    repository, base_sha = _repository(tmp_path)
    (repository / "autoresearch" / "candidate.py").write_text(content, encoding="utf-8")

    with pytest.raises(CandidateVerificationError, match="content_forbidden"):
        _verify(repository, base_sha)


def test_preexisting_sensitive_content_does_not_reject_an_unrelated_change(
    tmp_path: Path,
) -> None:
    """baseline에 이미 있던 민감 패턴 때문에 무관한 candidate 변경을 거부하면 안 된다."""
    repository, _initial_base = _repository(tmp_path)
    target = repository / "autoresearch" / "candidate.py"
    target.write_text(
        'API_TOKEN = "test-orchestration-token"\nBASE = 1\n', encoding="utf-8"
    )
    _git(repository, "add", "autoresearch/candidate.py")
    _git(repository, "commit", "-m", "sensitive baseline fixture")
    base_sha = _git(repository, "rev-parse", "HEAD")
    target.write_text(
        'API_TOKEN = "test-orchestration-token"\nBASE = 2\n', encoding="utf-8"
    )

    assert _verify(repository, base_sha) == ("autoresearch/candidate.py",)


@pytest.mark.parametrize("suffix", (".csv", ".pkl", ".parquet"))
def test_generated_data_file_is_rejected_even_under_an_allowed_path(
    tmp_path: Path, suffix: str
) -> None:
    """생성 데이터 확장자를 허용하면 repository 규정의 대용량 산출물 금지가 우회된다."""
    repository, base_sha = _repository(tmp_path)
    target = repository / "tools" / f"generated{suffix}"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(b"fixture-data")

    with pytest.raises(CandidateVerificationError, match="generated_data"):
        _verify(repository, base_sha)


@pytest.mark.parametrize("operation", ("rename", "delete"))
def test_generated_data_rename_away_or_delete_is_rejected(
    tmp_path: Path, operation: str
) -> None:
    """previous 경로를 빼면 생성 데이터의 rename-away·delete가 allowlist를 우회한다."""
    repository, _initial_base = _repository(tmp_path)
    generated = repository / "tools" / "generated.csv"
    generated.parent.mkdir(exist_ok=True)
    generated.write_text("generated,data\n", encoding="utf-8")
    _git(repository, "add", "tools/generated.csv")
    _git(repository, "commit", "-m", "generated fixture")
    base_sha = _git(repository, "rev-parse", "HEAD")
    if operation == "rename":
        _git(repository, "mv", "tools/generated.csv", "tools/generated.txt")
    else:
        _git(repository, "rm", "tools/generated.csv")

    with pytest.raises(CandidateVerificationError, match="generated_data"):
        _verify(repository, base_sha)


def test_committed_candidate_generated_data_delete_is_rejected(tmp_path: Path) -> None:
    """재시도 commit에서도 생성 데이터 삭제를 허용하면 동일한 정책을 우회한다."""
    repository, _initial_base = _repository(tmp_path)
    generated = repository / "tools" / "generated.parquet"
    generated.parent.mkdir(exist_ok=True)
    generated.write_bytes(b"PAR1fixture")
    _git(repository, "add", "tools/generated.parquet")
    _git(repository, "commit", "-m", "generated fixture")
    base_sha = _git(repository, "rev-parse", "HEAD")
    _git(repository, "rm", "tools/generated.parquet")
    _git(repository, "commit", "-m", "candidate removes generated data")
    candidate_sha = _git(repository, "rev-parse", "HEAD")

    with pytest.raises(CandidateVerificationError, match="generated_data"):
        verify_candidate(
            repository=repository,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            policy=CandidatePolicy(),
        )


def test_verification_result_binds_deterministic_fingerprint_and_staged_tree(
    tmp_path: Path,
) -> None:
    """handoff 값이 없거나 비결정적이면 Stage 5가 verifier 결과를 재확인할 수 없다."""
    repository, base_sha = _repository(tmp_path)
    (repository / "autoresearch" / "candidate.py").write_text("BASE = 2\n", encoding="utf-8")

    first = verify_candidate(repository, base_sha, None, CandidatePolicy())
    second = verify_candidate(repository, base_sha, None, CandidatePolicy())

    assert first.content_fingerprint == second.content_fingerprint
    assert len(first.content_fingerprint) == 64
    assert first.content_fingerprint == first.content_fingerprint.lower()
    assert len(first.verified_tree_oid) == 40
    assert first.verified_tree_oid == second.verified_tree_oid


@pytest.mark.parametrize("mutation", ("bytes", "mode", "path"))
def test_handoff_values_change_when_candidate_tree_changes(
    tmp_path: Path, mutation: str
) -> None:
    """bytes·mode·path 중 하나가 달라도 Stage 5가 같은 verifier 결과로 commit하면 안 된다."""
    repository, base_sha = _repository(tmp_path)
    target = repository / "autoresearch" / "candidate.py"
    target.write_text("BASE = 2\n", encoding="utf-8")
    before = verify_candidate(repository, base_sha, None, CandidatePolicy())

    if mutation == "bytes":
        target.write_text("BASE = 3\n", encoding="utf-8")
    elif mutation == "mode":
        target.chmod(0o755)
    else:
        renamed = repository / "autoresearch" / "renamed.py"
        target.rename(renamed)

    after = verify_candidate(repository, base_sha, None, CandidatePolicy())

    assert after.content_fingerprint != before.content_fingerprint
    assert after.verified_tree_oid != before.verified_tree_oid


def test_committed_candidate_handoff_tree_is_its_commit_tree(tmp_path: Path) -> None:
    """재시도 candidate가 다른 tree OID를 반환하면 Stage 5 채택이 잘못된 commit을 신뢰한다."""
    repository, base_sha = _repository(tmp_path)
    target = repository / "autoresearch" / "candidate.py"
    target.write_text("BASE = 2\n", encoding="utf-8")
    _git(repository, "add", "autoresearch/candidate.py")
    _git(repository, "commit", "-m", "candidate")
    candidate_sha = _git(repository, "rev-parse", "HEAD")

    result = verify_candidate(repository, base_sha, candidate_sha, CandidatePolicy())

    assert result.verified_tree_oid == _git(repository, "rev-parse", f"{candidate_sha}^{{tree}}")


def test_normal_token_technical_context_is_not_treated_as_a_credential(tmp_path: Path) -> None:
    """token이라는 일반 기술 용어만으로 source 변경을 거부하면 허용 scope가 과도해진다."""
    repository, base_sha = _repository(tmp_path)
    (repository / "autoresearch" / "candidate.py").write_text(
        "def refresh_token(token: str) -> str:\n    return token\n", encoding="utf-8"
    )

    assert _verify(repository, base_sha) == ("autoresearch/candidate.py",)


def test_working_tree_mutation_after_snapshot_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """snapshot 이후 원본을 바꾸면 Stage 5가 verifier가 보지 못한 tree를 commit할 수 있다."""
    repository, base_sha = _repository(tmp_path)
    target = repository / "autoresearch" / "candidate.py"
    target.write_text("BASE = 2\n", encoding="utf-8")
    original = verifier._materialize_working_tree

    def mutate_after_snapshot(*args: object, **kwargs: object) -> Path:
        snapshot = original(*args, **kwargs)
        target.write_text("BASE = 3\n", encoding="utf-8")
        return snapshot

    monkeypatch.setattr(verifier, "_materialize_working_tree", mutate_after_snapshot)

    with pytest.raises(CandidateVerificationError, match="working_tree_changed"):
        _verify(repository, base_sha)


def test_snapshot_descriptor_race_to_symlink_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """snapshot copy가 path를 다시 열면 lstat 뒤 symlink 교체로 verifier 밖 파일을 읽는다."""
    repository, base_sha = _repository(tmp_path)
    target = repository / "autoresearch" / "candidate.py"
    target.write_text("BASE = 2\n", encoding="utf-8")
    original_fstat = verifier.os.fstat
    replaced = False

    def replace_after_open(file_descriptor: int) -> os.stat_result:
        nonlocal replaced
        result = original_fstat(file_descriptor)
        if not replaced:
            replaced = True
            target.unlink()
            target.symlink_to("/etc/passwd")
        return result

    monkeypatch.setattr(verifier.os, "fstat", replace_after_open)

    with pytest.raises(CandidateVerificationError):
        _verify(repository, base_sha)


def test_copy_detection_checks_forbidden_source_and_allowed_destination(tmp_path: Path) -> None:
    """copy source를 검사하지 않으면 docs의 금지 내용을 tools로 복사해 우회할 수 있다."""
    repository, base_sha = _repository(tmp_path)
    source = repository / "docs" / "forbidden_source.py"
    source.parent.mkdir()
    source.write_text("VALUE = 'stable fixture'\n", encoding="utf-8")
    _git(repository, "add", "docs/forbidden_source.py")
    _git(repository, "commit", "-m", "copy source fixture")
    copy_base = _git(repository, "rev-parse", "HEAD")
    destination = repository / "tools" / "copied.py"
    destination.parent.mkdir()
    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    _git(repository, "add", "tools/copied.py")

    with pytest.raises(CandidateVerificationError, match="forbidden_path"):
        _verify(repository, copy_base)


def test_untracked_copy_detection_checks_forbidden_source(tmp_path: Path) -> None:
    """untracked copy도 finalizer의 git add --all 뒤 source와 함께 검사해야 한다."""
    repository, _initial_base = _repository(tmp_path)
    source = repository / "docs" / "forbidden_source.py"
    source.parent.mkdir()
    source.write_text("VALUE = 'stable fixture'\n", encoding="utf-8")
    _git(repository, "add", "docs/forbidden_source.py")
    _git(repository, "commit", "-m", "copy source fixture")
    copy_base = _git(repository, "rev-parse", "HEAD")
    destination = repository / "tools" / "copied.py"
    destination.parent.mkdir()
    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(CandidateVerificationError, match="forbidden_path"):
        _verify(repository, copy_base)


def test_pure_untracked_file_is_not_misclassified_as_a_copy(tmp_path: Path) -> None:
    """copy detection이 순수 신규 파일을 source가 있는 변경처럼 불안정하게 거부하면 안 된다."""
    repository, base_sha = _repository(tmp_path)
    target = repository / "tools" / "new.py"
    target.parent.mkdir(exist_ok=True)
    target.write_text("VALUE = 'new fixture'\n", encoding="utf-8")

    assert _verify(repository, base_sha) == ("tools/new.py",)


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
    ) -> tuple[int, str]:
        calls.append((command, cwd, environment))
        return (1, "ruff output") if "ruff" in command else (0, "")

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
    assert all(cwd != repository for _command, cwd, _environment in calls)
    environment = calls[0][2]
    assert environment["UV_PROJECT_ENVIRONMENT"] == "/opt/autoresearch-venv"
    assert "GITHUB_TOKEN" not in environment
    assert "ORCH_EXECUTOR_API_TOKEN" not in environment
    assert "KUBERNETES_SERVICE_HOST" not in environment
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in environment


def test_failing_pytest_does_not_reject_the_candidate_and_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """무관한 테스트 실패로 candidate를 거부하면 실험이 아무 기록도 남기지 못한다(#615)."""
    repository, base_sha = _repository(tmp_path)
    (repository / "autoresearch" / "candidate.py").write_text("BASE = 2\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    def pytest_fails(
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> tuple[int, str]:
        commands.append(command)
        if "pytest" in command:
            return 1, "FAILED tests/test_unrelated.py::test_environment_dependent\n"
        return 0, ""

    monkeypatch.setattr(verifier, "_run_fixed_command", pytest_fails)

    result = verify_candidate(
        repository=repository,
        base_sha=base_sha,
        candidate_sha=None,
        policy=CandidatePolicy(),
    )

    # 거부되지 않는다 — candidate는 그대로 finalizer로 넘어간다.
    assert result.changed_paths == ("autoresearch/candidate.py",)
    # 그러나 관측치는 반드시 남는다. 차단을 푸는 대신 기록도 없으면 완화가 아니라 삭제다.
    assert result.pytest_exit_code == 1
    assert "test_environment_dependent" in result.pytest_output
    # pytest는 차단 명령이 모두 통과한 **뒤에** 돈다.
    assert commands[-1] == ("uv", "run", "--no-sync", "python", "-m", "pytest")


def test_blocking_commands_still_reject_before_pytest_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """pytest를 비차단으로 바꾼 것이 diff check·Ruff까지 느슨하게 만들면 안 된다."""
    repository, base_sha = _repository(tmp_path)
    (repository / "autoresearch" / "candidate.py").write_text("BASE = 2\n", encoding="utf-8")
    commands: list[tuple[str, ...]] = []

    def diff_check_fails(
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> tuple[int, str]:
        commands.append(command)
        return (1, "whitespace error") if "diff" in command else (0, "")

    monkeypatch.setattr(verifier, "_run_fixed_command", diff_check_fails)

    with pytest.raises(CandidateVerificationError, match="diff_check_failed"):
        verify_candidate(
            repository=repository,
            base_sha=base_sha,
            candidate_sha=None,
            policy=CandidatePolicy(),
        )

    # 8분짜리 pytest는 차단 명령이 실패하면 아예 돌지 않는다.
    assert not any("pytest" in command for command in commands)


def test_pytest_output_is_capped_to_the_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """실패한 pytest의 traceback은 수 MB까지 커진다 — handoff와 로그가 그만큼 커지면 안 된다."""
    repository, base_sha = _repository(tmp_path)
    (repository / "autoresearch" / "candidate.py").write_text("BASE = 2\n", encoding="utf-8")
    huge = b"x" * (verifier._PYTEST_OUTPUT_TAIL_BYTES * 2) + b"LAST_LINE\n"

    def fake_run(command, *, cwd, env, capture_output, check):  # noqa: ANN001, ANN202
        return subprocess.CompletedProcess(command, 1, stdout=huge, stderr=b"")

    monkeypatch.setattr(verifier.subprocess, "run", fake_run)

    exit_code, output = _REAL_RUN_FIXED_COMMAND(
        ("uv", "run", "--no-sync", "python", "-m", "pytest"),
        cwd=repository,
        environment={},
    )

    assert exit_code == 1
    assert len(output.encode("utf-8")) <= verifier._PYTEST_OUTPUT_TAIL_BYTES
    assert output.endswith("LAST_LINE\n")
