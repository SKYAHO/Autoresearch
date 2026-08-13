"""Executor candidate finalizer의 commit·push·내부 API 보고 경계를 검증한다.

실제 bare Git remote와 짧은 fake HTTP server를 사용해 Stage 4가 승인한 tree만 정확히
한 candidate commit으로 수렴시키고, 원격 SHA와 Candidate API 보고가 같은지 확인한다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
from threading import Thread
from typing import Any
import uuid

import pytest

from applications.experiment_platform.executor import api_client, finalizer, verifier
from applications.experiment_platform.executor.verifier import CandidatePolicy, VerificationResult


_ISSUE_NUMBER = 557
_ISSUE_BRANCH = "exp/557-candidate-finalizer"
_EXECUTOR_NAME = "Autoresearch Experiment Executor"
_EXECUTOR_EMAIL = "experiment-executor@autoresearch.invalid"


def _git(repository: Path, *arguments: str) -> str:
    """테스트용 실제 Git repository를 조작하거나 상태를 읽는다."""
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _bare_git(repository: Path, *arguments: str) -> str:
    """bare remote ref를 읽어 finalizer push의 실제 결과를 확인한다."""
    result = subprocess.run(
        ("git", "--git-dir", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, Path, str]:
    """base commit과 exp remote ref를 가진 실제 local/bare Git 쌍을 만든다."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ("git", "init", "--bare", str(remote)), check=True, capture_output=True
    )
    repository = tmp_path / "workspace"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Finalizer test")
    _git(repository, "config", "user.email", "finalizer-test@example.invalid")
    _git(repository, "config", "core.hooksPath", "/dev/null")
    (repository / "autoresearch").mkdir()
    (repository / "autoresearch" / "candidate.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    _git(repository, "add", "autoresearch/candidate.py")
    _git(repository, "commit", "-m", "base")
    base_sha = _git(repository, "rev-parse", "HEAD")
    _git(repository, "remote", "add", "origin", remote.as_uri())
    _git(repository, "switch", "-c", _ISSUE_BRANCH)
    _git(repository, "push", "origin", f"HEAD:refs/heads/{_ISSUE_BRANCH}")
    return repository, remote, base_sha


def _commit_executor_candidate(repository: Path) -> str:
    """고정 identity/message를 가진 기준선 직계 candidate commit 하나를 만든다."""
    (repository / "autoresearch" / "candidate.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    _git(repository, "add", "--all")
    _git(
        repository,
        "-c",
        f"user.name={_EXECUTOR_NAME}",
        "-c",
        f"user.email={_EXECUTOR_EMAIL}",
        "commit",
        "-m",
        f"exp: issue #{_ISSUE_NUMBER} candidate",
    )
    return _git(repository, "rev-parse", "HEAD")


def _commit_executor_candidate_with_body(repository: Path, body: str) -> str:
    """고정 subject 뒤의 body까지 제어한 commit으로 message parser 경계를 시험한다."""
    parent = _git(repository, "rev-parse", "HEAD")
    (repository / "autoresearch" / "candidate.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    _git(repository, "add", "--all")
    tree = _git(repository, "write-tree")
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": _EXECUTOR_NAME,
        "GIT_AUTHOR_EMAIL": _EXECUTOR_EMAIL,
        "GIT_COMMITTER_NAME": _EXECUTOR_NAME,
        "GIT_COMMITTER_EMAIL": _EXECUTOR_EMAIL,
    }
    result = subprocess.run(
        ("git", "-C", str(repository), "commit-tree", tree, "-p", parent),
        check=True,
        capture_output=True,
        input=f"exp: issue #{_ISSUE_NUMBER} candidate\n\n{body}",
        text=True,
        env=environment,
    )
    return result.stdout.strip()


def _verification_for_worktree(
    repository: Path,
    base_sha: str,
    monkeypatch: pytest.MonkeyPatch,
) -> VerificationResult:
    """Stage 5 fixture에 필요한 Stage 4 handoff를 실제 verifier로 만든다."""
    monkeypatch.setattr(verifier, "_run_fixed_command", lambda *_args, **_kwargs: (0, ""))
    return verifier.verify_candidate(
        repository=repository,
        base_sha=base_sha,
        candidate_sha=None,
        policy=CandidatePolicy(),
    )


def _verification_for_commit(
    repository: Path,
    base_sha: str,
    candidate_sha: str,
    monkeypatch: pytest.MonkeyPatch,
) -> VerificationResult:
    """재시도 candidate의 Stage 4 handoff를 실제 verifier로 만든다."""
    monkeypatch.setattr(verifier, "_run_fixed_command", lambda *_args, **_kwargs: (0, ""))
    return verifier.verify_candidate(
        repository=repository,
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        policy=CandidatePolicy(),
    )


@dataclass
class _RecordedRequest:
    """fake Candidate API가 관찰한 HTTP 경계값이다."""

    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class _FakeApi:
    """응답 상태와 원시 요청을 제어하는 짧은 HTTP server fixture다."""

    status_code: int = 200
    response_factory: Callable[[dict[str, Any]], dict[str, Any]] = field(
        default=lambda payload: {"candidate_sha": payload["candidate_sha"]}
    )
    requests: list[_RecordedRequest] = field(default_factory=list)


@contextmanager
def _candidate_api_server(api: _FakeApi) -> Iterator[str]:
    """real HTTP transport를 쓰되 각 테스트가 응답을 안전하게 제어하게 한다."""
    fake = api

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            length = int(self.headers["Content-Length"])
            body = self.rfile.read(length)
            payload = __import__("json").loads(body.decode("utf-8"))
            fake.requests.append(
                _RecordedRequest(
                    path=self.path,
                    headers=dict(self.headers.items()),
                    body=body,
                )
            )
            response = (
                __import__("json").dumps(fake.response_factory(payload)).encode("utf-8")
            )
            self.send_response(fake.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, _format: str, *_args: object) -> None:
            """test output에 HTTP 요청 값, 특히 header token을 남기지 않는다."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@contextmanager
def _unauthorized_git_remote() -> Iterator[str]:
    """credential helper 호출을 유도하는 401 Git smart-HTTP remote를 제공한다."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="executor-test"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            """token-bearing URL/header가 pytest output에 남지 않게 한다."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/private.git"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _malicious_helper(repository: Path, sentinel: Path) -> None:
    """helper가 token 환경을 상속하면 sentinel에만 기록하는 local config를 만든다."""
    script = repository.parent / "credential-helper"
    script.write_text(
        f"#!/bin/sh\nprintf '%s' \"$GITHUB_TOKEN\" > {sentinel}\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    _git(repository, "config", "credential.helper", f"!{script}")


def _finalize_input(
    repository: Path,
    base_sha: str,
    api_url: str,
    tmp_path: Path,
    *,
    expected_remote_tip: str | None = None,
) -> finalizer.FinalizeInput:
    """테스트 remote/API token 파일을 포함한 finalizer 입력을 만든다."""
    push_token = tmp_path / "push-token"
    push_token.write_text("push-token-must-not-leak\n", encoding="utf-8")
    api_token = tmp_path / "api-token"
    api_token.write_text("api-token-must-not-leak\n", encoding="utf-8")
    return finalizer.FinalizeInput(
        experiment_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        issue_number=_ISSUE_NUMBER,
        issue_branch=_ISSUE_BRANCH,
        base_dev_sha=base_sha,
        expected_remote_tip=expected_remote_tip or base_sha,
        repository=repository,
        github_repository="SKYAHO/Autoresearch",
        push_token_file=push_token,
        api_url=api_url,
        api_token_file=api_token,
    )


@pytest.mark.parametrize(
    "issue_branch",
    [
        # #589 이후 API가 봉인하는 형식이다.
        f"exp/{_ISSUE_NUMBER}",
        # #589 이전에 발행된 실험이 DB에 들고 있는 형식이다.
        f"exp/{_ISSUE_NUMBER}-candidate-finalizer",
    ],
)
def test_finalizer_accepts_both_sealed_branch_forms(
    tmp_path: Path, issue_branch: str
) -> None:
    """여기서 막히면 실험이 verify까지 끝낸 뒤 push 직전에 fail-closed된다.

    발행 단계의 실패를 없앤 것이 실행 단계의 실패로 자리만 옮기면 안 된다.
    """
    config = _finalize_input(tmp_path, "a" * 40, "http://127.0.0.1:1", tmp_path)
    config = replace(config, issue_branch=issue_branch)

    finalizer._validate_repository(config)


@pytest.mark.parametrize(
    "issue_branch",
    [f"exp/{_ISSUE_NUMBER + 1}", f"exp/{_ISSUE_NUMBER}_invalid", "main"],
)
def test_finalizer_rejects_a_branch_outside_the_sealed_issue(
    tmp_path: Path, issue_branch: str
) -> None:
    """다른 이슈 번호나 브랜치 이름에 못 쓰는 문자는 계속 막는다."""
    config = _finalize_input(tmp_path, "a" * 40, "http://127.0.0.1:1", tmp_path)
    config = replace(config, issue_branch=issue_branch)

    with pytest.raises(finalizer.CandidateFinalizationError, match="finalize_input_invalid"):
        finalizer._validate_repository(config)


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("base", finalizer.CandidateState.NEW),
        ("executor", finalizer.CandidateState.ADOPTABLE),
        ("untrusted", "remote_tip_conflict"),
    ],
)
def test_classify_candidate_state_accepts_only_base_or_fixed_executor_commit(
    tmp_path: Path,
    kind: str,
    expected: finalizer.CandidateState | str,
) -> None:
    """잘못된 remote tip을 새 candidate 또는 재시도로 오인하면 계보가 깨진다."""
    repository, _remote, base_sha = _repository(tmp_path)
    remote_tip = base_sha
    if kind == "executor":
        remote_tip = _commit_executor_candidate(repository)
    elif kind == "untrusted":
        (repository / "autoresearch" / "candidate.py").write_text(
            "VALUE = 9\n", encoding="utf-8"
        )
        _git(repository, "add", "--all")
        _git(repository, "commit", "-m", "not an executor candidate")
        remote_tip = _git(repository, "rev-parse", "HEAD")

    if type(expected) is str:
        with pytest.raises(finalizer.CandidateFinalizationError, match=expected):
            finalizer.classify_candidate_state(
                repository,
                base_dev_sha=base_sha,
                issue_number=_ISSUE_NUMBER,
                remote_tip=remote_tip,
            )
    else:
        assert (
            finalizer.classify_candidate_state(
                repository,
                base_dev_sha=base_sha,
                issue_number=_ISSUE_NUMBER,
                remote_tip=remote_tip,
            )
            is expected
        )


def test_classify_candidate_state_rejects_fixed_empty_commit(
    tmp_path: Path,
) -> None:
    """변경 없는 고정 형식 commit을 채택하면 Stage 4의 no_changes 정책을 우회한다."""
    repository, _remote, base_sha = _repository(tmp_path)
    _git(
        repository,
        "-c",
        f"user.name={_EXECUTOR_NAME}",
        "-c",
        f"user.email={_EXECUTOR_EMAIL}",
        "commit",
        "--allow-empty",
        "-m",
        f"exp: issue #{_ISSUE_NUMBER} candidate",
    )
    empty_candidate = _git(repository, "rev-parse", "HEAD")

    with pytest.raises(
        finalizer.CandidateFinalizationError, match="remote_tip_conflict"
    ):
        finalizer.classify_candidate_state(
            repository,
            base_dev_sha=base_sha,
            issue_number=_ISSUE_NUMBER,
            remote_tip=empty_candidate,
        )


def test_classify_candidate_state_rejects_whitespace_only_message_body(
    tmp_path: Path,
) -> None:
    """strip()이 공백 body를 비어 있다고 보면 고정 message 계약을 우회한다."""
    repository, _remote, base_sha = _repository(tmp_path)
    candidate_sha = _commit_executor_candidate_with_body(repository, " \n")

    with pytest.raises(
        finalizer.CandidateFinalizationError, match="remote_tip_conflict"
    ):
        finalizer.classify_candidate_state(
            repository,
            base_dev_sha=base_sha,
            issue_number=_ISSUE_NUMBER,
            remote_tip=candidate_sha,
        )


def test_finalizer_rejects_local_credential_helper_before_token_bearing_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """401 remote 전에 helper가 write token을 읽으면 유출을 되돌릴 수 없다."""
    repository, _remote, base_sha = _repository(tmp_path)
    sentinel = tmp_path / "helper-token"
    _malicious_helper(repository, sentinel)
    verification = VerificationResult((), "unused", "0" * 40)
    fake_api = _FakeApi()
    with (
        _unauthorized_git_remote() as remote_url,
        _candidate_api_server(fake_api) as api_url,
    ):
        _git(repository, "remote", "set-url", "origin", remote_url)
        config = _finalize_input(repository, base_sha, api_url, tmp_path)
        monkeypatch.setattr(
            finalizer, "_clean_remote_url", lambda _repository: remote_url
        )

        with pytest.raises(
            finalizer.CandidateFinalizationError, match="credential_helper_present"
        ) as error:
            finalizer.finalize_candidate(config, verification)

    assert not sentinel.exists()
    assert "push-token-must-not-leak" not in str(error.value)
    assert not fake_api.requests


def test_token_bearing_git_disables_helper_injected_after_common_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """guard 뒤 config가 바뀌어도 401 Git subprocess가 helper에 token을 넘기면 안 된다."""
    repository, _remote, base_sha = _repository(tmp_path)
    sentinel = tmp_path / "helper-token"
    verification = VerificationResult((), "unused", "0" * 40)
    fake_api = _FakeApi()
    original_guard = finalizer._preflight_repository

    def inject_helper(*args: object, **kwargs: object) -> None:
        original_guard(*args, **kwargs)
        _malicious_helper(repository, sentinel)

    with (
        _unauthorized_git_remote() as remote_url,
        _candidate_api_server(fake_api) as api_url,
    ):
        _git(repository, "remote", "set-url", "origin", remote_url)
        config = _finalize_input(repository, base_sha, api_url, tmp_path)
        monkeypatch.setattr(
            finalizer, "_clean_remote_url", lambda _repository: remote_url
        )
        monkeypatch.setattr(finalizer, "_preflight_repository", inject_helper)

        with pytest.raises(
            finalizer.CandidateFinalizationError, match="git_failed"
        ) as error:
            finalizer.finalize_candidate(config, verification)

    assert not sentinel.exists()
    assert "push-token-must-not-leak" not in str(error.value)
    assert not fake_api.requests


def test_finalize_new_candidate_commits_once_pushes_and_reports_same_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NEW 경로가 검증 tree와 다른 commit/API SHA를 만들면 평가 계보가 갈린다."""
    repository, remote, base_sha = _repository(tmp_path)
    (repository / "autoresearch" / "candidate.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    verification = _verification_for_worktree(repository, base_sha, monkeypatch)
    fake_api = _FakeApi()
    with _candidate_api_server(fake_api) as api_url:
        config = _finalize_input(repository, base_sha, api_url, tmp_path)
        monkeypatch.setattr(
            finalizer, "_clean_remote_url", lambda _repository: remote.as_uri()
        )

        candidate_sha = finalizer.finalize_candidate(config, verification)

    assert (
        _bare_git(remote, "rev-parse", f"refs/heads/{_ISSUE_BRANCH}") == candidate_sha
    )
    assert _git(repository, "rev-parse", "HEAD") == candidate_sha
    assert _git(repository, "rev-parse", f"{candidate_sha}^") == base_sha
    assert _git(
        repository, "show", "-s", "--format=%an <%ae>|%cn <%ce>|%s|%b", candidate_sha
    ) == (
        f"{_EXECUTOR_NAME} <{_EXECUTOR_EMAIL}>|{_EXECUTOR_NAME} <{_EXECUTOR_EMAIL}>|"
        f"exp: issue #{_ISSUE_NUMBER} candidate|"
    )
    assert (
        _git(repository, "rev-list", "--count", f"{base_sha}..{candidate_sha}") == "1"
    )
    assert len(fake_api.requests) == 1
    assert __import__("json").loads(fake_api.requests[0].body) == {
        "idempotency_key": "executor-candidate:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "issue_number": _ISSUE_NUMBER,
        "issue_branch": _ISSUE_BRANCH,
        "base_dev_sha": base_sha,
        "candidate_sha": candidate_sha,
    }


def test_finalize_new_candidate_rejects_worktree_changed_after_stage_four(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """검증 뒤 파일이 바뀌면 승인하지 않은 tree를 commit하는 TOCTOU가 생긴다."""
    repository, remote, base_sha = _repository(tmp_path)
    target = repository / "autoresearch" / "candidate.py"
    target.write_text("VALUE = 2\n", encoding="utf-8")
    verification = _verification_for_worktree(repository, base_sha, monkeypatch)
    target.write_text("VALUE = 3\n", encoding="utf-8")
    fake_api = _FakeApi()
    with _candidate_api_server(fake_api) as api_url:
        config = _finalize_input(repository, base_sha, api_url, tmp_path)
        monkeypatch.setattr(
            finalizer, "_clean_remote_url", lambda _repository: remote.as_uri()
        )

        with pytest.raises(
            finalizer.CandidateFinalizationError, match="content_fingerprint_mismatch"
        ):
            finalizer.finalize_candidate(config, verification)

    assert _bare_git(remote, "rev-parse", f"refs/heads/{_ISSUE_BRANCH}") == base_sha
    assert not fake_api.requests


def test_finalize_rejects_push_race_without_replacing_remote_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """push 직전 다른 candidate가 도착하면 force update 없이 실패해야 한다."""
    repository, remote, base_sha = _repository(tmp_path)
    (repository / "autoresearch" / "candidate.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    verification = _verification_for_worktree(repository, base_sha, monkeypatch)
    racer = tmp_path / "racer"
    subprocess.run(
        ("git", "clone", remote.as_uri(), str(racer)), check=True, capture_output=True
    )
    _git(racer, "config", "user.name", "Finalizer race test")
    _git(racer, "config", "user.email", "finalizer-race@example.invalid")
    _git(racer, "switch", _ISSUE_BRANCH)
    (racer / "autoresearch" / "candidate.py").write_text(
        "VALUE = 88\n", encoding="utf-8"
    )
    _git(racer, "add", "--all")
    _git(racer, "commit", "-m", "racing candidate")
    racer_sha = _git(racer, "rev-parse", "HEAD")
    fake_api = _FakeApi()
    original_push = finalizer._push_candidate

    def race_then_push(*args: object, **kwargs: object) -> None:
        _git(racer, "push", "origin", f"HEAD:refs/heads/{_ISSUE_BRANCH}")
        original_push(*args, **kwargs)

    with _candidate_api_server(fake_api) as api_url:
        config = _finalize_input(repository, base_sha, api_url, tmp_path)
        monkeypatch.setattr(
            finalizer, "_clean_remote_url", lambda _repository: remote.as_uri()
        )
        monkeypatch.setattr(finalizer, "_push_candidate", race_then_push)

        with pytest.raises(finalizer.CandidateFinalizationError, match="push_failed"):
            finalizer.finalize_candidate(config, verification)

    assert _bare_git(remote, "rev-parse", f"refs/heads/{_ISSUE_BRANCH}") == racer_sha
    assert not fake_api.requests


def test_finalize_rechecks_remote_before_commit_without_local_or_api_side_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """staging 뒤 remote tip이 바뀌면 local commit을 만들기 전에 중단해야 한다."""
    repository, remote, base_sha = _repository(tmp_path)
    (repository / "autoresearch" / "candidate.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    verification = _verification_for_worktree(repository, base_sha, monkeypatch)
    racer = tmp_path / "racer"
    subprocess.run(
        ("git", "clone", remote.as_uri(), str(racer)), check=True, capture_output=True
    )
    _git(racer, "config", "user.name", "Finalizer race test")
    _git(racer, "config", "user.email", "finalizer-race@example.invalid")
    _git(racer, "switch", _ISSUE_BRANCH)
    (racer / "autoresearch" / "candidate.py").write_text(
        "VALUE = 88\n", encoding="utf-8"
    )
    _git(racer, "add", "--all")
    _git(racer, "commit", "-m", "racing candidate")
    racer_sha = _git(racer, "rev-parse", "HEAD")
    original_recheck = finalizer._assert_remote_base_before_commit

    def race_then_recheck(*args: object, **kwargs: object) -> None:
        _git(racer, "push", "origin", f"HEAD:refs/heads/{_ISSUE_BRANCH}")
        original_recheck(*args, **kwargs)

    fake_api = _FakeApi()
    with _candidate_api_server(fake_api) as api_url:
        config = _finalize_input(repository, base_sha, api_url, tmp_path)
        monkeypatch.setattr(
            finalizer, "_clean_remote_url", lambda _repository: remote.as_uri()
        )
        monkeypatch.setattr(
            finalizer, "_assert_remote_base_before_commit", race_then_recheck
        )

        with pytest.raises(
            finalizer.CandidateFinalizationError, match="remote_tip_changed"
        ):
            finalizer.finalize_candidate(config, verification)

    assert _git(repository, "rev-parse", "HEAD") == base_sha
    assert _bare_git(remote, "rev-parse", f"refs/heads/{_ISSUE_BRANCH}") == racer_sha
    assert not fake_api.requests


def test_finalize_commits_verified_tree_when_index_changes_after_tree_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tree 검증 직후 index를 바꿔도 commit/push/API SHA가 검증 tree를 벗어나면 안 된다."""
    repository, remote, base_sha = _repository(tmp_path)
    (repository / "autoresearch" / "candidate.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    verification = _verification_for_worktree(repository, base_sha, monkeypatch)
    original_recheck = finalizer._assert_remote_base_before_commit

    def mutate_index_then_recheck(*args: object, **kwargs: object) -> None:
        _git(repository, "read-tree", base_sha)
        original_recheck(*args, **kwargs)

    fake_api = _FakeApi()
    with _candidate_api_server(fake_api) as api_url:
        config = _finalize_input(repository, base_sha, api_url, tmp_path)
        monkeypatch.setattr(
            finalizer, "_clean_remote_url", lambda _repository: remote.as_uri()
        )
        monkeypatch.setattr(
            finalizer, "_assert_remote_base_before_commit", mutate_index_then_recheck
        )

        candidate_sha = finalizer.finalize_candidate(config, verification)

    assert (
        _git(repository, "rev-parse", f"{candidate_sha}^{{tree}}")
        == verification.verified_tree_oid
    )
    assert (
        _bare_git(remote, "rev-parse", f"refs/heads/{_ISSUE_BRANCH}") == candidate_sha
    )
    assert len(fake_api.requests) == 1


def test_finalize_adopts_matching_remote_candidate_without_a_new_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API만 실패한 재시도는 이미 검증된 remote commit을 다시 만들면 안 된다."""
    repository, remote, base_sha = _repository(tmp_path)
    candidate_sha = _commit_executor_candidate(repository)
    _git(repository, "push", "origin", f"HEAD:refs/heads/{_ISSUE_BRANCH}")
    verification = _verification_for_commit(
        repository, base_sha, candidate_sha, monkeypatch
    )
    fake_api = _FakeApi()
    with _candidate_api_server(fake_api) as api_url:
        config = _finalize_input(
            repository,
            base_sha,
            api_url,
            tmp_path,
            expected_remote_tip=candidate_sha,
        )
        monkeypatch.setattr(
            finalizer, "_clean_remote_url", lambda _repository: remote.as_uri()
        )

        adopted_sha = finalizer.finalize_candidate(config, verification)

    assert adopted_sha == candidate_sha
    assert _git(repository, "rev-parse", "HEAD") == candidate_sha
    assert (
        _git(repository, "rev-list", "--count", f"{base_sha}..{candidate_sha}") == "1"
    )
    assert len(fake_api.requests) == 1


def test_finalize_rejects_same_tree_executor_commit_when_remote_tip_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """verifier가 본 SHA와 다른 commit은 tree가 같아도 API에 보고하면 안 된다."""
    repository, remote, base_sha = _repository(tmp_path)
    expected_sha = _commit_executor_candidate(repository)
    tree = _git(repository, "rev-parse", f"{expected_sha}^{{tree}}")
    result = subprocess.run(
        (
            "git",
            "-C",
            str(repository),
            "commit-tree",
            tree,
            "-p",
            base_sha,
            "-m",
            f"exp: issue #{_ISSUE_NUMBER} candidate",
        ),
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": _EXECUTOR_NAME,
            "GIT_AUTHOR_EMAIL": _EXECUTOR_EMAIL,
            "GIT_COMMITTER_NAME": _EXECUTOR_NAME,
            "GIT_COMMITTER_EMAIL": _EXECUTOR_EMAIL,
            "GIT_AUTHOR_DATE": "2026-08-06T00:00:01+00:00",
            "GIT_COMMITTER_DATE": "2026-08-06T00:00:01+00:00",
        },
    )
    changed_sha = result.stdout.strip()
    assert changed_sha != expected_sha
    _git(repository, "push", "origin", f"{changed_sha}:refs/heads/{_ISSUE_BRANCH}")
    verification = _verification_for_commit(
        repository, base_sha, expected_sha, monkeypatch
    )
    fake_api = _FakeApi()

    with _candidate_api_server(fake_api) as api_url:
        config = _finalize_input(
            repository,
            base_sha,
            api_url,
            tmp_path,
            expected_remote_tip=expected_sha,
        )
        monkeypatch.setattr(
            finalizer, "_clean_remote_url", lambda _repository: remote.as_uri()
        )

        with pytest.raises(
            finalizer.CandidateFinalizationError, match="remote_tip_changed"
        ):
            finalizer.finalize_candidate(config, verification)

    assert not fake_api.requests


def test_finalize_retries_api_failure_by_adopting_same_pushed_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NEW push 뒤 API만 실패해도 재시도는 새 commit 없이 기존 SHA를 보고해야 한다."""
    repository, remote, base_sha = _repository(tmp_path)
    (repository / "autoresearch" / "candidate.py").write_text(
        "VALUE = 2\n", encoding="utf-8"
    )
    verification = _verification_for_worktree(repository, base_sha, monkeypatch)
    fake_api = _FakeApi(
        status_code=500, response_factory=lambda _payload: {"detail": "failed"}
    )
    with _candidate_api_server(fake_api) as api_url:
        config = _finalize_input(repository, base_sha, api_url, tmp_path)
        monkeypatch.setattr(
            finalizer, "_clean_remote_url", lambda _repository: remote.as_uri()
        )

        with pytest.raises(
            finalizer.CandidateFinalizationError, match="candidate_api_failed"
        ):
            finalizer.finalize_candidate(config, verification)
        pushed_sha = _bare_git(remote, "rev-parse", f"refs/heads/{_ISSUE_BRANCH}")
        fake_api.status_code = 200
        fake_api.response_factory = lambda payload: {
            "candidate_sha": payload["candidate_sha"]
        }

        retry_config = _finalize_input(
            repository,
            base_sha,
            api_url,
            tmp_path,
            expected_remote_tip=pushed_sha,
        )
        retried_sha = finalizer.finalize_candidate(retry_config, verification)

    assert retried_sha == pushed_sha
    assert _git(repository, "rev-list", "--count", f"{base_sha}..{pushed_sha}") == "1"
    assert len(fake_api.requests) == 2


def test_candidate_api_sends_exact_contract_with_fixed_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """header·payload·timeout이 달라지면 내부 API 인증 또는 멱등성이 깨진다."""
    token_file = tmp_path / "api-token"
    token_file.write_text("api-token-must-not-leak\n", encoding="utf-8")
    fake_api = _FakeApi()
    observed_timeouts: list[float] = []
    original_urlopen = api_client.urlopen

    def recording_urlopen(request: object, *, timeout: float) -> object:
        observed_timeouts.append(timeout)
        return original_urlopen(request, timeout=timeout)

    with _candidate_api_server(fake_api) as api_url:
        monkeypatch.setattr(api_client, "urlopen", recording_urlopen)
        api_client.report_candidate(
            api_url=api_url,
            token_file=token_file,
            experiment_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            issue_number=_ISSUE_NUMBER,
            issue_branch=_ISSUE_BRANCH,
            base_dev_sha="a" * 40,
            candidate_sha="b" * 40,
        )

    assert observed_timeouts == [api_client.CANDIDATE_API_TIMEOUT_SECONDS]
    assert len(fake_api.requests) == 1
    request = fake_api.requests[0]
    assert (
        request.path
        == "/internal/executor/experiments/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/candidate"
    )
    assert request.headers["X-Orch-Executor-Token"] == "api-token-must-not-leak"
    assert request.headers["Content-Type"] == "application/json"
    assert __import__("json").loads(request.body) == {
        "idempotency_key": "executor-candidate:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "issue_number": _ISSUE_NUMBER,
        "issue_branch": _ISSUE_BRANCH,
        "base_dev_sha": "a" * 40,
        "candidate_sha": "b" * 40,
    }


@pytest.mark.parametrize(
    "status_code,response,reason",
    [
        (
            409,
            {"detail": "api-token-must-not-leak server-body"},
            "candidate_api_conflict",
        ),
        (
            500,
            {"detail": "api-token-must-not-leak server-body"},
            "candidate_api_failed",
        ),
        (200, {"candidate_sha": "c" * 40}, "candidate_api_sha_mismatch"),
    ],
)
def test_candidate_api_rejects_conflict_or_bad_response_without_leaking_body(
    tmp_path: Path,
    status_code: int,
    response: dict[str, str],
    reason: str,
) -> None:
    """409·server body·다른 응답 SHA를 신뢰하면 candidate 수렴을 증명할 수 없다."""
    token_file = tmp_path / "api-token"
    token_file.write_text("api-token-must-not-leak\n", encoding="utf-8")
    fake_api = _FakeApi(
        status_code=status_code, response_factory=lambda _payload: response
    )

    with _candidate_api_server(fake_api) as api_url:
        with pytest.raises(api_client.CandidateApiError, match=reason) as error:
            api_client.report_candidate(
                api_url=api_url,
                token_file=token_file,
                experiment_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                issue_number=_ISSUE_NUMBER,
                issue_branch=_ISSUE_BRANCH,
                base_dev_sha="a" * 40,
                candidate_sha="b" * 40,
            )

    assert "api-token-must-not-leak" not in str(error.value)
    assert "server-body" not in str(error.value)


def test_result_api_sends_exact_contract_with_fixed_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """결과 보고도 같은 token header와 실험별 고정 멱등 key를 쓴다."""
    token_file = tmp_path / "api-token"
    token_file.write_text("api-token-must-not-leak\n", encoding="utf-8")
    snapshot = {"contract_version": "experiment-metric-snapshot-v1", "seeds": [11]}
    fake_api = _FakeApi(response_factory=lambda _payload: {"status": "PASSED"})
    observed_timeouts: list[float] = []
    original_urlopen = api_client.urlopen

    def recording_urlopen(request: object, *, timeout: float) -> object:
        observed_timeouts.append(timeout)
        return original_urlopen(request, timeout=timeout)

    with _candidate_api_server(fake_api) as api_url:
        monkeypatch.setattr(api_client, "urlopen", recording_urlopen)
        api_client.report_result(
            api_url=api_url,
            token_file=token_file,
            experiment_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            candidate_sha="b" * 40,
            metric_snapshot=snapshot,
        )

    assert observed_timeouts == [api_client.CANDIDATE_API_TIMEOUT_SECONDS]
    request = fake_api.requests[0]
    assert (
        request.path
        == "/internal/executor/experiments/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/result"
    )
    assert request.headers["X-Orch-Executor-Token"] == "api-token-must-not-leak"
    assert __import__("json").loads(request.body) == {
        "idempotency_key": "executor-result:aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "candidate_sha": "b" * 40,
        "metric_snapshot": snapshot,
    }


@pytest.mark.parametrize(
    "status_code,response,reason",
    [
        (409, {"detail": "api-token-must-not-leak server-body"}, "result_api_conflict"),
        (500, {"detail": "api-token-must-not-leak server-body"}, "result_api_failed"),
        # 200이어도 상태가 옮겨가지 않았으면 보고된 것이 아니다.
        (200, {"status": "EVALUATING"}, "result_api_status_unexpected"),
    ],
)
def test_result_api_rejects_conflict_or_unexpected_status_without_leaking_body(
    tmp_path: Path,
    status_code: int,
    response: dict[str, str],
    reason: str,
) -> None:
    """상태가 완주로 옮겨간 응답만 보고 성공으로 취급한다."""
    token_file = tmp_path / "api-token"
    token_file.write_text("api-token-must-not-leak\n", encoding="utf-8")
    fake_api = _FakeApi(
        status_code=status_code, response_factory=lambda _payload: response
    )

    with _candidate_api_server(fake_api) as api_url:
        with pytest.raises(api_client.CandidateApiError, match=reason) as error:
            api_client.report_result(
                api_url=api_url,
                token_file=token_file,
                experiment_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                candidate_sha="b" * 40,
                metric_snapshot={"seeds": [11]},
            )

    assert "api-token-must-not-leak" not in str(error.value)
    assert "server-body" not in str(error.value)


@contextmanager
def _version_skew_result_api(*, always_422: bool) -> Iterator[tuple[str, list[dict]]]:
    """`report_markdown`이 실린 요청만 422로 거절하는(또는 항상 거절하는) 서버다.

    구 API pod가 아직 `report_markdown`을 모르는 상황(`extra="forbid"`)을 재현한다.
    """
    seen_payloads: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            length = int(self.headers["Content-Length"])
            body = self.rfile.read(length)
            payload = __import__("json").loads(body.decode("utf-8"))
            seen_payloads.append(payload)
            if always_422 or "report_markdown" in payload:
                response_body = {"detail": "unrecognized field: report_markdown"}
                status = 422
            else:
                response_body = {"status": "PASSED"}
                status = 200
            response = __import__("json").dumps(response_body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, _format: str, *_args: object) -> None:
            """test output에 token을 남기지 않는다."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", seen_payloads
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def test_result_api_retries_once_without_report_on_422_version_skew(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """구 API pod가 `report_markdown`을 몰라 422를 내면 그 필드 없이 한 번 재시도한다.

    이 재시도가 없으면 새 executor가 새 필드를 실어 보낸 순간 완주 보고 전체가
    실패해, 지표는 GCS에 있는데 `metric_summary`는 null인 채로 실험이 회수된다.
    """
    token_file = tmp_path / "api-token"
    token_file.write_text("api-token-must-not-leak\n", encoding="utf-8")
    experiment_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")

    with _version_skew_result_api(always_422=False) as (api_url, seen_payloads):
        with caplog.at_level("WARNING"):
            api_client.report_result(
                api_url=api_url,
                token_file=token_file,
                experiment_id=experiment_id,
                candidate_sha="b" * 40,
                metric_snapshot={"seeds": [11]},
                report_markdown="# 결론",
            )

    assert len(seen_payloads) == 2
    assert "report_markdown" in seen_payloads[0]
    assert "report_markdown" not in seen_payloads[1]
    assert any(
        "report_markdown dropped on 422 retry" in record.message
        and str(experiment_id) in record.message
        for record in caplog.records
    )


def test_result_api_gives_up_after_a_second_422(tmp_path: Path) -> None:
    """두 번째도 422면 그대로 올린다 — 재시도는 정확히 한 번뿐이고 원인을 구분하지 않는다."""
    token_file = tmp_path / "api-token"
    token_file.write_text("api-token-must-not-leak\n", encoding="utf-8")

    with _version_skew_result_api(always_422=True) as (api_url, seen_payloads):
        with pytest.raises(api_client.CandidateApiError, match="result_api_failed"):
            api_client.report_result(
                api_url=api_url,
                token_file=token_file,
                experiment_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                candidate_sha="b" * 40,
                metric_snapshot={"seeds": [11]},
                report_markdown="# 결론",
            )

    assert len(seen_payloads) == 2


def test_result_api_does_not_retry_a_422_without_a_report(tmp_path: Path) -> None:
    """리포트 없이 보낸 요청이 422면 재시도할 것이 없으므로 그대로 올린다."""
    token_file = tmp_path / "api-token"
    token_file.write_text("api-token-must-not-leak\n", encoding="utf-8")

    with _version_skew_result_api(always_422=True) as (api_url, seen_payloads):
        with pytest.raises(api_client.CandidateApiError, match="result_api_failed"):
            api_client.report_result(
                api_url=api_url,
                token_file=token_file,
                experiment_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                candidate_sha="b" * 40,
                metric_snapshot={"seeds": [11]},
            )

    assert len(seen_payloads) == 1
