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

import logging
import uuid
from collections.abc import Callable
from typing import Final, Protocol

from kubernetes.client.exceptions import ApiException


_LOGGER = logging.getLogger(__name__)

# 컨테이너가 아직 뜨지 않았거나 Pod이 이미 회수된 상태를 뜻하는 응답이다. 8-container
# 순차 실행이라 뒤 컨테이너가 없는 것은 **정상**이고, TTL 회수·재시도 교체로
# `list_namespaced_pod` 직후 Pod이 사라지는 레이스도 여기로 떨어진다. 둘 다 skip이라
# 지금은 한 갈래로 다루되, 나중에 구분이 필요해질 수 있어 사유를 분리해 둔다.
_ABSENT_STATUSES: Final = frozenset({400, 404})

# `repository._job_name`이 만드는 접두사다. 두 곳이 갈리면 수집이 조용히 멈춘다.
_JOB_NAME_PREFIX: Final = "ar-exec-"


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


class PodLogReader(Protocol):
    """수집기가 쓰는 Kubernetes 읽기 연산.

    `JobClient`(Job 생성)와 분리한다 — 책임이 다르고, 섞으면 테스트 더블부터 꼬인다.
    """

    def list_pods(self, namespace: str, job_name: str) -> list: ...

    def read_log(self, namespace: str, pod_name: str, container: str) -> str: ...


class LogSink(Protocol):
    """적재 대상. 구현은 `create_experiment_log`를 부르는 얇은 어댑터다."""

    def write(self, *, idempotency_key: str, log_type: str, content: str) -> None: ...


def collect_container_logs(
    reader: PodLogReader,
    sink: LogSink,
    *,
    namespace: str,
    job_name: str,
    pod_name: str,
    containers: list[str],
    terminated: set[str],
) -> list[str]:
    """한 Job의 컨테이너 로그를 읽어 완성된 청크만 적재하고 사유 코드를 돌려준다.

    **fail-open이다.** 한 컨테이너가 실패해도 나머지를 계속 수집하고, 수집 실패가
    실험 실행을 막지 않는다 — 관측 때문에 파이프라인이 멈추는 것이 더 나쁘다.

    돌려주는 사유 코드는 `executor/phase2.py`의 `_safe_failure_reason` 관례를 따라
    접미사 없는 고정 코드다. 경로·응답 본문은 싣지 않는다.
    """
    problems: list[str] = []
    for container in containers:
        try:
            text = reader.read_log(namespace, pod_name, container)
        except ApiException as error:
            if error.status in _ABSENT_STATUSES:
                # 정상 상황이다 — 다음 주기에 다시 본다.
                continue
            _LOGGER.warning(
                "pod log read failed reason=pod_log_read_failed "
                "job=%s container=%s status=%s",
                job_name,
                container,
                error.status,
            )
            problems.append("pod_log_read_failed")
            continue

        for seq, chunk in enumerate(
            complete_chunks(text, terminated=container in terminated)
        ):
            try:
                sink.write(
                    idempotency_key=log_idempotency_key(pod_name, container, seq),
                    log_type=container,
                    content=chunk,
                )
            except Exception:
                _LOGGER.warning(
                    "log write failed reason=log_write_failed job=%s container=%s",
                    job_name,
                    container,
                )
                problems.append("log_write_failed")
                break
    return problems


class KubernetesPodLogs:
    """`CoreV1Api`로 Pod 목록·컨테이너 로그를 읽는 어댑터.

    변환 로직이 없는 얇은 층이지만, **호출 인자는 테스트로 고정한다** —
    `job-name=` 문자열이 한 글자만 틀려도 빈 목록이 조용히 돌아오고,
    `container=`를 빠뜨리면 첫 컨테이너 로그가 반환돼 단계가 뒤섞인다.
    둘 다 예외가 아니라 잘못된 성공으로 나타나는 종류다.
    """

    def __init__(self, api) -> None:
        self._api = api

    def list_pods(self, namespace: str, job_name: str) -> list:
        """Job이 만든 Pod을 찾는다. `job-name`은 Kubernetes가 자동으로 붙이는 label이다."""
        response = self._api.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"job-name={job_name}",
        )
        return list(response.items)

    def read_log(self, namespace: str, pod_name: str, container: str) -> str:
        """한 컨테이너의 로그 전체를 읽는다.

        `since_seconds`류 증분 조회를 쓰지 않는다 — 같은 창을 다시 읽으면 로그가 자라
        내용이 달라져 멱등키가 충돌한다. 대신 전체를 읽고 고정 경계로 자른다
        (`complete_chunks`).
        """
        return self._api.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container,
        )


