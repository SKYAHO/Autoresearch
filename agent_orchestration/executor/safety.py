"""Executor의 prompt와 candidate 파일에서 실제 credential 값을 감지한다.

[파이프라인]
workspace-preparer 이후 Codex worker·candidate-verifier가 각각 비신뢰 이슈 입력과
비신뢰 repository 변경을 다루기 전에 공통 credential 형식 검사만 제공하는 보조 구간이다.

[기능]
GitHub·OpenAI·Google token, JWT, PEM private key, credential JSON key와 concrete secret
assignment 형식을 값 노출 없이 감지한다.

[비책임]
이슈 구조 검증(`prompt.py`), Git diff·경로·파일 정책(`verifier.py`), Codex 실행과
workspace 준비는 담당하지 않는다.
"""

from __future__ import annotations

import re


_CREDENTIAL_VALUE_PATTERNS = (
    re.compile(r"\bghp_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"-----BEGIN(?: [A-Z]+)? PRIVATE KEY-----"),
    re.compile(r'(?i)"private_key"\s*:'),
    re.compile(
        r"(?im)^\s*[A-Z][A-Z0-9_]*(?:TOKEN|SECRET|API_KEY)\s*=\s*"
        r"(?:['\"][^'\"]+['\"]|[^\s#]+)"
    ),
)


def contains_credential_value(text: str) -> bool:
    """실제 credential 형식이나 concrete assignment가 하나라도 있으면 참을 반환한다."""
    return any(pattern.search(text) is not None for pattern in _CREDENTIAL_VALUE_PATTERNS)
