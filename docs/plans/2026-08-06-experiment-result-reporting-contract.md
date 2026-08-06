# 실험 판정 결과 Experiment API 반영 구현 계획 (#550)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `compare-paired-experiment`가 만든 `PairedExperimentResult` JSON을 읽어 그 판정을 Experiment API에 반영하는 `report-experiment-result` CLI를 만든다.

**Architecture:** 순수 변환 로직(`src/pipeline/experiment_result_report.py`)과 HTTP 전송(`agent_orchestration/ui/client.py`), 명령 배선(`src/cli.py`)을 분리한다. 변환 로직은 네트워크를 만지지 않으므로 대부분의 계약을 HTTP 없이 테스트한다. 판정은 하지 않고 이미 내려진 판정을 옮기기만 한다.

**Tech Stack:** Python 3, pydantic v2, typer, `urllib.request`(기존 client), pytest + `typer.testing.CliRunner`

## Global Constraints

정본은 `docs/specs/2026-08-06-experiment-result-reporting-contract.md`다. 아래 값은 spec과 서버 스키마에서 그대로 옮긴 것이며 임의로 바꾸지 않는다.

- `reason` 최대 길이: **8192** (`agent_orchestration/app/experiments/schemas.py:98`)
- `ExperimentLogCreate.content` 최대 길이: **8192** (`schemas.py:142`)
- `idempotency_key` 최대 길이: **128** (`schemas.py:140`)
- outcome 매핑: `comparison_passed`→`PASSED`, `comparison_rejected`→`FAILED`, `comparison_failed`→`ERROR`
- 허용 전이(`app/experiments/models.py:77-93`): `CREATED→RUNNING`, `RUNNING→{EVALUATING, ERROR}`, `EVALUATING→{PASSED, FAILED, ERROR}`, `PASSED→PROMOTED`
- 자가 claim `reason` 접두사: `manual-self-claim:`
- 종료 코드: 0 정상, 1 API·전이 실패, 2 인자·계약 오류
- 에러 출력에 **예외 원문을 싣지 않는다** — 예외 타입과 고정 진단만 출력한다
- `metric_snapshot`은 **터미널 전이에서만** 싣는다 (중간 전이에 실으면 `Experiment.metric_summary`가 확정 전 값으로 덮어써진다, `app/experiments/service.py:252-253`)
- 응답/요청 언어는 한국어 격식체, 모듈 docstring은 `[파이프라인]`/`[기능]`/`[비책임]` 형식 (CLAUDE.md)

## File Structure

| 파일 | 책임 |
| --- | --- |
| `src/pipeline/experiment_result_report.py` (신규) | 순수 변환: outcome→상태 매핑, 전이 경로 계획, `metric_snapshot`·`reason`·로그 본문 조립, idempotency key 생성. 네트워크 없음 |
| `agent_orchestration/ui/client.py` (수정) | `patch_status`, `post_log` 추가. 인증·에러 변환은 기존 `_request_json` 재사용 |
| `src/cli.py` (수정) | `report-experiment-result` 명령 배선. 오케스트레이션·`ERROR` 강등·종료 코드 매핑 |
| `tests/test_experiment_result_report.py` (신규) | 변환 로직 계약 |
| `tests/test_agent_orchestration_ui_write.py` (신규) | 쓰기 메서드의 HTTP 요청 형태 |
| `tests/test_cli.py` (수정) | 명령 배선·종료 코드·강등 |

---

### Task 1: 변환 로직 모듈 — 매핑·스냅샷·reason

**Files:**
- Create: `src/pipeline/experiment_result_report.py`
- Test: `tests/test_experiment_result_report.py`

**Interfaces:**
- Consumes: `src.pipeline.paired_experiment.PairedExperimentResult`
- Produces: `ResultReportError`, `target_status(result) -> str`, `build_metric_snapshot(result) -> dict[str, object]`, `build_reason(result) -> str`, 상태 상수 `STATUS_CREATED/RUNNING/EVALUATING/PASSED/FAILED/ERROR/PROMOTED`, `TERMINAL_STATUSES`

