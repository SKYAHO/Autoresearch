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
import signal
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol

from kubernetes.client.exceptions import ApiException

from agent_orchestration.app.experiments.exceptions import IdempotencyConflictError
from agent_orchestration.app.experiments.schemas import ExperimentLogCreate
from agent_orchestration.app.experiments.service import create_experiment_log
from agent_orchestration.launcher.config import (
    _optional_positive_integer_environment,
    _required_environment,
)
from agent_orchestration.launcher.jobs import EXPERIMENT_EXECUTOR_LABEL_SELECTOR
from agent_orchestration.launcher.resident import run_forever


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


def ordered_pods(pods: list[_Pod]) -> list[_Pod]:
    """수집 대상 Pod을 생성 순서대로 돌려준다.

    Job 생성 직후에는 Pod이 아직 없는 것이 **정상**이므로 빈 목록을 오류로 다루지
    않는다.

    `backoffLimit=1` 재시도로 Pod이 둘이 되면 **둘 다** 수집한다. 실패한 시도의 마지막
    청크가 실패 원인이 찍히는 자리인데, 최신 Pod만 보면 화면에서 정확히 그 구간이
    사라진다 — #559가 없애려던 상황이 그대로 남는다. 멱등키에 `pod_name`이 들어 있어
    두 시도가 섞이지 않는다(`log_idempotency_key`).

    비용은 Pod 수만큼의 API 호출인데 `backoffLimit=1`이라 상한이 2다.
    """
    # API 반환 순서를 신뢰하지 않는다 — 재시도 Pod이 먼저 올지 나중에 올지는 보장이 없다.
    # 오래된 것부터 적재해야 워크벤치의 시간순 읽기와 순서가 맞는다.
    return sorted(pods, key=lambda pod: pod.metadata.creation_timestamp)


class PodLogReader(Protocol):
    """수집기가 쓰는 Kubernetes 읽기 연산.

    `JobClient`(Job 생성)와 분리한다 — 책임이 다르고, 섞으면 테스트 더블부터 꼬인다.
    """

    def list_pods(self, namespace: str, job_name: str) -> list: ...

    def read_log(self, namespace: str, pod_name: str, container: str) -> str: ...


class LogSink(Protocol):
    """적재 대상. 구현은 `create_experiment_log`를 부르는 얇은 어댑터다."""

    def write(self, *, idempotency_key: str, log_type: str, content: str) -> None: ...


class SeqCursor:
    """이미 적재한 청크를 다시 쓰지 않게 하는 프로세스 메모리 high-water mark.

    **영속 커서가 아니다.** 매 tick 로그 전체를 다시 읽어 같은 경계로 자르는 설계는
    그대로 두고, 그중 **쓰기만** 줄인다. `create_experiment_log`는 호출 1회가 곧
    트랜잭션 1회 + SELECT 2회라, 5초 주기로 도는 동안 이미 적재된 청크까지 매번
    다시 태우면 실험 1건이 수만 건을 만든다. 그중 첫 tick 이후는 전부 no-op이다.

    정확성 전제는 그대로다 — 재시작하면 이 표가 비고, 그러면 지금까지처럼 전부 다시
    계산해 같은 키·같은 내용으로 올린다. 적재분이 불변이므로 결과가 같다.
    """

    def __init__(self) -> None:
        self._next: dict[tuple[str, str], int] = {}

    def next_seq(self, pod_name: str, container: str) -> int:
        """다음에 써야 할 `seq`. 처음 보는 조합이면 0이다."""
        return self._next.get((pod_name, container), 0)

    def mark_written(self, pod_name: str, container: str, seq: int) -> None:
        """`seq`까지 처리했다고 기록한다. 되돌아가지 않는다."""
        key = (pod_name, container)
        if seq + 1 > self._next.get(key, 0):
            self._next[key] = seq + 1

    def retain(self, pod_names: set[str]) -> None:
        """이번 tick에 보이지 않은 Pod의 항목을 버린다.

        상주 프로세스라 Pod마다 항목이 쌓이면 그대로 누수다. 잘못 버려도 손실은 없다 —
        다시 0부터 계산해 멱등 no-op이 될 뿐이다.
        """
        for key in [key for key in self._next if key[0] not in pod_names]:
            del self._next[key]


