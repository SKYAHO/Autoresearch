"""Executor container 사이의 검증된 workspace state 파일 경계.

[파이프라인]
workspace-preparer가 이슈·원격 ref를 검증한 뒤 Codex worker, verifier, finalizer가 같은
봉인 결과를 읽기 전의 전달 구간을 담당한다.

[기능]
정규 JSON state를 0400으로 기록하고 매 read마다 schema·SHA·허용 scope·workspace 아래의
절대 repository 경로를 다시 검사해 변조된 container 간 입력을 fail-closed로 막는다.

[비책임]
GitHub 이슈/refs 조회, clone, Codex 실행, candidate 검증·commit·push는 담당하지 않는다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import stat
from tempfile import NamedTemporaryFile
from typing import Literal


_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_ALLOWED_SCOPES = frozenset(
    {"prod_model_contract", "feast_definition", "promotion"}
)


class ExecutorWorkspaceStateError(ValueError):
    """workspace state가 신뢰 계약을 만족하지 않는다."""


@dataclass(frozen=True)
class ExecutorWorkspaceState:
    """workspace-preparer가 후속 container에 봉인해 전달하는 상태."""

    schema_version: Literal[1]
    repository: Path
    issue_body: str
    allowed_scope: tuple[str, ...]
    base_dev_sha: str
    remote_tip: str


def _validated_state(state: ExecutorWorkspaceState, *, workspace: Path) -> ExecutorWorkspaceState:
    """state의 타입·경로·식별자 계약을 읽기와 쓰기 모두에서 검증한다."""
    if type(state.schema_version) is not int or state.schema_version != 1:
        raise ExecutorWorkspaceStateError("schema_version")
    workspace_path = workspace.resolve()
    if not workspace_path.is_absolute():
        raise ExecutorWorkspaceStateError("workspace")
    repository = state.repository.resolve()
    if not repository.is_absolute() or not repository.is_relative_to(workspace_path):
        raise ExecutorWorkspaceStateError("repository")
    if repository != workspace_path / "repository":
        raise ExecutorWorkspaceStateError("repository")
    if not isinstance(state.issue_body, str) or not state.issue_body:
        raise ExecutorWorkspaceStateError("issue_body")
    if (
        len(set(state.allowed_scope)) != len(state.allowed_scope)
        or any(scope not in _ALLOWED_SCOPES for scope in state.allowed_scope)
    ):
        raise ExecutorWorkspaceStateError("allowed_scope")
    if _SHA_PATTERN.fullmatch(state.base_dev_sha) is None:
        raise ExecutorWorkspaceStateError("base_dev_sha")
    if _SHA_PATTERN.fullmatch(state.remote_tip) is None:
        raise ExecutorWorkspaceStateError("remote_tip")
    return ExecutorWorkspaceState(
        schema_version=1,
        repository=repository,
        issue_body=state.issue_body,
        allowed_scope=tuple(state.allowed_scope),
        base_dev_sha=state.base_dev_sha,
        remote_tip=state.remote_tip,
    )


def write_state(
    path: Path,
    state: ExecutorWorkspaceState,
    *,
    workspace: Path,
) -> None:
    """검증된 state를 canonical JSON과 mode 0400으로 원자 기록한다."""
    validated = _validated_state(state, workspace=workspace)
    target = path.resolve()
    if target.is_relative_to(workspace.resolve()):
        raise ExecutorWorkspaceStateError("state_path")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(validated)
    payload["repository"] = str(validated.repository)
    encoded = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=target.parent, prefix=".state-", delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(encoded)
            handle.flush()
            os.fchmod(handle.fileno(), 0o400)
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    target.chmod(0o400)


def read_state(path: Path, *, workspace: Path) -> ExecutorWorkspaceState:
    """state JSON을 파싱하고 후속 container마다 신뢰 계약을 재검증한다."""
    try:
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode) or stat.S_IMODE(mode) != 0o400:
            raise ExecutorWorkspaceStateError("state_file")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ExecutorWorkspaceStateError("state_file") from error
    expected_keys = {
        "schema_version",
        "repository",
        "issue_body",
        "allowed_scope",
        "base_dev_sha",
        "remote_tip",
    }
    if not isinstance(payload, dict) or set(payload) != expected_keys:
        raise ExecutorWorkspaceStateError("state_payload")
    allowed_scope = payload["allowed_scope"]
    if not isinstance(allowed_scope, list) or not all(
        isinstance(scope, str) for scope in allowed_scope
    ):
        raise ExecutorWorkspaceStateError("allowed_scope")
    if not isinstance(payload["repository"], str):
        raise ExecutorWorkspaceStateError("repository")
    state = ExecutorWorkspaceState(
        schema_version=payload["schema_version"],
        repository=Path(payload["repository"]),
        issue_body=payload["issue_body"],
        allowed_scope=tuple(allowed_scope),
        base_dev_sha=payload["base_dev_sha"],
        remote_tip=payload["remote_tip"],
    )
    return _validated_state(state, workspace=workspace)