테스트 fixture는 `tests/test_cli.py`의 기존 헬퍼 `_paired_request_payload(seeds)`와 `_paired_result(request, outcome=...)`를 재사용한다. `tests/__init__.py`가 있어 import 가능하며, 결과 double을 새로 만들면 `#454` 계약이 바뀔 때 두 벌이 따로 낡는다.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_experiment_result_report.py
"""paired 판정 결과를 Experiment API payload로 바꾸는 계약을 검증한다."""

from __future__ import annotations

import pytest

from src.pipeline.paired_experiment import PairedExperimentRequest
from src.pipeline.experiment_result_report import (
    STATUS_ERROR,
    STATUS_FAILED,
    STATUS_PASSED,
    build_metric_snapshot,
    build_reason,
    target_status,
)
from tests.test_cli import _paired_request_payload, _paired_result


def _result(outcome: str):
    request = PairedExperimentRequest.model_validate(_paired_request_payload((42, 43, 44)))
    return _paired_result(request, outcome=outcome)


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        ("comparison_passed", STATUS_PASSED),
        ("comparison_rejected", STATUS_FAILED),
        ("comparison_failed", STATUS_ERROR),
    ],
)
def test_outcome_maps_to_experiment_status(outcome: str, expected: str) -> None:
    """comparison_failed는 판정 불가이므로 FAILED가 아니라 ERROR로 옮긴다."""
    assert target_status(_result(outcome)) == expected


def test_metric_snapshot_uses_contract_field_names() -> None:
    """#454 계약의 필드명을 그대로 옮긴다 — 이름을 새로 지으면 계약 변경이 조용히 통과한다."""
    snapshot = build_metric_snapshot(_result("comparison_passed"))

    assert set(snapshot) == {
        "metric_name",
        "primary_baseline",
        "primary_candidate",
        "paired_delta_mean",
        "confidence_interval_lower",
        "confidence_interval_upper",
        "seeds",
        "outcome",
        "reason_codes",
        "evaluated_at",
    }
    assert snapshot["seeds"] == [42, 43, 44]
    assert isinstance(snapshot["evaluated_at"], str)


def test_reason_is_truncated_with_marker() -> None:
    """8192자 상한을 넘으면 잘리되, 잘렸다는 사실을 문자열에 남긴다."""
    result = _result("comparison_rejected").model_copy(
        update={"decision_reason": "x" * 9000}
    )

    reason = build_reason(result)

    assert len(reason) == 8192
    assert reason.endswith("…(truncated)")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_experiment_result_report.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.pipeline.experiment_result_report'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/pipeline/experiment_result_report.py
"""paired 실험 판정 결과를 Experiment API 반영용 payload로 변환한다.

[파이프라인]
`compare-paired-experiment`가 게시한 `PairedExperimentResult`와 Experiment API 사이의
변환 경계다. 판정은 이미 끝난 뒤이므로 이 모듈은 판정하지 않는다.

[기능]
outcome→실험 상태 매핑, `metric_snapshot`·`reason`·포인터 로그 본문 조립, 128자 상한을
지키는 idempotency key 생성, 현재 상태에서 목표 터미널까지의 전이 경로 계획을 제공한다.

[비책임]
HTTP 전송(`agent_orchestration.ui.client`), 명령 배선과 종료 코드(`src.cli`), 판정
자체(`src.pipeline.experiment_evaluation`).
"""

from __future__ import annotations

from src.pipeline.paired_experiment import PairedExperimentResult

STATUS_CREATED = "CREATED"
STATUS_RUNNING = "RUNNING"
STATUS_EVALUATING = "EVALUATING"
STATUS_PASSED = "PASSED"
STATUS_FAILED = "FAILED"
STATUS_ERROR = "ERROR"
STATUS_PROMOTED = "PROMOTED"

TERMINAL_STATUSES = frozenset(
    {STATUS_PASSED, STATUS_FAILED, STATUS_ERROR, STATUS_PROMOTED}
)

# 서버 스키마 상한과 같은 값이다. 넘기면 Pydantic 검증에서 거부된다.
MAX_REASON_LENGTH = 8192
MAX_LOG_CONTENT_LENGTH = 8192
MAX_IDEMPOTENCY_KEY_LENGTH = 128

TRUNCATION_MARKER = "…(truncated)"