def experiment_id_from_job_name(job_name: str) -> uuid.UUID | None:
    """Job 이름에서 Experiment UUID를 복원한다.

    `repository._job_name`의 역함수다 — 그쪽이 `ar-exec-{uuid.hex}`를 만든다.
    두 규칙이 갈리면 수집이 조용히 멈추므로 테스트로 함께 고정한다.

    label로 걸러도 형식이 다른 Job이 섞일 수 있어(예: Phase 1의 `ar-branch-`),
    맞지 않으면 오류가 아니라 `None`으로 돌려 건너뛰게 한다.
    """
    if not job_name.startswith(_JOB_NAME_PREFIX):
        return None
    try:
        return uuid.UUID(hex=job_name[len(_JOB_NAME_PREFIX) :])
    except ValueError:
        return None


class ActiveJobs(Protocol):
    """수집 대상 Job 이름을 주는 연산."""

    def list_active_job_names(self, namespace: str) -> list[str]: ...


def container_states(pod) -> tuple[list[str], set[str]]:
    """Pod 상태에서 컨테이너 이름 순서와 종료된 것들을 뽑는다.

    이름을 코드에 하드코딩하지 않는다 — 컨테이너 통합으로 구성이 바뀌어도 그대로
    따라간다. init container가 먼저 순서대로 돌고 app container가 뒤에 오므로 그 순서를
    유지한다.

    스케줄 직후에는 상태 배열이 아직 `None`이다. 오류가 아니라 빈 결과로 다룬다.
    """
    names: list[str] = []
    terminated: set[str] = set()
    for group in (pod.status.init_container_statuses, pod.status.container_statuses):
        for status in group or []:
            names.append(status.name)
            if getattr(status.state, "terminated", None) is not None:
                terminated.add(status.name)
    return names, terminated


def collect_once(
    jobs: ActiveJobs,
    reader: PodLogReader,
    sink_for: "Callable[[uuid.UUID], LogSink]",
    *,
    namespace: str,
) -> list[str]:
    """한 주기 분량을 수집하고 사유 코드를 모아 돌려준다.

    Job 목록은 Kubernetes에서 얻는다 — DB의 `RUNNING`으로 거르면 `EVALUATING` 전환 뒤에도
    같은 Job이 계속 도는 구간을 놓친다(정본 계약 참조).
    """
    problems: list[str] = []
    for job_name in jobs.list_active_job_names(namespace):
        experiment_id = experiment_id_from_job_name(job_name)
        if experiment_id is None:
            # label로 걸러도 형식이 다른 Job이 섞일 수 있다. 형식 오류는 정상 skip이라
            # 아래 job_collection_failed와 성격이 다르다.
            continue
        try:
            pod = select_pod(reader.list_pods(namespace, job_name))
            if pod is None:
                continue
            containers, terminated = container_states(pod)
            problems.extend(
                collect_container_logs(
                    reader,
                    sink_for(experiment_id),
                    namespace=namespace,
                    job_name=job_name,
                    pod_name=pod.metadata.name,
                    containers=containers,
                    terminated=terminated,
                )
            )
        except Exception:
            # Job 단위로 격리한다. 여기서 새어 나가면 이 Job 하나가 아니라 뒤의 Job
            # 전부가 그 tick에서 날아가고, A가 계속 실패하면 B·C·D는 영영 안 걷힌다.
            _LOGGER.warning(
                "job collection failed reason=job_collection_failed job=%s", job_name
            )
            problems.append("job_collection_failed")
            continue
    return problems