def collect_container_logs(
    reader: PodLogReader,
    sink: LogSink,
    *,
    namespace: str,
    job_name: str,
    pod_name: str,
    containers: list[str],
    terminated: set[str],
    cursor: SeqCursor | None = None,
) -> list[str]:
    """한 Job의 컨테이너 로그를 읽어 완성된 청크만 적재하고 사유 코드를 돌려준다.

    **fail-open이다.** 한 컨테이너가 실패해도 나머지를 계속 수집하고, 수집 실패가
    실험 실행을 막지 않는다 — 관측 때문에 파이프라인이 멈추는 것이 더 나쁘다.

    돌려주는 사유 코드는 `executor/phase2.py`의 `_safe_failure_reason` 관례를 따라
    접미사 없는 고정 코드다. 경로·응답 본문은 싣지 않는다.
    """
    problems: list[str] = []
    cursor = cursor if cursor is not None else SeqCursor()
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

        start = cursor.next_seq(pod_name, container)
        for seq, chunk in enumerate(
            complete_chunks(text, terminated=container in terminated)
        ):
            if seq < start:
                continue
            try:
                sink.write(
                    idempotency_key=log_idempotency_key(pod_name, container, seq),
                    log_type=container,
                    content=chunk,
                )
                cursor.mark_written(pod_name, container, seq)
            except IdempotencyConflictError:
                # 같은 키에 다른 내용 — 재시도해도 같은 결과다. 여기서 멈추면 그
                # 컨테이너 로그가 Pod 수명 내내 이 지점에서 끊기므로 이 청크만 버린다.
                # 일시 장애와 뭉치지 않는 이유는 대응이 다르기 때문이다 — 이쪽은
                # 키 규칙이나 로그 회전 같은 설계 신호이지 기다려서 낫는 종류가 아니다.
                _LOGGER.warning(
                    "log write conflicted reason=log_write_conflict "
                    "job=%s container=%s seq=%s",
                    job_name,
                    container,
                    seq,
                )
                problems.append("log_write_conflict")
                # 지나간 것으로 표시한다 — 다음 tick에 같은 충돌을 되풀이하지 않는다.
                cursor.mark_written(pod_name, container, seq)
                continue
            except Exception:
                # 일시 장애로 본다. 건너뛰지 않고 멈춘다 — 저장된 커서가 없어 다음
                # tick이 같은 청크를 다시 계산하므로 그대로 회복된다. 지나쳐 버리면
                # 그 구간이 영영 비고 화면에는 로그가 이어진 것처럼 보인다.
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
        """한 컨테이너의 로그 전체를 UTF-8 원문으로 읽는다.

        `since_seconds`류 증분 조회를 쓰지 않는다 — 같은 창을 다시 읽으면 로그가 자라
        내용이 달라져 멱등키가 충돌한다. 대신 전체를 읽고 고정 경계로 자른다
        (`complete_chunks`).

        **`_preload_content=False`로 받아 직접 디코드한다.** 기본값(`True`)으로 두면
        client가 `str`로 역직렬화하는데, Pod 로그 응답은 JSON이 아니라 plain text라
        `api_client.deserialize`의 `json.loads`가 `ValueError`로 떨어지고 bytes가 그대로
        `__deserialize_primitive(data, str)`에 들어간다. 거기서 `str(bytes)`가 호출되므로
        **디코드가 아니라 repr**이 나온다 — 반환 타입 힌트는 `str`이지만 내용은
        `b'INFO:...\\n'`이고, 줄바꿈은 리터럴 `\\n`, 한글은 `\\xed\\x95\\x9c`로 깨진다(#661).

        `errors="replace"`인 이유는 컨테이너가 UTF-8이 아닌 바이트를 찍을 수 있고, 그때
        예외를 올리면 그 stage의 로그가 통째로 사라지기 때문이다 — 수집은 진단용이므로
        일부가 깨져도 남기는 쪽이 낫다.
        """
        response = self._api.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container,
            _preload_content=False,
        )
        return response.data.decode("utf-8", errors="replace")


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
    cursor: SeqCursor | None = None,
) -> list[str]:
    """한 주기 분량을 수집하고 사유 코드를 모아 돌려준다.

    Job 목록은 Kubernetes에서 얻는다 — DB의 `RUNNING`으로 거르면 `EVALUATING` 전환 뒤에도
    같은 Job이 계속 도는 구간을 놓친다(정본 계약 참조).
    """
    problems: list[str] = []
    cursor = cursor if cursor is not None else SeqCursor()
    seen_pods: set[str] = set()
    for job_name in jobs.list_active_job_names(namespace):
        experiment_id = experiment_id_from_job_name(job_name)
        if experiment_id is None:
            # label로 걸러도 형식이 다른 Job이 섞일 수 있다. 형식 오류는 정상 skip이라
            # 아래 job_collection_failed와 성격이 다르다.
            continue
        try:
            sink = sink_for(experiment_id)
            for pod in ordered_pods(reader.list_pods(namespace, job_name)):
                containers, terminated = container_states(pod)
                seen_pods.add(pod.metadata.name)
                problems.extend(
                    collect_container_logs(
                        reader,
                        sink,
                        namespace=namespace,
                        job_name=job_name,
                        pod_name=pod.metadata.name,
                        containers=containers,
                        terminated=terminated,
                        cursor=cursor,
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
    cursor.retain(seen_pods)
    return problems


class KubernetesActiveJobs:
    """`BatchV1Api`로 executor Job 이름을 얻는 어댑터.

    launcher와 같은 `EXPERIMENT_EXECUTOR_LABEL_SELECTOR` 상수를 쓴다 — 두 곳이 갈리면
    수집 대상이 조용히 어긋난다.

    종료된 Job도 TTL 동안 목록에 남는데 이는 필요한 성질이다. 완료 직후 마지막 부분
    청크를 flush하려면 종료 여부를 알아야 하고, 그 판정을 Pod 상태에서 얻는다.
    """

    def __init__(self, api) -> None:
        self._api = api

    def list_active_job_names(self, namespace: str) -> list[str]:
        response = self._api.list_namespaced_job(
            namespace=namespace,
            label_selector=EXPERIMENT_EXECUTOR_LABEL_SELECTOR,
        )
        return [job.metadata.name for job in response.items]


class DatabaseLogSink:
    """`create_experiment_log`를 부르는 어댑터.

    HTTP API를 거치지 않는다 — launcher 이미지가 `app` 패키지를 포함하고 이미 DB
    세션을 쓰므로 서비스 함수를 직접 부른다. API 토큰·egress가 불필요해지는 이유다.

    `create` 주입은 테스트용이다. `ExperimentLogCreate` 조립이 틀리면 실행 시점에
    422로만 드러나므로 인자를 테스트로 고정한다.
    """

    def __init__(
        self,
        session,
        experiment_id: uuid.UUID,
        *,
        create: "Callable[..., object] | None" = None,
    ) -> None:
        self._session = session
        self._experiment_id = experiment_id
        self._create = create

    def write(self, *, idempotency_key: str, log_type: str, content: str) -> None:
        create = self._create
        request = ExperimentLogCreate(
            idempotency_key=idempotency_key,
            log_type=log_type,
            content=content,
        )
        if create is None:
            create = create_experiment_log
        create(self._session, self._experiment_id, request)


@dataclass(frozen=True)
class LogCollectorSettings:
    """수집기가 실제로 쓰는 설정만 담는다.

    `LauncherSettings`를 재사용하지 않는다 — 그쪽은 Job 생성에 필요한 값 7개를 필수로
    요구하고 `ORCH_EXECUTOR_IMAGE`는 digest 형식 검증까지 한다. 수집기는 Job을 만들지
    않는데도 그 값을 줘야 하고, **executor 릴리스마다 수집기 매니페스트를 따라 고쳐야
    한다.** 안 쓰는 값에 배포가 묶인다.

    수집기가 실제로 쓰는 것은 셋뿐이다.
    """

    database_url: str
    job_namespace: str
    log_collect_interval_sec: int = 5

    @classmethod
    def from_environment(cls) -> "LogCollectorSettings":
        """환경 변수에서 설정을 읽는다. 없으면 기동 시점에 막는다."""
        return cls(
            database_url=_required_environment("ORCH_DATABASE_URL"),
            job_namespace=_required_environment("ORCH_JOB_NAMESPACE"),
            log_collect_interval_sec=_optional_positive_integer_environment(
                "ORCH_LOG_COLLECT_INTERVAL_SEC",
                default=5,
            ),
        )


def main() -> int:
    """상주 수집기 진입점.

    engine은 프로세스당 1회 만들고 세션은 tick마다 연다 — `launcher/main.py`와 같은
    방식이며, 차이는 이 프로세스가 상주한다는 것뿐이다. 세션을 오래 들고 있으면
    트랜잭션·identity map이 누적되고 한 번 끊긴 커넥션이 프로세스 수명 내내 따라온다.
    """
    from kubernetes import client, config as kube_config

    from agent_orchestration.app.database import (
        create_database_engine,
        create_session_factory,
    )
    logging.basicConfig(level=logging.INFO)
    settings = LogCollectorSettings.from_environment()
    kube_config.load_incluster_config()

    jobs = KubernetesActiveJobs(client.BatchV1Api())
    reader = KubernetesPodLogs(client.CoreV1Api())
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)

    stopping = False

    def _request_stop(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    # 커서는 프로세스 수명 동안 산다. 세션은 tick마다 새로 열지만 이건 DB 상태가
    # 아니라 "이번 프로세스가 이미 쓴 것" 메모다 — 재시작하면 비고, 그러면 전부 다시
    # 계산해 멱등 no-op이 된다.
    cursor = SeqCursor()

    def tick() -> list[str]:
        with session_factory() as session:
            return collect_once(
                jobs,
                reader,
                lambda experiment_id: DatabaseLogSink(session, experiment_id),
                namespace=settings.job_namespace,
                cursor=cursor,
            )

    try:
        _LOGGER.info(
            "log collector started namespace=%s interval=%ss",
            settings.job_namespace,
            settings.log_collect_interval_sec,
        )
        run_forever(
            tick,
            should_stop=lambda: stopping,
            sleep=time.sleep,
            interval_sec=settings.log_collect_interval_sec,
            label="log collector",
        )
    finally:
        engine.dispose()
    _LOGGER.info("log collector stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