# comparison_failed는 HOLD 판정과 검증 실패를 겸한다(#454). 둘 다 "기각"이 아니라
# "판정되지 않았다"이므로 FAILED가 아닌 ERROR로 옮긴다.
_OUTCOME_TO_STATUS = {
    "comparison_passed": STATUS_PASSED,
    "comparison_rejected": STATUS_FAILED,
    "comparison_failed": STATUS_ERROR,
}


class ResultReportError(RuntimeError):
    """판정 결과를 Experiment API 상태로 옮길 수 없는 상태."""


def target_status(result: PairedExperimentResult) -> str:
    """판정 결과가 도달해야 할 터미널 상태를 반환한다."""
    try:
        return _OUTCOME_TO_STATUS[result.outcome]
    except KeyError as error:
        raise ResultReportError("알 수 없는 비교 outcome입니다.") from error


def build_metric_snapshot(result: PairedExperimentResult) -> dict[str, object]:
    """#454 결과 계약의 지표 필드를 이름 그대로 옮긴다."""
    return {
        "metric_name": result.metric_name,
        "primary_baseline": result.primary_baseline,
        "primary_candidate": result.primary_candidate,
        "paired_delta_mean": result.paired_delta_mean,
        "confidence_interval_lower": result.confidence_interval_lower,
        "confidence_interval_upper": result.confidence_interval_upper,
        "seeds": list(result.seeds),
        "outcome": result.outcome,
        "reason_codes": list(result.reason_codes),
        # datetime은 json.dumps가 직렬화하지 못한다.
        "evaluated_at": result.evaluated_at.isoformat(),
    }


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def build_reason(result: PairedExperimentResult) -> str:
    """판정 사유와 reason code를 상한 안에서 하나의 문자열로 만든다."""
    codes = ", ".join(result.reason_codes) if result.reason_codes else "none"
    return _truncate(
        f"{result.decision_reason} [reason_codes: {codes}]", MAX_REASON_LENGTH
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_experiment_result_report.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/experiment_result_report.py tests/test_experiment_result_report.py
git commit -m "feat: paired 판정 결과의 상태 매핑과 지표 스냅샷 변환 (#550)"
```

---

### Task 2: 전이 경로 계획과 idempotency key 상한

**Files:**
- Modify: `src/pipeline/experiment_result_report.py`
- Test: `tests/test_experiment_result_report.py`

**Interfaces:**
- Consumes: Task 1의 상태 상수와 `ResultReportError`
- Produces: `plan_transitions(current_status: str, target: str) -> tuple[str, ...]`, `build_log_idempotency_key(experiment_id: str, result) -> str`, `build_log_content(result, *, log_uri: str | None) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_experiment_result_report.py 에 이어서 추가

from src.pipeline.experiment_result_report import (  # 기존 import에 합친다
    MAX_IDEMPOTENCY_KEY_LENGTH,
    STATUS_CREATED,
    STATUS_EVALUATING,
    STATUS_PROMOTED,
    STATUS_RUNNING,
    ResultReportError,
    build_log_content,
    build_log_idempotency_key,
    plan_transitions,
)


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (STATUS_CREATED, (STATUS_RUNNING, STATUS_EVALUATING, STATUS_PASSED)),
        (STATUS_RUNNING, (STATUS_EVALUATING, STATUS_PASSED)),
        (STATUS_EVALUATING, (STATUS_PASSED,)),
        (STATUS_PASSED, ()),
    ],
)
def test_plan_transitions_resumes_from_current_status(
    current: str, expected: tuple[str, ...]
) -> None:
    """현재 상태부터 남은 전이만 밟는다 — PATCH가 멱등이 아니라 재호출이 event를 늘린다."""
    assert plan_transitions(current, STATUS_PASSED) == expected


@pytest.mark.parametrize("current", [STATUS_FAILED, STATUS_ERROR, STATUS_PROMOTED])
def test_plan_transitions_refuses_to_overwrite_other_terminal(current: str) -> None:
    """이미 결론이 난 실험을 다른 결론으로 덮어쓰지 않는다."""
    with pytest.raises(ResultReportError):
        plan_transitions(current, STATUS_PASSED)


