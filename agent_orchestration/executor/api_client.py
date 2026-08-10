"""Executor candidate와 실험 지표를 내부 Experiment API에 보고하는 HTTP 경계.

[파이프라인]
Stage 5 finalizer가 원격 Git ref를 확인한 뒤 candidate SHA를 Experiment의 평가 전이로
보고하는 구간과, 채점·게시가 끝난 뒤 지표 요약을 보고해 완주를 확정하는 구간이다.

[기능]
전용 token 파일로 인증 header를 만들고, 고정 멱등 key와 봉인 좌표를 내부 endpoint에
전송한 뒤 응답이 요청과 같은 SHA·기대한 상태인지 검증한다.

[비책임]
candidate의 Git commit·push(`finalizer.py`), 지표 계산과 요약 조립(`measurement.py`),
candidate row lock·상태 전이(`app/experiments/service.py`), Pod token Secret 주입
(Autoresearch-infra)은 담당하지 않는다.
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

# 결과 보고가 성공했을 때 서버가 돌려주는 상태다. 이 경로에서 `PASSED`는 "가설이 맞았다"가
# 아니라 **실험이 완주하고 결과가 나왔다**는 뜻이다
# (`docs/specs/2026-08-09-agent-authored-experiment-report.md` 결정 6).
_COMPLETED_STATUS: Final = "PASSED"


class CandidateApiError(RuntimeError):
    """token·response body를 포함하지 않는 executor API 실패 사유다.

    이름은 candidate 보고만 있던 시절의 것이다. 결과 보고도 같은 token·같은 내부
    경계를 쓰므로 예외를 나누지 않는다 — 사유 코드(`candidate_*`/`result_*`)가
    어느 보고였는지 구분한다.
    """


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


def _endpoint(api_url: str, experiment_id: uuid.UUID, action: str) -> str:
    """신뢰 가능한 http(s) base URL 아래의 고정 internal endpoint만 만든다."""
    parsed = urlsplit(api_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
        raise CandidateApiError("candidate_api_url_invalid")
    return (
        f"{api_url.rstrip('/')}/internal/executor/experiments/{experiment_id}/{action}"
    )


def _post_json(
    endpoint: str, payload: dict[str, object], token: str, *, failure: str, conflict: str
) -> dict[str, object]:
    """고정 endpoint에 봉인 payload를 보내고 JSON object 응답만 돌려준다.

    실패 사유에 응답 body와 token을 넣지 않는다 — container 로그는 실험마다 남고
    사람이 아니라 다음 실행이 읽는다.
    """
    request = Request(
        endpoint,
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
                raise CandidateApiError(failure)
            response_body = response.read()
    except HTTPError as error:
        if error.code == 409:
            raise CandidateApiError(conflict) from error
        raise CandidateApiError(failure) from error
    except (OSError, URLError) as error:
        raise CandidateApiError(failure) from error
    try:
        response_payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateApiError("candidate_api_response_invalid") from error
    if not isinstance(response_payload, dict):
        raise CandidateApiError("candidate_api_response_invalid")
    return response_payload


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
    payload: dict[str, object] = {
        "idempotency_key": f"executor-candidate:{experiment_id}",
        "issue_number": issue_number,
        "issue_branch": issue_branch,
        "base_dev_sha": base_dev_sha,
        "candidate_sha": candidate_sha,
    }
    response_payload = _post_json(
        _endpoint(api_url, experiment_id, "candidate"),
        payload,
        token,
        failure="candidate_api_failed",
        conflict="candidate_api_conflict",
    )
    if response_payload.get("candidate_sha") != candidate_sha:
        raise CandidateApiError("candidate_api_sha_mismatch")


def report_result(
    *,
    api_url: str,
    token_file: Path,
    experiment_id: uuid.UUID,
    candidate_sha: str,
    metric_snapshot: dict[str, object],
    report_markdown: str | None = None,
) -> None:
    """채점이 끝난 실험 지표를 보고하고 응답이 완주 상태인지 확인한다.

    응답 상태를 확인하는 이유는 candidate 보고에서 SHA를 되받아 확인하는 이유와
    같다 — 200을 받았다는 것과 상태가 실제로 옮겨갔다는 것은 다르다. 여기서 넘어가면
    지표가 어디에도 없는데 실행만 성공으로 끝난다.

    `report_markdown`이 `None`이면 key 자체를 싣지 않는다. 리포트 없이 보내는 것이
    정상 경로이고, API도 그렇게 받도록 돼 있다.
    """
    token = _read_token(token_file)
    payload: dict[str, object] = {
        "idempotency_key": f"executor-result:{experiment_id}",
        "candidate_sha": candidate_sha,
        "metric_snapshot": metric_snapshot,
    }
    if report_markdown is not None:
        payload["report_markdown"] = report_markdown
    response_payload = _post_json(
        _endpoint(api_url, experiment_id, "result"),
        payload,
        token,
        failure="result_api_failed",
        conflict="result_api_conflict",
    )
    if response_payload.get("status") != _COMPLETED_STATUS:
        raise CandidateApiError("result_api_status_unexpected")
