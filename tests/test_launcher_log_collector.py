"""실험 로그 수집기의 Pod 조회·청크·멱등키·오류 분류 계약을 검증한다(#559).

[파이프라인] executor Pod가 도는 동안 그 컨테이너 로그를 읽어 `experiment_logs`에
적재하는 구간을 담당한다. executor 코드는 건드리지 않고 밖에서 관측만 한다.

[기능] `job-name` label로 Pod을 찾고, append-only 로그를 고정 경계로 잘라 완성된
청크만 적재하며, 컨테이너 미시작 같은 정상 상황과 실제 오류를 구분하는 것을 검증한다.

[비책임] Experiment 선점과 Job 생성(`test_experiment_launcher.py`), Step 기록(범위 밖),
Kubernetes RBAC·NetworkPolicy(`SKYAHO/Autoresearch-infra`)는 다루지 않는다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_orchestration.launcher.log_collector import (  # noqa: E402
    CHUNK_SIZE,
    complete_chunks,
    log_idempotency_key,
    select_pod,
)


class _Pod:
    """`creationTimestamp`와 이름만 갖는 최소 Pod double."""

    def __init__(self, name: str, created: datetime) -> None:
        self.metadata = type(
            "Meta", (), {"name": name, "creation_timestamp": created}
        )()


def test_select_pod_returns_none_when_no_pod_exists() -> None:
    """Job 생성 직후 스케줄링 지연은 정상이다 — 오류가 아니라 없음으로 다룬다."""
    assert select_pod([]) is None


def test_select_pod_returns_the_only_pod() -> None:
    pod = _Pod("ar-exec-abc-x7k2", datetime(2026, 8, 9, 12, 0, tzinfo=UTC))

    assert select_pod([pod]) is pod


def test_select_pod_picks_the_newest_when_a_retry_created_a_second_pod() -> None:
    """`backoffLimit=1` 재시도로 Pod이 둘이 되면 최신 것만 수집한다.

    API 반환 순서에 의존하지 않는지 보려고 **오래된 것을 먼저** 둔다.
    """
    older = _Pod("ar-exec-abc-x7k2", datetime(2026, 8, 9, 12, 0, tzinfo=UTC))
    newer = _Pod("ar-exec-abc-m9p4", datetime(2026, 8, 9, 12, 30, tzinfo=UTC))

    assert select_pod([older, newer]) is newer
    assert select_pod([newer, older]) is newer


def test_complete_chunks_holds_back_a_growing_tail() -> None:
    """자라는 중인 마지막 청크를 올리면 다음 주기에 같은 키·다른 내용이 되어 충돌한다."""
    assert complete_chunks("a" * (CHUNK_SIZE - 1), terminated=False) == []


def test_complete_chunks_emits_a_chunk_only_when_the_boundary_is_reached() -> None:
    text = "a" * CHUNK_SIZE

    assert complete_chunks(text, terminated=False) == [text]


def test_complete_chunks_keeps_the_partial_remainder_until_termination() -> None:
    """경계를 넘긴 뒤에도 남는 꼬리는 완성 전까지 보류한다 — 앞부분만 불변이다."""
    text = "a" * CHUNK_SIZE + "b" * 10

    assert complete_chunks(text, terminated=False) == ["a" * CHUNK_SIZE]


def test_complete_chunks_flushes_the_remainder_when_the_container_terminated() -> None:
    """종료된 컨테이너의 로그는 더 자라지 않으므로 꼬리도 불변이 된다."""
    text = "a" * CHUNK_SIZE + "b" * 10

    assert complete_chunks(text, terminated=True) == ["a" * CHUNK_SIZE, "b" * 10]


def test_complete_chunks_ignores_empty_output() -> None:
    assert complete_chunks("", terminated=True) == []


def test_log_idempotency_key_includes_the_pod_name() -> None:
    """`pod_name`이 없으면 재시도로 새로 뜬 Pod의 청크가 이전 것과 같은 키를 갖는다.

    새 Pod의 로그는 처음부터 다시 시작해 `seq`가 겹치는데, 내용은 다르다. 그러면
    정상 재시도가 `IdempotencyConflictError`로 보인다.
    """
    first = log_idempotency_key("ar-exec-abc-x7k2", "codex-worker", 0)
    retried = log_idempotency_key("ar-exec-abc-m9p4", "codex-worker", 0)

    assert first != retried


def test_log_idempotency_key_stays_within_the_schema_limit() -> None:
    """`ExperimentLogCreate.idempotency_key`가 max_length=128이다.

    컨테이너 이름이 길어지면 회귀하므로 현행 최장 조합으로 경계를 고정한다.
    """
    key = log_idempotency_key(
        "ar-exec-6ec09890a4a84c699760c01349351505-x7k2",  # 46자
        "candidate-finalizer",  # 현행 최장 컨테이너 이름 19자
        9999,
    )

    assert len(key) <= 128
    assert len(key) < 80, f"여유가 급격히 줄었다: {len(key)}자"
