"""Executor Codex worker에 전달할 봉인된 코드 수정 지시를 조립한다.

[파이프라인]
workspace-preparer가 이슈와 checkout을 검증한 뒤 Codex worker가 workspace 파일을 수정하기
직전의 입력 조립 구간을 담당한다.

[기능]
검증된 이슈 본문, 허용·금지 경로와 executor image가 고정한 검증 명령을 사람이 읽을 수 있는
비대화식 Codex 지시로 만든다.

[비책임]
GitHub 이슈·ref 검증과 clone(`workspace.py`), Codex 프로세스 실행(`codex_worker.py`),
변경 검증·commit·push(Stage 4/5)는 담당하지 않는다.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from agent_orchestration.executor.codex_worker import CodexRunInput


_BASE_ALLOWED_PATHS = (
    "src/** (src/features/model_contract.py 제외)",
    "autoresearch/**",
    "tests/**",
    "tools/**",
)
_CONDITIONAL_ALLOWED_PATHS = {
    "prod_model_contract": "src/features/model_contract.py",
    "feast_definition": "feature_repo/**",
}
_PROHIBITED_PATHS = (
    ".git/**",
    ".github/**",
    ".claude/**",
    "docs/**",
    "deploy/**",
    "proxy/**",
    "agent_orchestration/**",
    ".env 및 .env.* ( .env.example 포함 )",
)
_VERIFICATION_COMMANDS = (
    "uv run --no-sync ruff check agent_orchestration autoresearch tests tools",
    "uv run --no-sync python -m pytest",
)
_UNSAFE_ISSUE_BODY_PATTERN = re.compile(
    r"(?im)(?:"
    r"\b(?:token|secret|password|api[ _-]?key)\b|"
    r"\b[A-Z][A-Z0-9_]*_TOKEN\b|"
    r"\bORCH_[A-Z0-9_]+\b|"
    r"(?:https?|file)://|"
    r"/(?:var/(?:run|secrets)|secrets)(?:/|\b)|"
    r"(?:ignore|disregard)\s+(?:all\s+)?(?:previous|above)\s+"
    r"(?:instructions?|rules?|constraints?)|"
    r"(?:system|developer|assistant)\s*(?:message|prompt|instructions?)|"
    r"^---\s*$"
    r")"
)


class CodexPromptError(ValueError):
    """검증된 이슈 본문이 Codex prompt 경계를 침범할 수 있다."""


def _validate_issue_body_for_prompt(issue_body: str) -> None:
    """민감 경로나 지시 경계 탈출 형태의 이슈 본문을 값 노출 없이 거부한다."""
    if _UNSAFE_ISSUE_BODY_PATTERN.search(issue_body) is not None:
        raise CodexPromptError("issue_body_unsafe")


def build_codex_prompt(run: CodexRunInput) -> str:
    """검증된 이슈·scope로부터 credential-free Codex 수정 지시를 만든다.

    Args:
        run: workspace-preparer가 검증한 repository·이슈 본문·수정 scope 실행 입력.

    Returns:
        Codex CLI의 마지막 argv로 전달할 비대화식 지시문.
    """
    _validate_issue_body_for_prompt(run.issue_body)
    allowed_paths = list(_BASE_ALLOWED_PATHS)
    allowed_paths.extend(
        path
        for scope, path in _CONDITIONAL_ALLOWED_PATHS.items()
        if scope in run.allowed_scope
    )
    allowed = "\n".join(f"- {path}" for path in allowed_paths)
    prohibited = "\n".join(f"- {path}" for path in _PROHIBITED_PATHS)
    commands = "\n".join(f"- `{command}`" for command in _VERIFICATION_COMMANDS)
    return f"""You are the code modification worker for a pre-validated experiment.

The repository checkout and issue text below were validated before this process started.
Modify files only within the permitted paths. Do not create, change, delete, commit, or
push Git refs. Do not report results to any service. Do not change dependencies.

Validated issue body:
---
{run.issue_body}
---

Permitted paths:
{allowed}

Prohibited paths:
{prohibited}

Run these fixed verification commands after your changes:
{commands}
"""