@pytest.mark.parametrize(
    "field",
    ["evaluation_id", "evidence_id", None],
)
def test_log_idempotency_key_stays_within_limit(field: str | None) -> None:
    """접두사까지 이어붙이면 137자가 되어 서버가 거부한다 — 해시 부분만 쓴다."""
    result = _result("comparison_passed")
    if field == "evaluation_id":
        result = result.model_copy(
            update={"evaluation_id": "experiment-evaluation-" + "a" * 64}
        )
    elif field == "evidence_id":
        result = result.model_copy(
            update={
                "evaluation_id": None,
                "evidence_id": "paired-seed-evidence-" + "b" * 64,
            }
        )
    else:
        result = result.model_copy(update={"evaluation_id": None, "evidence_id": None})

    key = build_log_idempotency_key("0" * 36, result)

    assert len(key) <= MAX_IDEMPOTENCY_KEY_LENGTH
    assert "experiment-evaluation" not in key
    assert "paired-seed-evidence" not in key


def test_log_content_is_a_pointer_within_limit() -> None:
    """원본 JSON이 아니라 GCS 위치를 가리키는 요약만 남긴다."""
    content = build_log_content(_result("comparison_passed"), log_uri="gs://bucket/run.log")

    assert len(content) <= 8192
    assert "gs://bucket/run.log" in content
    assert "outcome=comparison_passed" in content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_experiment_result_report.py -v`
Expected: FAIL — `ImportError: cannot import name 'plan_transitions'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/pipeline/experiment_result_report.py 에 이어서 추가

def plan_transitions(current_status: str, target: str) -> tuple[str, ...]:
    """현재 상태에서 목표 터미널까지 밟아야 할 전이를 순서대로 반환한다.

    `PATCH /status`는 멱등이 아니므로(`service.py:286-288`) 이미 지난 전이를 다시
    호출하면 event가 중복된다. 그래서 남은 전이만 계획한다.
    """
    if current_status == target:
        return ()
    if current_status in TERMINAL_STATUSES:
        raise ResultReportError("이미 종료된 실험의 결론을 덮어쓰지 않습니다.")

    path: list[str] = []
    if current_status == STATUS_CREATED:
        # launcher를 대신한 자가 claim이다. 호출자가 reason에 표식을 붙인다.
        path.append(STATUS_RUNNING)
    if current_status in (STATUS_CREATED, STATUS_RUNNING):
        path.append(STATUS_EVALUATING)
    if current_status not in (STATUS_CREATED, STATUS_RUNNING, STATUS_EVALUATING):
        raise ResultReportError("알 수 없는 실험 상태입니다.")
    path.append(target)
    return tuple(path)


def build_log_idempotency_key(
    experiment_id: str, result: PairedExperimentResult
) -> str:
    """재실행해도 로그가 중복되지 않도록 결정론적 key를 만든다.

    `_stable_id`의 접두사(`experiment-evaluation-` 등)는 고유성에 기여하지 않는
    장식인데, 그대로 이어붙이면 137자가 되어 128자 상한을 넘긴다. 마지막 `-` 뒤
    sha256 부분만 쓴다.
    """
    raw = result.evaluation_id or result.evidence_id or result.candidate_sha
    key = f"{experiment_id}:paired-result:{raw.rsplit('-', 1)[-1]}"
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ResultReportError("idempotency key가 서버 상한을 넘었습니다.")
    return key


def build_log_content(
    result: PairedExperimentResult, *, log_uri: str | None
) -> str:
    """원본 JSON 대신 산출물 위치를 가리키는 포인터 로그를 만든다."""
    lines = [
        f"outcome={result.outcome}",
        f"decision_reason={result.decision_reason}",
        f"model_uri={result.model_uri or '-'}",
    ]
    if log_uri is not None:
        lines.append(f"run_log_uri={log_uri}")
    for run in result.runs:
        lines.append(
            f"seed={run.seed} artifact_uri={run.artifact_uri} log_uri={run.log_uri}"
        )
    return _truncate("\n".join(lines), MAX_LOG_CONTENT_LENGTH)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_experiment_result_report.py -v`
Expected: PASS (14 passed)

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/experiment_result_report.py tests/test_experiment_result_report.py
git commit -m "feat: 재개 가능한 전이 계획과 128자 상한 idempotency key (#550)"
```

---

### Task 3: `ExperimentClient` 쓰기 메서드

