"""Experiment API 쓰기 경로의 요청 형태를 검증한다.

전체 파이프라인 중 HTTP 전송 계층만 본다 — 어떤 값을 보낼지 정하는 것은
`src.pipeline.experiment_result_report`와 `src.cli`의 책임이다.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_orchestration.ui import client as client_module
from agent_orchestration.ui.client import ExperimentClient

_EXPERIMENT_PAYLOAD: dict[str, Any] = {
    "id": "11111111-1111-1111-1111-111111111111",
    "hypothesis": "가설",
    "status": "PASSED",
    "metric_summary": {"metric_name": "roc_auc"},
    "agent_session_id": None,
    "created_at": "2026-08-06T00:00:00+00:00",
    "updated_at": "2026-08-06T00:00:01+00:00",
}

_LOG_PAYLOAD: dict[str, Any] = {
    "id": "22222222-2222-2222-2222-222222222222",
    "experiment_id": "11111111-1111-1111-1111-111111111111",
    "log_type": "stdout",
    "content": "outcome=comparison_passed",
    "created_at": "2026-08-06T00:00:02+00:00",
}


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _capture(monkeypatch: pytest.MonkeyPatch, response: dict[str, Any]) -> list[Any]:
    requests: list[Any] = []

    def _fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        requests.append(request)
        return _FakeResponse(response)

    monkeypatch.setattr(client_module, "urlopen", _fake_urlopen)
    return requests


def _sent_payload(request: Any) -> dict[str, Any]:
    return json.loads(request.data.decode("utf-8"))


def test_patch_status_sends_terminal_payload_with_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """터미널 전이는 지표를 함께 싣는다 — 여기서 metric_summary가 확정된다."""
    captured = _capture(monkeypatch, _EXPERIMENT_PAYLOAD)
    client = ExperimentClient("http://api", "token-1234567890")

    experiment = client.patch_status(
        "exp-1",
        "PASSED",
        reason="criteria_met [reason_codes: none]",
        metric_snapshot={"metric_name": "roc_auc", "seeds": [42, 43, 44]},
    )

    request = captured[0]
    assert request.method == "PATCH"
    assert request.full_url == "http://api/experiments/exp-1/status"
    assert request.get_header("X-orch-token") == "token-1234567890"
    assert _sent_payload(request) == {
        "status": "PASSED",
        "reason": "criteria_met [reason_codes: none]",
        "metric_snapshot": {"metric_name": "roc_auc", "seeds": [42, 43, 44]},
    }
    assert experiment.status == "PASSED"


@pytest.mark.parametrize("status", ["RUNNING", "EVALUATING"])
def test_patch_status_omits_metric_snapshot_on_intermediate_transition(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """중간 전이는 지표 키 자체를 보내지 않는다.

    `_transition_experiment`는 metric_snapshot이 None이 아니면 전이할 때마다
    `Experiment.metric_summary`를 통째로 덮어쓴다(`service.py:252-253`). 중간 전이가
    지표를 실으면 확정 전 값이 대시보드에 노출된다.
    """
    captured = _capture(monkeypatch, _EXPERIMENT_PAYLOAD)
    client = ExperimentClient("http://api", "token-1234567890")

    client.patch_status("exp-1", status, reason="진행")

    assert _sent_payload(captured[0]) == {"status": status, "reason": "진행"}


def test_patch_status_omits_explicitly_none_metric_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None을 명시로 넘겨도 키를 보내지 않는다 — 호출자가 분기를 지우지 않아도 안전하다."""
    captured = _capture(monkeypatch, _EXPERIMENT_PAYLOAD)
    client = ExperimentClient("http://api", "token-1234567890")

    client.patch_status("exp-1", "EVALUATING", reason="진행", metric_snapshot=None)

    assert "metric_snapshot" not in _sent_payload(captured[0])


def test_post_log_sends_idempotency_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """로그는 결정론적 key와 함께 POST한다 — 재실행 시 중복을 서버가 막는다."""
    captured = _capture(monkeypatch, _LOG_PAYLOAD)
    client = ExperimentClient("http://api", "token-1234567890")

    log = client.post_log(
        "exp-1",
        idempotency_key="exp-1:paired-result:" + "a" * 64,
        content="outcome=comparison_passed",
    )

    request = captured[0]
    assert request.method == "POST"
    assert request.full_url == "http://api/experiments/exp-1/logs"
    assert _sent_payload(request) == {
        "idempotency_key": "exp-1:paired-result:" + "a" * 64,
        "log_type": "stdout",
        "content": "outcome=comparison_passed",
    }
    assert log.content == "outcome=comparison_passed"
