"""executor Pod의 컨테이너 로그를 읽어 Experiment Log로 적재하는 경계(#559).

[파이프라인] launcher가 executor Job을 만든 뒤부터 그 Job이 끝날 때까지 — 실행 중인
Pod의 컨테이너 로그를 밖에서 읽어 워크벤치가 보는 `experiment_logs`에 옮기는 구간을
담당한다. executor 컨테이너는 건드리지 않으므로 credential 경계가 유지된다.

[기능] `job-name` label로 Pod을 고르고, append-only 로그를 고정 경계로 잘라 완성된
청크만 돌려주며, 재시도로 Pod이 바뀌어도 섞이지 않는 멱등키를 만든다.

[비책임] Kubernetes 호출 자체(`CoreV1Api` 어댑터), DB 적재
(`app.experiments.service.create_experiment_log`), Step 기록(#559 범위 밖),
RoleBinding·Deployment(`SKYAHO/Autoresearch-infra`)는 담당하지 않는다.

정본 계약: `docs/specs/2026-08-09-experiment-log-collector.md`
"""

from __future__ import annotations

from typing import Final, Protocol


# `ExperimentLogCreate.content`가 max_length=8192(문자 기준)이므로 여유를 두고 자른다.
# byte가 아니라 문자로 자르는 이유도 그 제약이 문자 기준이기 때문이다.
CHUNK_SIZE: Final = 8000


def complete_chunks(text: str, *, terminated: bool) -> list[str]:
    """적재해도 안전한 청크만 돌려준다.

    컨테이너 로그는 append-only라 앞부분은 다시 바뀌지 않는다. 그래서 고정 경계로
    자르면 **완성된 청크는 불변**이고, 같은 멱등키로 다시 올려도 내용이 같다.

    반대로 아직 자라는 중인 꼬리를 올리면 다음 주기에 같은 키·다른 내용이 되어
    `create_experiment_log`가 `IdempotencyConflictError`를 낸다. 그래서 경계를 채운
    청크만 내보내고 꼬리는 보류한다.

    컨테이너가 종료되면 더 자라지 않으므로 꼬리도 불변이 된다 — 그때 함께 내보낸다.
    """
    if not text:
        return []
    chunks = [text[i : i + CHUNK_SIZE] for i in range(0, len(text), CHUNK_SIZE)]
    if not terminated and len(chunks[-1]) < CHUNK_SIZE:
        chunks.pop()
    return chunks


def log_idempotency_key(pod_name: str, container: str, seq: int) -> str:
    """같은 청크를 다시 올려도 row가 늘지 않게 하는 결정적 키를 만든다.

    **`pod_name`을 반드시 포함한다.** `backoffLimit=1` 재시도로 Pod이 새로 뜨면 그
    컨테이너 로그는 처음부터 다시 시작해 `seq`가 겹치는데 내용은 다르다. Job 이름만
    쓰면 서로 다른 실행의 청크가 같은 키를 갖게 되고, 정상 재시도가
    `IdempotencyConflictError`로 보인다.

    `pod_name`은 Job 이름을 접두사로 포함하므로 Job 식별력을 잃지 않으면서 짧다 —
    `{job_name}:{pod_name}:...` 형태는 상한 128자에 여유가 16자뿐이라 쓰지 않는다.
    """
    return f"{pod_name}:{container}:{seq}"


class _Pod(Protocol):
    """`select_pod`가 쓰는 최소 Pod 속성."""

    metadata: object


def select_pod(pods: list[_Pod]) -> _Pod | None:
    """수집 대상 Pod 하나를 고른다.

    Job 생성 직후에는 Pod이 아직 없는 것이 **정상**이므로 빈 목록을 오류로 다루지
    않는다. `backoffLimit=1` 재시도로 Pod이 둘이 될 수 있어 최신 하나만 고른다.
    """
    if not pods:
        return None
    # API 반환 순서를 신뢰하지 않는다 — 재시도 Pod이 먼저 올지 나중에 올지는 보장이 없다.
    return max(pods, key=lambda pod: pod.metadata.creation_timestamp)
