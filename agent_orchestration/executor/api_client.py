"""Executor candidate를 내부 Experiment API에 보고하는 HTTP 경계.

[파이프라인]
Stage 5 finalizer가 원격 Git ref를 확인한 뒤 candidate SHA를 Experiment의 평가 전이로
보고하는 구간이다.

[기능]
전용 token 파일로 인증 header를 만들고, 고정 멱등 key와 봉인 좌표를 내부 candidate
endpoint에 전송한 뒤 응답 SHA가 요청 SHA와 같은지 검증한다.

[비책임]
candidate의 Git commit·push(`finalizer.py`), candidate row lock·상태 전이
(`app/experiments/service.py`), Pod token Secret 주입(Autoresearch-infra)은 담당하지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path
import stat
from typing import Final
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid


CANDIDATE_API_TIMEOUT_SECONDS: Final = 10.0


class CandidateApiError(RuntimeError):
    """token·response body를 포함하지 않는 candidate API 실패 사유다."""


def _read_token(path: Path) -> str:
    """regular token 파일만 읽고 값 자체는 어떤 예외에도 넣지 않는다."""
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise CandidateApiError("candidate_api_token_file_invalid")
        value = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise CandidateApiError("candidate_api_token_file_unavailable") from error
    if not value:
        raise CandidateApiError("candidate_api_token_file_empty")
    return value


def _endpoint(api_url: str, experiment_id: uuid.UUID) -> str:
    """신뢰 가능한 http(s) base URL 아래의 고정 internal endpoint만 만든다."""
    parsed = urlsplit(api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise CandidateApiError("candidate_api_url_invalid")
    return (
        f"{api_url.rstrip('/')}/internal/executor/experiments/{experiment_id}/candidate"
    )


def report_candidate(
    *,
    api_url: str,
    token_file: Path,
    experiment_id: uuid.UUID,
    issue_number: int,
    issue_branch: str,
    base_dev_sha: str,
    candidate_sha: str,
) -> None:
    """candidate SHA와 봉인 좌표를 전송하고 응답의 동일 SHA를 요구한다."""
    token = _read_token(token_file)
    payload = {
        "idempotency_key": f"executor-candidate:{experiment_id}",
        "issue_number": issue_number,
        "issue_branch": issue_branch,
        "base_dev_sha": base_dev_sha,
        "candidate_sha": candidate_sha,
    }
    request = Request(
        _endpoint(api_url, experiment_id),
        data=json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Orch-Executor-Token": token,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=CANDIDATE_API_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise CandidateApiError("candidate_api_failed")
            response_body = response.read()
    except HTTPError as error:
        if error.code == 409:
            raise CandidateApiError("candidate_api_conflict") from error
        raise CandidateApiError("candidate_api_failed") from error
    except (OSError, URLError) as error:
        raise CandidateApiError("candidate_api_failed") from error
    try:
        response_payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateApiError("candidate_api_response_invalid") from error
    if not isinstance(response_payload, dict) or response_payload.get("candidate_sha") != candidate_sha:
        raise CandidateApiError("candidate_api_sha_mismatch")
