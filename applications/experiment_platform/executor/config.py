"""실험 executor Pod의 환경 입력 검증 경계.

[파이프라인]
launcher가 봉인 좌표를 Pod 환경 변수로 전달한 뒤, token 발급과 exp branch 생성이
시작되기 전에 외부 입력을 신뢰 가능한 타입으로 바꾸는 구간을 담당한다.

[기능]
실험·이슈·branch·기준 SHA·repository·token 파일 좌표와 token-minter 전용 GitHub App
좌표를 읽고 형식·상호 일치 및 필수 파일을 fail-closed로 검증한다.

[비책임]
installation token 발급(`token_minter.py`), Git ref 멱등 판단(`branch_creator.py`), 환경 변수와
Secret/volume을 Pod에 주입하는 Kubernetes 구성(Autoresearch-infra)은 담당하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import uuid

from agent_orchestration.github_app import GitHubAppCredentials


_POSITIVE_INTEGER_PATTERN = re.compile(r"^[1-9][0-9]*$")
# 실험 브랜치 이름 형식의 **정본**이다. executor 안에서 이 계약을 보는 곳(`finalizer`의
# 입력 검증, `phase2`의 로그 좌표 정제)은 사본을 두지 않고 이것을 가져다 쓴다 — 형식이
# 바뀔 때 한 곳만 고치면 되게 하려는 것이다.
#
# API가 봉인하는 이름은 `exp/<이슈번호>`다(#589). slug 접미사는 그 변경 이전에 발행된
# 실험이 DB에 들고 있는 형식이며, launcher가 그 값을 그대로 넘기므로 계속 받는다.
ISSUE_BRANCH_PATTERN = re.compile(
    r"^exp/(?P<issue_number>[1-9][0-9]*)(?:-[a-z0-9]+(?:-[a-z0-9]+)*)?$"
)
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def issue_branch_matches(issue_branch: str, issue_number: int) -> bool:
    """봉인된 브랜치 이름이 그 이슈 번호에 속하는지 판정한다.

    브랜치 이름은 이슈 번호에서만 나오므로(#589) 번호를 넘어 대조할 것이 남아 있지
    않다. 구 형식의 slug는 발행 시점 제목에서 나온 임의 문자열이라 여기서 재계산할 수
    없다.
    """
    match = ISSUE_BRANCH_PATTERN.fullmatch(issue_branch)
    return match is not None and int(match.group("issue_number")) == issue_number


class ExecutorConfigError(ValueError):
    """Executor 환경 입력이 누락됐거나 계약에 맞지 않는다."""


def _required_environment(name: str) -> str:
    """비어 있지 않은 환경 변수 값을 반환한다."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ExecutorConfigError(f"missing {name}")
    return value


def _positive_integer(name: str) -> int:
    """양의 십진 정수 환경 변수를 파싱한다."""
    value = _required_environment(name)
    if _POSITIVE_INTEGER_PATTERN.fullmatch(value) is None:
        raise ExecutorConfigError(f"invalid {name}")
    return int(value)


def _regular_file(name: str) -> Path:
    """regular file을 가리키는 환경 변수 경로를 반환한다."""
    path = Path(_required_environment(name))
    try:
        mode = path.stat().st_mode
    except OSError as error:
        raise ExecutorConfigError(f"invalid {name}") from error
    if not stat.S_ISREG(mode):
        raise ExecutorConfigError(f"invalid {name}")
    return path


@dataclass(frozen=True)
class BranchCreatorInput:
    """launcher가 executor에 전달한 봉인 branch 생성 좌표."""

    experiment_id: uuid.UUID
    issue_number: int
    issue_branch: str
    base_dev_sha: str
    github_repository: str
    token_file: Path

    @classmethod
    def from_environment(cls) -> BranchCreatorInput:
        """Pod 환경에서 봉인 좌표를 읽고 모든 입력을 검증한다."""
        experiment_id_value = _required_environment("ORCH_EXPERIMENT_ID")
        try:
            experiment_id = uuid.UUID(experiment_id_value)
        except ValueError as error:
            raise ExecutorConfigError("invalid ORCH_EXPERIMENT_ID") from error

        issue_number = _positive_integer("ORCH_ISSUE_NUMBER")
        issue_branch = _required_environment("ORCH_ISSUE_BRANCH")
        if not issue_branch_matches(issue_branch, issue_number):
            raise ExecutorConfigError("invalid ORCH_ISSUE_BRANCH")

        base_dev_sha = _required_environment("ORCH_BASE_DEV_SHA")
        if _SHA_PATTERN.fullmatch(base_dev_sha) is None:
            raise ExecutorConfigError("invalid ORCH_BASE_DEV_SHA")

        github_repository = _required_environment("ORCH_GITHUB_REPOSITORY")
        if _REPOSITORY_PATTERN.fullmatch(github_repository) is None:
            raise ExecutorConfigError("invalid ORCH_GITHUB_REPOSITORY")

        return cls(
            experiment_id=experiment_id,
            issue_number=issue_number,
            issue_branch=issue_branch,
            base_dev_sha=base_dev_sha,
            github_repository=github_repository,
            token_file=_regular_file("ORCH_GITHUB_TOKEN_FILE"),
        )


@dataclass(frozen=True)
class TokenMinterInput:
    """initContainer가 token을 발급하는 데 필요한 App 좌표와 출력 경로."""

    credentials: GitHubAppCredentials
    output: Path

    @classmethod
    def from_environment(cls) -> TokenMinterInput:
        """Pod 환경에서 GitHub App 좌표와 token 출력 경로를 읽는다."""
        credentials = GitHubAppCredentials(
            app_id=_positive_integer("ORCH_GITHUB_APP_ID"),
            installation_id=_positive_integer(
                "ORCH_GITHUB_APP_INSTALLATION_ID"
            ),
            private_key_path=_regular_file(
                "ORCH_GITHUB_APP_PRIVATE_KEY_FILE"
            ),
        )
        return cls(
            credentials=credentials,
            output=Path(_required_environment("ORCH_GITHUB_TOKEN_FILE")),
        )
