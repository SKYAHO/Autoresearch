"""paired 실험 판정 결과를 Experiment API 반영용 payload로 변환한다.

[파이프라인]
`compare-paired-experiment`가 게시한 `PairedExperimentResult`와 Experiment API 사이의
변환 경계다. 판정은 이미 끝난 뒤이므로 이 모듈은 판정하지 않는다.

[기능]
outcome→실험 상태 매핑, `metric_snapshot`·`reason`·포인터 로그 본문 조립, 128자 상한을
지키는 idempotency key 생성, 현재 상태에서 목표 터미널까지의 전이 경로 계획을 제공한다.

[비책임]
HTTP 전송(`agent_orchestration.ui.client`), 명령 배선과 종료 코드(`autoresearch.cli`), 판정
자체(`autoresearch.model_evaluation.experiment_evaluation`).
"""

from __future__ import annotations

import hashlib

from autoresearch.model_evaluation.paired_experiment import PairedExperimentResult

STATUS_CREATED = "CREATED"
STATUS_RUNNING = "RUNNING"
STATUS_EVALUATING = "EVALUATING"
STATUS_PASSED = "PASSED"
STATUS_FAILED = "FAILED"
STATUS_ERROR = "ERROR"
STATUS_PROMOTED = "PROMOTED"

# 서버의 `TERMINAL_STATUSES`와 **다른 집합**이므로 이름을 달리한다. 서버는
# `{FAILED, ERROR, PROMOTED}`이고 `PASSED → PROMOTED`를 허용하지만, 이 명령 관점에서는
# `PASSED`도 이미 내려진 결론이라 덮어쓰지 않는다. 승격은 이 계약의 범위가 아니다.
# 서버 집합과의 관계는 테스트가 고정한다(`test_experiment_result_report.py`).
REPORT_IMMUTABLE_STATUSES = frozenset(
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


class LauncherOwnedExperimentError(ResultReportError):
    """launcher가 선점할 `CREATED` 실험이라 이 명령이 다룰 수 없다.

    운영 대응이 `TerminalStatusConflictError`와 다르다 — 이쪽은 기다리면 풀린다.
    """


class TerminalStatusConflictError(ResultReportError):
    """이미 결론이 난 실험이라 다른 결론으로 덮어쓸 수 없다.

    기다려도 풀리지 않는다 — 대상 실험을 잘못 짚었는지 확인해야 한다.
    """


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


def plan_transitions(current_status: str, target: str) -> tuple[str, ...]:
    """현재 상태에서 목표 터미널까지 밟아야 할 전이를 순서대로 반환한다.

    `PATCH /status`는 멱등이 아니므로(`service.py:286-288`) 이미 지난 전이를 다시
    호출하면 event가 중복된다. 그래서 남은 전이만 계획한다.

    `CREATED`는 거부한다. #547이 병합되면서 `CREATED` + `executor_job_name IS NULL`은
    launcher의 `CREATED_CLAIM_STATEMENT`(`launcher/repository.py:49`)가 선점할 대기
    행이 되었다. 여기서 RUNNING으로 올리면 같은 행을 두고 launcher와 경합하고,
    올라간 행은 `executor_job_name`이 없어 launcher의 두 claim 쿼리 어디에도 걸리지
    않는 고아가 된다.
    """
    if current_status == target:
        return ()
    if current_status in REPORT_IMMUTABLE_STATUSES:
        raise TerminalStatusConflictError(
            "이미 종료된 실험의 결론을 덮어쓰지 않습니다."
        )
    if current_status == STATUS_CREATED:
        raise LauncherOwnedExperimentError(
            "CREATED 실험은 launcher가 RUNNING으로 선점합니다 — 직접 전이하지 않습니다."
        )
    if current_status not in (STATUS_RUNNING, STATUS_EVALUATING):
        raise ResultReportError("알 수 없는 실험 상태입니다.")

    path: list[str] = []
    # 서버는 `RUNNING → ERROR`를 직접 허용한다(`models.py:79-81`). 판정에 도달하지 못한
    # 실행에 `EVALUATING` event를 남기면 워크벤치가 "평가 중"이었다고 표시해 타임라인이
    # 사실과 어긋나고, 왕복이 한 번 늘어 실패 지점만 늘어난다.
    if current_status == STATUS_RUNNING and target != STATUS_ERROR:
        path.append(STATUS_EVALUATING)
    path.append(target)
    return tuple(path)


def build_log_idempotency_key(
    experiment_id: str, result: PairedExperimentResult
) -> str:
    """재실행해도 로그가 중복되지 않도록 결정론적 key를 만든다.

    `_stable_id`의 접두사(`experiment-evaluation-` 등)는 고유성에 기여하지 않는
    장식인데, 그대로 이어붙이면 137자가 되어 128자 상한을 넘긴다. 마지막 `-` 뒤
    sha256 부분만 쓴다.

    두 식별자가 모두 없는 경로는 **판정 엔진을 부르지도 못한 검증 실패**뿐이다
    (`paired_experiment.py:389-390`). 이때 `candidate_sha`로 떨어뜨리면 내용과 무관한
    고정값이라, 같은 후보에서 사유가 다른 검증 실패가 두 번 나면 key가 겹친다. 서버는
    같은 key에 다른 fingerprint가 오면 `IdempotencyConflictError`(409)를 내므로
    두 번째 사유가 기록되지 않고 명령도 실패한다. 그래서 이 경로만 결과 내용으로
    discriminator를 만든다 — 같은 내용이면 같은 key, 다른 사유면 다른 key다.
    """
    stable = result.evaluation_id or result.evidence_id
    if stable is not None:
        discriminator = stable.rsplit("-", 1)[-1]
    else:
        discriminator = hashlib.sha256(
            "\x1f".join(
                (result.candidate_sha, result.decision_reason, *result.reason_codes)
            ).encode("utf-8")
        ).hexdigest()
    key = f"{experiment_id}:paired-result:{discriminator}"
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ResultReportError("idempotency key가 서버 상한을 넘었습니다.")
    return key


def build_log_content(result: PairedExperimentResult, *, log_uri: str | None) -> str:
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