**Files:**
- Modify: `agent_orchestration/ui/client.py` (docstring `[비책임]`, 새 메서드 2개)
- Test: `tests/test_agent_orchestration_ui_write.py`

**Interfaces:**
- Consumes: 기존 `_request_json`, `_parse_model`, `Experiment.from_json`, `Log.from_json`
- Produces: `ExperimentClient.patch_status(experiment_id, status, *, reason=None, metric_snapshot=None) -> Experiment`, `ExperimentClient.post_log(experiment_id, *, idempotency_key, content, log_type="stdout") -> Log`

`POST /events` 메서드는 만들지 않는다. `PATCH /status`가 서버에서 전이 event를 함께 기록하므로(`service.py:280-289`) 별도 호출은 중복이다.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_orchestration_ui_write.py
"""Experiment API 쓰기 경로의 요청 형태를 검증한다.

HTTP 전송 계층만 본다 — 어떤 값을 보낼지 정하는 것은
`src.pipeline.experiment_result_report`의 책임이다.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_orchestration.ui import client as client_module
from agent_orchestration.ui.client import ExperimentClient


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


def _experiment_payload() -> dict[str, Any]:
    return {
        "id": "11111111-1111-1111-1111-111111111111",
        "hypothesis": "가설",
        "status": "PASSED",
        "metric_summary": {"metric_name": "roc_auc"},
        "agent_session_id": None,
        "created_at": "2026-08-06T00:00:00+00:00",
        "updated_at": "2026-08-06T00:00:01+00:00",
    }


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    requests: list[Any] = []

    def _fake_urlopen(request: Any, timeout: float) -> _FakeResponse:
        requests.append(request)
        return _FakeResponse(_experiment_payload())

    monkeypatch.setattr(client_module, "urlopen", _fake_urlopen)
    return requests


def test_patch_status_sends_patch_with_token(captured: list[Any]) -> None:
    """전이 요청은 PATCH이며 공유 토큰 헤더를 그대로 싣는다."""
    client = ExperimentClient("http://api", "token-1234567890")

    experiment = client.patch_status(
        "exp-1", "PASSED", reason="사유", metric_snapshot={"metric_name": "roc_auc"}
    )

    request = captured[0]
    assert request.method == "PATCH"
    assert request.full_url == "http://api/experiments/exp-1/status"
    assert request.get_header("X-orch-token") == "token-1234567890"
    assert json.loads(request.data.decode("utf-8")) == {
        "status": "PASSED",
        "reason": "사유",
        "metric_snapshot": {"metric_name": "roc_auc"},
    }
    assert experiment.status == "PASSED"


def test_patch_status_omits_unset_fields(captured: list[Any]) -> None:
    """중간 전이는 metric_snapshot을 싣지 않는다 — 실으면 metric_summary가 덮어써진다."""
    client = ExperimentClient("http://api", "token-1234567890")

    client.patch_status("exp-1", "EVALUATING", reason="진행")

    assert json.loads(captured[0].data.decode("utf-8")) == {
        "status": "EVALUATING",
        "reason": "진행",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_agent_orchestration_ui_write.py -v`
Expected: FAIL — `AttributeError: 'ExperimentClient' object has no attribute 'patch_status'`

- [ ] **Step 3: Write minimal implementation**

`agent_orchestration/ui/client.py`의 모듈 docstring에서 `[비책임]`의 `상태·Event·Log 쓰기`를 삭제하고 `[기능]`에 쓰기를 추가한다.

```python
# [기능] 줄을 다음으로 교체
Experiment 생성·조회, 사전등록 필드의 `[AR]` 이슈 발행 요청, Event/Log cursor 조회,
metadata 조회, 실험 상태 전이와 실행 Log 기록, API 오류의 안전한 분류를 제공한다.

# [비책임] 줄을 다음으로 교체
Streamlit 화면 렌더링, session state, Agent 실행, Step 쓰기. 이슈 본문 조립과
`gh` 호출은 API 서버가 한다 — 이 모듈은 발행을 **요청**할 뿐이다.
```

`get_metadata` 아래에 메서드 2개를 추가한다.

```python
    def patch_status(
        self,
        experiment_id: str,
        status: str,
        *,
        reason: str | None = None,
        metric_snapshot: dict[str, Any] | None = None,
    ) -> Experiment:
        """실험 상태를 전이한다.

        서버가 이 전이의 event를 함께 기록하므로 호출자가 event를 따로 만들지 않는다.
        `metric_snapshot`을 실으면 `Experiment.metric_summary`를 덮어쓴다.
        """
        payload: dict[str, object] = {"status": status}
        if reason is not None:
            payload["reason"] = reason
        if metric_snapshot is not None:
            payload["metric_snapshot"] = metric_snapshot
        return self._parse_model(
            self._request_json(
                "PATCH", f"/experiments/{experiment_id}/status", payload
            ),
            Experiment.from_json,
        )

    def post_log(
        self,
        experiment_id: str,
        *,
        idempotency_key: str,
        content: str,
        log_type: str = "stdout",
    ) -> Log:
        """실행 Log를 멱등하게 기록한다."""
        return self._parse_model(
            self._request_json(
                "POST",
                f"/experiments/{experiment_id}/logs",
                {
                    "idempotency_key": idempotency_key,
                    "log_type": log_type,
                    "content": content,
                },
            ),
            Log.from_json,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_agent_orchestration_ui_write.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add agent_orchestration/ui/client.py tests/test_agent_orchestration_ui_write.py
git commit -m "feat: Experiment API 상태 전이·Log 쓰기 client 메서드 (#550)"
```

---

### Task 4: `report-experiment-result` 명령 배선

**Files:**
- Modify: `src/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1~3의 전부
- Produces: CLI 명령 `report-experiment-result --result <path> --experiment-id <uuid> [--log-uri <uri>]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli.py 상단 import 블록에 추가
# (기존에는 PairedExperimentResult만 import되어 있다 — Request는 없다)
from src.pipeline.paired_experiment import PairedExperimentRequest  # noqa: E402

# tests/test_cli.py 끝에 추가


class _StubClient:
    """CLI 배선만 보는 client double — HTTP는 ui client 테스트가 본다."""

    def __init__(self, status: str, *, fail_on: str | None = None) -> None:
        self.status = status
        self.fail_on = fail_on
        self.calls: list[tuple[str, dict]] = []

    @classmethod
    def factory(cls, status: str, *, fail_on: str | None = None):
        instance = cls(status, fail_on=fail_on)
        return instance, type(
            "_Factory", (), {"from_environment": staticmethod(lambda: instance)}
        )

    def get_experiment(self, experiment_id: str):
        return type("_Exp", (), {"status": self.status})()

    def patch_status(self, experiment_id, status, *, reason=None, metric_snapshot=None):
        self.calls.append(("patch", {"status": status, "metric": metric_snapshot}))
        if status == self.fail_on:
            raise RuntimeError("전이 실패")
        self.status = status
        return type("_Exp", (), {"status": status})()

    def post_log(self, experiment_id, *, idempotency_key, content, log_type="stdout"):
        self.calls.append(("log", {"key": idempotency_key}))
        return None


def _write_result(tmp_path: Path, outcome: str) -> Path:
    request = PairedExperimentRequest.model_validate(_paired_request_payload((42, 43, 44)))
    path = tmp_path / "result.json"
    path.write_text(_paired_result(request, outcome=outcome).model_dump_json(), encoding="utf-8")
    return path


def test_report_result_walks_remaining_transitions(tmp_path, monkeypatch) -> None:
    """CREATED에서 시작하면 RUNNING→EVALUATING→PASSED를 밟고 지표는 마지막에만 싣는다."""
    stub, factory = _StubClient.factory("CREATED")
    monkeypatch.setattr(cli, "ExperimentClient", factory)
    result_path = _write_result(tmp_path, "comparison_passed")

    outcome = CliRunner().invoke(
        cli.app,
        ["report-experiment-result", "--result", str(result_path),
         "--experiment-id", "0" * 36],
    )

    assert outcome.exit_code == 0
    patches = [call for call in stub.calls if call[0] == "patch"]
    assert [call[1]["status"] for call in patches] == ["RUNNING", "EVALUATING", "PASSED"]
    assert patches[0][1]["metric"] is None
    assert patches[1][1]["metric"] is None
    assert patches[2][1]["metric"] is not None


def test_report_result_demotes_to_error_on_failure(tmp_path, monkeypatch) -> None:
    """중간 실패는 RUNNING에 주차하지 않고 ERROR로 내린다."""
    stub, factory = _StubClient.factory("CREATED", fail_on="PASSED")
    monkeypatch.setattr(cli, "ExperimentClient", factory)
    result_path = _write_result(tmp_path, "comparison_passed")

    outcome = CliRunner().invoke(
        cli.app,
        ["report-experiment-result", "--result", str(result_path),
         "--experiment-id", "0" * 36],
    )

    assert outcome.exit_code == 1
    assert [call[1]["status"] for call in stub.calls if call[0] == "patch"][-1] == "ERROR"


def test_report_result_rejects_invalid_result_json(tmp_path, monkeypatch) -> None:
    """계약 위반 JSON은 API를 부르기 전에 종료 코드 2로 거부한다."""
    stub, factory = _StubClient.factory("CREATED")
    monkeypatch.setattr(cli, "ExperimentClient", factory)
    path = tmp_path / "bad.json"
    path.write_text('{"outcome": "comparison_passed"}', encoding="utf-8")

    outcome = CliRunner().invoke(
        cli.app,
        ["report-experiment-result", "--result", str(path), "--experiment-id", "0" * 36],
    )

    assert outcome.exit_code == 2
    assert stub.calls == []
    assert "[결과 반영 실패]" in outcome.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_cli.py -k report_result -v`
Expected: FAIL — `AttributeError: module 'src.cli' has no attribute 'ExperimentClient'`

- [ ] **Step 3: Write minimal implementation**

`src/cli.py` 상단 import에 추가한다.

```python
from agent_orchestration.ui.client import ExperimentApiError, ExperimentClient
from src.pipeline.experiment_result_report import (
    STATUS_ERROR,
    STATUS_EVALUATING,
    STATUS_RUNNING,
    ResultReportError,
    build_log_content,
    build_log_idempotency_key,
    build_metric_snapshot,
    build_reason,
    plan_transitions,
    target_status,
)
```

`compare_paired_experiment` 아래에 명령을 추가한다.

```python
@app.command("report-experiment-result")
def report_experiment_result(
    result: Path = typer.Option(
        ..., "--result", help="compare-paired-experiment가 게시한 결과 JSON 경로"
    ),
    experiment_id: str = typer.Option(
        ..., "--experiment-id", help="Experiment API의 실험 UUID"
    ),
    log_uri: Optional[str] = typer.Option(
        None, "--log-uri", help="포인터 로그에 함께 남길 실행 로그 위치"
    ),
) -> None:
    """paired 판정 결과를 Experiment API에 반영한다(#550).

    판정도 실행도 하지 않는다. 현재 상태를 먼저 읽어 남은 전이만 밟으며, 중간에
    실패하면 `RUNNING`에 주차하지 않고 `ERROR`로 내린다.

    exit code: 반영 성공 0, API·전이 실패 1, 인자·결과 계약 오류 2.
    """
    try:
        payload = json.loads(Path(result).read_text(encoding="utf-8"))
        parsed = paired_experiment.PairedExperimentResult.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        # 결과 payload에는 URI·식별자가 섞여 있으므로 오류 종류만 남긴다.
        typer.echo(
            f"[결과 반영 실패] {type(error).__name__}: "
            "paired-offline-experiment-result-v1 결과를 읽지 못했습니다.",
            err=True,
        )
        raise typer.Exit(code=2) from error

    try:
        client = ExperimentClient.from_environment()
    except ExperimentApiError as error:
        typer.echo(
            f"[결과 반영 실패] {type(error).__name__}: "
            "Experiment API 연결 설정이 올바르지 않습니다.",
            err=True,
        )
        raise typer.Exit(code=2) from error

    reached: Optional[str] = None
    try:
        target = target_status(parsed)
        current = client.get_experiment(experiment_id).status
        transitions = plan_transitions(current, target)
        reason = build_reason(parsed)
        for status in transitions:
            is_terminal = status == target
            client.patch_status(
                experiment_id,
                status,
                reason=(
                    f"manual-self-claim: {reason}"
                    if status == STATUS_RUNNING
                    else reason
                ),
                metric_snapshot=build_metric_snapshot(parsed) if is_terminal else None,
            )
            reached = status
        client.post_log(
            experiment_id,
            idempotency_key=build_log_idempotency_key(experiment_id, parsed),
            content=build_log_content(parsed, log_uri=log_uri),
        )
    except (ExperimentApiError, ResultReportError, RuntimeError) as error:
        _demote_to_error(client, experiment_id, reached)
        typer.echo(
            f"[결과 반영 실패] {type(error).__name__}: "
            "판정 결과를 Experiment API에 반영하지 못했습니다.",
            err=True,
        )
        raise typer.Exit(code=1) from error

    typer.echo(f"{experiment_id} -> {target}")


def _demote_to_error(client, experiment_id: str, reached: Optional[str]) -> None:
    """주차된 RUNNING/EVALUATING을 남기지 않도록 터미널로 내린다."""
    if reached not in (STATUS_RUNNING, STATUS_EVALUATING):
        return
    try:
        client.patch_status(
            experiment_id, STATUS_ERROR, reason="결과 반영 중 실패로 ERROR 강등"
        )
    except Exception:  # noqa: BLE001 - 강등 실패가 원래 오류를 가리지 않게 한다
        typer.echo(
            "[결과 반영 실패] ERROR 강등에도 실패했습니다 — 실험이 RUNNING에 남았을 수 "
            "있습니다. 같은 명령을 재실행하면 재개됩니다.",
            err=True,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_cli.py -k report_result -v`
Expected: PASS (3 passed)

- [ ] **Step 5: 전체 검증**

```bash
uv run python -m pytest -q
uv run --no-sync ruff check agent_orchestration autoresearch tests tools
```
Expected: 신규 실패 없음. Windows 환경에서 이미 실패하던 항목은 baseline 대비 증감으로 판단한다.

- [ ] **Step 6: Commit**

```bash
git add src/cli.py tests/test_cli.py
git commit -m "feat: paired 판정 결과를 Experiment API에 반영하는 CLI (#550)"
```

---

## Self-Review 결과

**Spec coverage** — spec의 각 절과 담당 Task 대응:

| spec 절 | Task |
| --- | --- |
| CLI 계약 / 종료 코드 | 4 |
| 상태 전이 계약(재개·주차 금지·자가 claim 표식) | 2(계획), 4(실행·강등·표식) |
| 결과→상태 매핑 | 1 |
| 기록 내용(`metric_snapshot`·`reason`·포인터 로그) | 1, 2 |
| 클라이언트 확장 + docstring 갱신 | 3 |
| 구현 시 함정(터미널 전용 스냅샷 / 설정 가드 재사용) | 4 |
| 완료 조건 10개 | 1~4 테스트로 전부 커버 |

**남은 수동 확인 1건**: spec의 "이미 다른 터미널 상태면 거부"는 Task 2에서 `plan_transitions` 단위로 검증하지만, CLI 종료 코드 1까지 통과하는 경로는 Task 4의 강등 테스트가 간접적으로만 덮는다. 구현 중 `plan_transitions`가 `ResultReportError`를 던지는 경우 `reached`가 `None`이라 강등이 일어나지 않고 종료 코드 1로 끝나는지 확인한다.

**Placeholder scan**: 없음. 모든 코드 단계에 실제 코드 블록이 있다.

**Type consistency**: `patch_status`/`post_log`/`plan_transitions`/`build_*` 시그니처가 Task 1~4에서 동일하다. 상태 문자열은 Task 1의 상수를 Task 2·4가 그대로 쓴다.

## 알려진 후속 — 실행 중 조건이 충족되어 반영 완료

`#546` 머지 시 `CREATED` 자가 claim을 **제거가 아니라 강등**해야 한다고 적어두었다.
Task 4 실행 중 `#547`이 main에 병합된 것을 확인해 **같은 브랜치에서 별도 커밋으로
반영했다**. `plan_transitions`의 `STATUS_CREATED` 분기가 `ResultReportError`를 던지고,
CLI의 `manual-self-claim:` 접두사는 도달 불가가 되어 제거했다.

따라서 위 Task 1~4 본문의 `CREATED` 관련 서술(전이 표 첫 행, 자가 claim 표식)은 **작성
시점 기준**이며, 최종 계약은 spec의 `상태 전이 계약`과 `알려진 한계` 절을 따른다.
