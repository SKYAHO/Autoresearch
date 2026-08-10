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

import uuid  # noqa: E402

from agent_orchestration.app.experiments.exceptions import (  # noqa: E402
    IdempotencyConflictError,
)
from agent_orchestration.app.experiments.schemas import (  # noqa: E402
    ExperimentLogCreate,
)
from agent_orchestration.launcher.config import LauncherSettings  # noqa: E402
from agent_orchestration.launcher.jobs import build_executor_job  # noqa: E402
from agent_orchestration.launcher.repository import ClaimedExperiment  # noqa: E402
from agent_orchestration.launcher.log_collector import (  # noqa: E402
    CHUNK_SIZE,
    complete_chunks,
    KubernetesPodLogs,
    DatabaseLogSink,
    run_forever,
    KubernetesActiveJobs,
    LogCollectorSettings,
    collect_once,
    container_states,
    experiment_id_from_job_name,
    LogSink,
    PodLogReader,
    collect_container_logs,
    log_idempotency_key,
    ordered_pods,
    SeqCursor,
)


_EXECUTOR_EXPERIMENT_ID = uuid.UUID("12345678-1234-5678-1234-567812345678")


def _executor_settings() -> LauncherSettings:
    """`build_executor_job`을 부르기 위한 최소 설정이다(수집기가 쓰는 값이 아니다)."""
    return LauncherSettings(
        database_url="postgresql://launcher:password@db/orchestration",
        job_namespace="agent-orchestration",
        executor_image=(
            "asia-northeast3-docker.pkg.dev/example/executor@sha256:" + "b" * 64
        ),
        executor_service_account="experiment-executor",
        executor_node_pool="batch-od",
        github_app_secret_name="experiment-app",
        github_app_id=123,
        github_app_installation_id=456,
        github_repository="SKYAHO/Autoresearch",
        max_concurrent_experiments=2,
        executor_api_url="http://agent-orchestration-api",
        executor_api_token_secret_name="executor-api-token",
        codex_home_secret_name="codex-auth",
        workspace_size_limit="8Gi",
        codex_timeout_sec=900,
        active_deadline_sec=2700,
    )


def _executor_claim() -> ClaimedExperiment:
    return ClaimedExperiment(
        experiment_id=_EXECUTOR_EXPERIMENT_ID,
        issue_number=574,
        issue_branch="exp/574-demo",
        base_dev_sha="a" * 40,
        job_name=f"ar-exec-{_EXECUTOR_EXPERIMENT_ID.hex}",
    )


class _Pod:
    """`creationTimestamp`와 이름만 갖는 최소 Pod double."""

    def __init__(self, name: str, created: datetime) -> None:
        self.metadata = type(
            "Meta", (), {"name": name, "creation_timestamp": created}
        )()


def test_ordered_pods_is_empty_when_no_pod_exists() -> None:
    """Job 생성 직후 스케줄링 지연은 정상이다 — 오류가 아니라 없음으로 다룬다."""
    assert ordered_pods([]) == []


def test_ordered_pods_returns_the_only_pod() -> None:
    pod = _Pod("ar-exec-abc-x7k2", datetime(2026, 8, 9, 12, 0, tzinfo=UTC))

    assert ordered_pods([pod]) == [pod]


def test_ordered_pods_keeps_the_failed_attempt_and_orders_it_first() -> None:
    """`backoffLimit=1` 재시도로 Pod이 둘이 되면 **둘 다** 수집한다.

    실패한 시도의 마지막 청크가 실패 원인이 찍히는 자리다 — 최신 Pod만 보면 화면에서
    바로 그 구간이 사라진다. 멱등키에 `pod_name`이 들어 있어 섞이지 않는다.

    오래된 것을 먼저 돌려주는 이유는 워크벤치가 적재 순서대로 읽기 때문이다.
    API 반환 순서에 의존하지 않는지 보려고 입력 순서를 뒤집어서도 확인한다.
    """
    older = _Pod("ar-exec-abc-x7k2", datetime(2026, 8, 9, 12, 0, tzinfo=UTC))
    newer = _Pod("ar-exec-abc-m9p4", datetime(2026, 8, 9, 12, 30, tzinfo=UTC))

    assert ordered_pods([older, newer]) == [older, newer]
    assert ordered_pods([newer, older]) == [older, newer]


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


def test_every_executor_container_name_fits_the_log_type_limit() -> None:
    """`ExperimentLogCreate.log_type`이 max_length=32이고 거기에 컨테이너 이름이 들어간다.

    이름을 상수로 적지 않고 `build_executor_job`이 실제로 만든 것을 순회한다 —
    컨테이너 통합(8 → 4/5)으로 이름이 바뀌면 자동으로 걸린다.

    넘치면 조용히 깨진다. `ExperimentLogCreate` 조립이 `ValidationError`를 내고 그건
    `collect_container_logs`의 쓰기 실패 갈래로 떨어져, 화면에는 오류가 아니라 "그
    단계 로그가 없음"으로 보인다.
    """
    limit = ExperimentLogCreate.model_fields["log_type"].metadata[-1].max_length
    job = build_executor_job(_executor_claim(), _executor_settings())
    spec = job.spec.template.spec
    names = [
        container.name
        for container in list(spec.init_containers or []) + list(spec.containers or [])
    ]

    assert names, "컨테이너를 하나도 못 찾았다 — 테스트가 무해해졌다"
    too_long = [name for name in names if len(name) > limit]
    assert not too_long, f"log_type 상한 {limit}자를 넘는 컨테이너 이름: {too_long}"


class _FakeReader:
    """`PodLogReader` 프로토콜의 테스트 더블."""

    def __init__(self, *, pods=None, logs=None, raises=None) -> None:
        self._pods = pods if pods is not None else []
        self._logs = logs or {}
        self._raises = raises or {}

    def list_pods(self, namespace: str, job_name: str) -> list:
        return self._pods

    def read_log(self, namespace: str, pod_name: str, container: str) -> str:
        if container in self._raises:
            raise self._raises[container]
        return self._logs.get(container, "")


class _FakeSink:
    """적재된 청크를 기록만 하는 `LogSink` 더블."""

    def __init__(self, *, raises=None, raises_for=None) -> None:
        self.written: list[tuple[str, str, str]] = []
        self._raises = raises
        self._raises_for = raises_for or {}

    def write(self, *, idempotency_key: str, log_type: str, content: str) -> None:
        if self._raises is not None:
            raise self._raises
        if idempotency_key in self._raises_for:
            raise self._raises_for[idempotency_key]
        self.written.append((idempotency_key, log_type, content))


def test_fakes_satisfy_the_collector_protocols() -> None:
    """더블이 프로토콜을 실제로 만족하는지 고정한다 — 시그니처가 갈리면 여기서 걸린다."""
    reader: PodLogReader = _FakeReader()
    sink: LogSink = _FakeSink()

    assert reader.list_pods("ns", "job") == []
    sink.write(idempotency_key="k", log_type="t", content="c")


def _api_exception(status: int) -> Exception:
    from kubernetes.client.exceptions import ApiException

    return ApiException(status=status)


def test_collect_skips_silently_when_the_container_has_not_started() -> None:
    """8-container 순차 실행이라 뒤 컨테이너가 아직 없는 것은 정상이다."""
    pod = _Pod("ar-exec-abc-x7k2", datetime(2026, 8, 9, 12, 0, tzinfo=UTC))
    reader = _FakeReader(pods=[pod], raises={"codex-worker": _api_exception(400)})
    sink = _FakeSink()

    problems = collect_container_logs(
        reader, sink, namespace="ns", job_name="ar-exec-abc",
        pod_name="ar-exec-abc-x7k2",
        containers=["codex-worker"], terminated=set(),
    )

    assert sink.written == []
    assert problems == []


def test_collect_skips_silently_when_the_pod_vanished_between_list_and_read() -> None:
    """TTL 회수·재시도 교체로 list 직후 Pod이 사라질 수 있다 — 404도 정상 skip이다.

    지금은 "컨테이너 미시작 404"와 같은 갈래로 처리하지만, 나중에 둘을 구분해야 할
    때 회귀 없이 갈리도록 레이스 자체를 케이스로 고정한다.
    """
    pod = _Pod("ar-exec-abc-x7k2", datetime(2026, 8, 9, 12, 0, tzinfo=UTC))
    reader = _FakeReader(pods=[pod], raises={"codex-worker": _api_exception(404)})
    sink = _FakeSink()

    problems = collect_container_logs(
        reader, sink, namespace="ns", job_name="ar-exec-abc",
        pod_name="ar-exec-abc-x7k2",
        containers=["codex-worker"], terminated=set(),
    )

    assert sink.written == []
    assert problems == []


def test_collect_reports_unexpected_api_errors_without_stopping() -> None:
    """그 외 API 오류는 조용히 넘기지 않는다 — 고정 사유 코드로 남긴다."""
    pod = _Pod("ar-exec-abc-x7k2", datetime(2026, 8, 9, 12, 0, tzinfo=UTC))
    reader = _FakeReader(
        pods=[pod],
        logs={"candidate-verifier": "x" * CHUNK_SIZE},
        raises={"codex-worker": _api_exception(500)},
    )
    sink = _FakeSink()

    problems = collect_container_logs(
        reader, sink, namespace="ns", job_name="ar-exec-abc",
        pod_name="ar-exec-abc-x7k2",
        containers=["codex-worker", "candidate-verifier"], terminated=set(),
    )

    assert problems == ["pod_log_read_failed"]
    # 한 컨테이너가 실패해도 나머지는 계속 수집한다.
    assert [row[1] for row in sink.written] == ["candidate-verifier"]


def test_collect_reports_sink_failure_without_stopping() -> None:
    """DB 적재 실패도 사유를 남기고 넘어간다 — 관측 때문에 실험을 막지 않는다."""
    pod = _Pod("ar-exec-abc-x7k2", datetime(2026, 8, 9, 12, 0, tzinfo=UTC))
    reader = _FakeReader(pods=[pod], logs={"codex-worker": "x" * CHUNK_SIZE})
    sink = _FakeSink(raises=RuntimeError("db down"))

    problems = collect_container_logs(
        reader, sink, namespace="ns", job_name="ar-exec-abc",
        pod_name="ar-exec-abc-x7k2",
        containers=["codex-worker"], terminated=set(),
    )

    assert problems == ["log_write_failed"]


def test_transient_write_failure_stops_the_rest_of_that_container() -> None:
    """DB가 죽었을 때 남은 청크로 계속 두드리지 않는다 — 다음 tick에 그대로 회복된다.

    건너뛰지 않는 것이 핵심이다. 실패한 청크를 지나쳐 버리면 저장된 커서가 없는
    설계에서 그 구간은 영영 비고, 화면에는 로그가 이어진 것처럼 보인다.
    """
    reader = _FakeReader(logs={"codex-worker": "x" * (CHUNK_SIZE * 3)})
    sink = _FakeSink(
        raises_for={"ar-exec-abc-x7k2:codex-worker:1": RuntimeError("db down")}
    )

    problems = collect_container_logs(
        reader, sink, namespace="ns", job_name="ar-exec-abc",
        pod_name="ar-exec-abc-x7k2",
        containers=["codex-worker"], terminated=set(),
    )

    assert problems == ["log_write_failed"]
    assert [row[0] for row in sink.written] == ["ar-exec-abc-x7k2:codex-worker:0"]


def test_idempotency_conflict_gets_its_own_reason_and_skips_only_that_chunk() -> None:
    """충돌은 DB 일시 장애와 다르다 — 재시도해도 낫지 않으므로 그 청크만 버린다.

    운영 중 로그만 보고 "키 규칙 결함"과 "DB 일시 장애"를 구분할 수 있어야 한다
    (정본 계약의 오류 분류표). 같은 사유 코드로 뭉치면 둘 다 `log_write_failed`로
    보여 어느 쪽인지 알 수 없다.

    `break`가 아니라 계속 가는 이유는, 충돌은 다음 tick에도 같은 지점에서 같은 결과가
    나오기 때문이다. 멈추면 그 컨테이너 로그가 Pod 수명 내내 그 지점에서 통째로
    끊긴다 — 구멍 하나가 blackout보다 낫다.
    """
    reader = _FakeReader(logs={"codex-worker": "x" * (CHUNK_SIZE * 3)})
    sink = _FakeSink(
        raises_for={
            "ar-exec-abc-x7k2:codex-worker:1": IdempotencyConflictError("dup"),
        }
    )

    problems = collect_container_logs(
        reader, sink, namespace="ns", job_name="ar-exec-abc",
        pod_name="ar-exec-abc-x7k2",
        containers=["codex-worker"], terminated=set(),
    )

    assert problems == ["log_write_conflict"]
    assert [row[0] for row in sink.written] == [
        "ar-exec-abc-x7k2:codex-worker:0",
        "ar-exec-abc-x7k2:codex-worker:2",
    ]


def test_collect_writes_complete_chunks_with_pod_scoped_keys() -> None:
    pod = _Pod("ar-exec-abc-x7k2", datetime(2026, 8, 9, 12, 0, tzinfo=UTC))
    reader = _FakeReader(pods=[pod], logs={"codex-worker": "x" * (CHUNK_SIZE * 2)})
    sink = _FakeSink()

    collect_container_logs(
        reader, sink, namespace="ns", job_name="ar-exec-abc",
        pod_name="ar-exec-abc-x7k2",
        containers=["codex-worker"], terminated=set(),
    )

    assert [row[0] for row in sink.written] == [
        "ar-exec-abc-x7k2:codex-worker:0",
        "ar-exec-abc-x7k2:codex-worker:1",
    ]


def test_collect_skips_chunks_it_already_wrote_in_an_earlier_tick() -> None:
    """이미 적재한 청크를 매 tick 다시 쓰지 않는다.

    `create_experiment_log`는 호출 1회가 곧 트랜잭션 1회 + SELECT 2회다. 5초 주기로
    31분을 도는 실험에서 컨테이너 하나만 500KB로 자라면 그 컨테이너 몫만 수만 건이
    되고, 첫 tick 이후는 전부 no-op이다.
    """
    cursor = SeqCursor()
    sink = _FakeSink()
    first = _FakeReader(logs={"codex-worker": "x" * (CHUNK_SIZE * 2)})
    later = _FakeReader(logs={"codex-worker": "x" * (CHUNK_SIZE * 3)})

    for reader in (first, later):
        collect_container_logs(
            reader, sink, namespace="ns", job_name="ar-exec-abc",
            pod_name="ar-exec-abc-x7k2",
            containers=["codex-worker"], terminated=set(), cursor=cursor,
        )

    assert [row[0] for row in sink.written] == [
        "ar-exec-abc-x7k2:codex-worker:0",
        "ar-exec-abc-x7k2:codex-worker:1",
        "ar-exec-abc-x7k2:codex-worker:2",
    ]


def test_a_restarted_collector_rewrites_everything() -> None:
    """커서는 프로세스 메모리에만 산다 — 재시작하면 비고 전부 다시 계산한다.

    영속 커서를 두지 않는 대신 이 성질에 기댄다. 적재분이 불변이라 다시 써도 같은
    내용이고, `create_experiment_log`가 기존 row를 그대로 돌려준다.
    """
    sink = _FakeSink()
    reader = _FakeReader(logs={"codex-worker": "x" * (CHUNK_SIZE * 2)})

    for _restart in range(2):
        collect_container_logs(
            reader, sink, namespace="ns", job_name="ar-exec-abc",
            pod_name="ar-exec-abc-x7k2",
            containers=["codex-worker"], terminated=set(), cursor=SeqCursor(),
        )

    assert len(sink.written) == 4


def test_seq_cursor_forgets_pods_that_are_no_longer_observed() -> None:
    """상주 프로세스라 Pod마다 항목이 쌓이면 그대로 누수다 — 사라진 Pod은 버린다."""
    cursor = SeqCursor()
    cursor.mark_written("ar-exec-abc-x7k2", "codex-worker", 4)
    cursor.mark_written("ar-exec-abc-m9p4", "codex-worker", 2)

    cursor.retain({"ar-exec-abc-m9p4"})

    assert cursor.next_seq("ar-exec-abc-m9p4", "codex-worker") == 3
    assert cursor.next_seq("ar-exec-abc-x7k2", "codex-worker") == 0


class _MultiPodReader:
    """Pod마다 다른 로그를 돌려주는 reader — 재시도 시나리오용."""

    def __init__(self, pods, logs_by_pod) -> None:
        self._pods = pods
        self._logs_by_pod = logs_by_pod

    def list_pods(self, namespace: str, job_name: str) -> list:
        return self._pods

    def read_log(self, namespace: str, pod_name: str, container: str) -> str:
        return self._logs_by_pod[pod_name]


def test_collect_once_collects_both_the_failed_attempt_and_the_retry() -> None:
    """`backoffLimit=1` 재시도가 앞 시도의 로그를 지우지 않는다.

    실패한 Pod의 마지막 청크에 실패 원인이 찍힌다. 최신 Pod만 걷으면 화면에서 정확히
    그 구간이 사라지고, 재현하려면 클러스터를 직접 봐야 한다 — #559가 없애려던 상황이
    그대로 남는다.

    실패한 Pod을 먼저 적재해 워크벤치의 시간순 읽기와 순서를 맞춘다.
    """
    job_name = "ar-exec-6ec09890a4a84c699760c01349351505"
    failed = _PodWithStatus(
        f"{job_name}-x7k2",
        datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
        ([_status("codex-worker", terminated=True)], None),
    )
    retry = _PodWithStatus(
        f"{job_name}-m9p4",
        datetime(2026, 8, 9, 12, 30, tzinfo=UTC),
        ([_status("codex-worker", terminated=False)], None),
    )
    reader = _MultiPodReader(
        [retry, failed],  # API 반환 순서를 신뢰하지 않는지 보려고 뒤집어 둔다.
        {
            failed.metadata.name: "Traceback: boom",
            retry.metadata.name: "x" * CHUNK_SIZE,
        },
    )
    sink = _FakeSink()

    problems = collect_once(
        _FakeJobs([job_name]), reader, lambda _experiment_id: sink, namespace="ns"
    )

    assert problems == []
    assert [(row[0], row[2]) for row in sink.written] == [
        (f"{job_name}-x7k2:codex-worker:0", "Traceback: boom"),
        (f"{job_name}-m9p4:codex-worker:0", "x" * CHUNK_SIZE),
    ]


def test_collect_once_skips_a_job_whose_pod_is_not_scheduled_yet() -> None:
    """Job 생성 직후 Pod이 아직 없는 것은 정상이다 — tick이 조용히 넘어간다."""
    sink = _FakeSink()

    problems = collect_once(
        _FakeJobs(["ar-exec-6ec09890a4a84c699760c01349351505"]),
        _FakeReader(pods=[]),
        lambda _experiment_id: sink,
        namespace="ns",
    )

    assert sink.written == []
    assert problems == []


class _RecordingCoreV1:
    """호출 인자를 기록만 하는 `CoreV1Api` 더블."""

    def __init__(self, log_body: bytes = b"output") -> None:
        self.list_calls: list[dict] = []
        self.log_calls: list[dict] = []
        self.log_body = log_body

    def list_namespaced_pod(self, **kwargs):
        self.list_calls.append(kwargs)
        return type("Resp", (), {"items": []})()

    def read_namespaced_pod_log(self, **kwargs):
        """`_preload_content=False`일 때의 실제 반환을 흉내낸다.

        kubernetes client는 이 옵션에서 역직렬화를 건너뛰고 `.data`가 bytes인 HTTP
        응답 객체를 그대로 돌려준다. 문자열을 돌려주는 더블을 쓰면 #661의 회귀를
        잡지 못한다 — 그 버그는 bytes를 `str()`에 넣어 생긴 것이었다.
        """
        self.log_calls.append(kwargs)
        return type("Resp", (), {"data": self.log_body})()


def test_pod_logs_adapter_filters_by_the_job_name_label() -> None:
    """`job-name=` 문자열이 한 글자만 틀려도 조용히 빈 목록이 온다 — 인자를 고정한다."""
    api = _RecordingCoreV1()

    KubernetesPodLogs(api).list_pods("autoresearch-experiments", "ar-exec-abc")

    assert api.list_calls == [
        {
            "namespace": "autoresearch-experiments",
            "label_selector": "job-name=ar-exec-abc",
        }
    ]


def test_pod_logs_adapter_passes_the_container_name() -> None:
    """`container=`를 빠뜨리면 첫 컨테이너 로그가 조용히 반환돼 단계가 뒤섞인다."""
    api = _RecordingCoreV1()

    text = KubernetesPodLogs(api).read_log("ns", "ar-exec-abc-x7k2", "codex-worker")

    assert text == "output"
    assert api.log_calls == [
        {
            "name": "ar-exec-abc-x7k2",
            "namespace": "ns",
            "container": "codex-worker",
            "_preload_content": False,
        }
    ]


def test_pod_logs_adapter_decodes_instead_of_stringifying_bytes() -> None:
    """로그를 repr이 아니라 UTF-8 원문으로 읽는다.

    `_preload_content` 기본값(`True`)으로 두면 client가 plain text 응답을 JSON으로
    파싱하려다 실패하고, bytes를 그대로 `str()`에 넣어 **repr**을 만든다. 그러면
    `b'` 접두사가 붙고 줄바꿈이 리터럴 `\\n`이 되며 한글이 `\\xed\\x95\\x9c`로 깨진다.
    실측으로 실험 로그 4건이 전부 이 상태였다(#661).
    """
    api = _RecordingCoreV1(log_body="INFO:__main__:단계 시작\nINFO:__main__:완료\n".encode())

    text = KubernetesPodLogs(api).read_log("ns", "ar-exec-abc-x7k2", "codex-worker")

    assert text == "INFO:__main__:단계 시작\nINFO:__main__:완료\n"
    assert not text.startswith("b'")
    assert "\\n" not in text
    assert "단계 시작" in text


def test_pod_logs_adapter_keeps_undecodable_bytes_instead_of_raising() -> None:
    """UTF-8이 아닌 바이트가 섞여도 그 stage의 로그를 통째로 잃지 않는다.

    수집은 진단용이므로 일부가 깨져도 남기는 쪽이 낫다.
    """
    api = _RecordingCoreV1(log_body=b"ok \xff\xfe tail")

    text = KubernetesPodLogs(api).read_log("ns", "pod", "container")

    assert text.startswith("ok ")
    assert text.endswith(" tail")


def test_experiment_id_from_job_name_inverts_the_launcher_rule() -> None:
    """`repository._job_name`이 만든 이름의 역함수다 — 두 규칙이 갈리면 수집이 멈춘다."""
    import uuid as _uuid

    experiment_id = _uuid.UUID("6ec09890-a4a8-4c69-9760-c01349351505")

    assert experiment_id_from_job_name(f"ar-exec-{experiment_id.hex}") == experiment_id


def test_experiment_id_from_job_name_rejects_foreign_names() -> None:
    """label로 걸러도 다른 Job이 섞일 수 있다 — 형식이 어긋나면 조용히 건너뛴다."""
    assert experiment_id_from_job_name("ar-branch-6ec09890a4a84c699760c01349351505") is None
    assert experiment_id_from_job_name("ar-exec-notahex") is None
    assert experiment_id_from_job_name("ar-exec-") is None


class _PodWithStatus:
    """`initContainerStatuses`·`containerStatuses`를 갖는 Pod double."""

    def __init__(self, name: str, created: datetime, statuses) -> None:
        self.metadata = type("Meta", (), {"name": name, "creation_timestamp": created})()
        init, main = statuses
        self.status = type(
            "Status", (), {"init_container_statuses": init, "container_statuses": main}
        )()


def _status(name: str, *, terminated: bool):
    state = type("State", (), {"terminated": object() if terminated else None})()
    return type("CS", (), {"name": name, "state": state})()


def test_container_states_reads_names_and_termination_from_the_pod() -> None:
    """컨테이너 이름을 하드코딩하지 않는다 — 통합으로 구성이 바뀌어도 따라간다."""
    pod = _PodWithStatus(
        "ar-exec-abc-x7k2",
        datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
        (
            [_status("workspace-preparer", terminated=True),
             _status("codex-worker", terminated=False)],
            [_status("candidate-finalizer", terminated=False)],
        ),
    )

    names, terminated = container_states(pod)

    assert names == ["workspace-preparer", "codex-worker", "candidate-finalizer"]
    assert terminated == {"workspace-preparer"}


def test_container_states_tolerates_a_pod_without_statuses_yet() -> None:
    """스케줄 직후에는 상태 배열이 None이다 — 오류가 아니라 빈 결과다."""
    pod = _PodWithStatus(
        "ar-exec-abc-x7k2", datetime(2026, 8, 9, 12, 0, tzinfo=UTC), (None, None)
    )

    assert container_states(pod) == ([], set())


class _FakeJobs:
    def __init__(self, names: list[str]) -> None:
        self._names = names

    def list_active_job_names(self, namespace: str) -> list[str]:
        return self._names


def test_collect_once_skips_job_names_that_are_not_experiments() -> None:
    """label로 걸러도 형식이 다른 Job이 섞일 수 있다 — 조용히 건너뛴다."""
    reader = _FakeReader(pods=[])
    sink = _FakeSink()

    problems = collect_once(
        _FakeJobs(["ar-branch-6ec09890a4a84c699760c01349351505"]),
        reader,
        lambda _experiment_id: sink,
        namespace="ns",
    )

    assert problems == []
    assert sink.written == []


class _ExplodingReader:
    """첫 Job에서만 분류 안 된 예외를 던지는 reader."""

    def __init__(self, bad_job_pod_name: str, good_pod) -> None:
        self._bad = bad_job_pod_name
        self._good = good_pod

    def list_pods(self, namespace: str, job_name: str) -> list:
        if job_name == self._bad:
            raise RuntimeError("transient API failure")
        return [self._good]

    def read_log(self, namespace: str, pod_name: str, container: str) -> str:
        return "x" * CHUNK_SIZE


def test_collect_once_isolates_a_failing_job_from_the_rest() -> None:
    """한 Job이 죽어도 뒤의 Job은 계속 걷는다.

    Job 단위로 격리하지 않으면 A가 계속 실패하는 동안 그 뒤의 B·C·D가 영영 수집되지
    않는다 — tick 하나가 아니라 그 Job 이후 전부를 잃는다.
    """
    good_pod = _PodWithStatus(
        "ar-exec-bbb-x7k2",
        datetime(2026, 8, 9, 12, 0, tzinfo=UTC),
        ([_status("codex-worker", terminated=False)], None),
    )
    bad = "ar-exec-" + "a" * 32
    good = "ar-exec-" + "b" * 32
    sink = _FakeSink()

    problems = collect_once(
        _FakeJobs([bad, good]),
        _ExplodingReader(bad, good_pod),
        lambda _experiment_id: sink,
        namespace="ns",
    )

    assert problems == ["job_collection_failed"]
    # 뒤 Job은 정상 수집된다.
    assert [row[1] for row in sink.written] == ["codex-worker"]


class _RecordingCreate:
    """`create_experiment_log` 호출 인자를 기록하는 더블."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, session, experiment_id, request):
        self.calls.append((session, experiment_id, request))


def test_database_sink_builds_the_log_create_request() -> None:
    """`ExperimentLogCreate` 조립이 틀리면 422로 조용히 실패한다 — 값을 고정한다."""
    import uuid as _uuid

    create = _RecordingCreate()
    experiment_id = _uuid.UUID("6ec09890-a4a8-4c69-9760-c01349351505")
    session = object()

    DatabaseLogSink(session, experiment_id, create=create).write(
        idempotency_key="ar-exec-abc-x7k2:codex-worker:0",
        log_type="codex-worker",
        content="hello",
    )

    assert len(create.calls) == 1
    passed_session, passed_id, request = create.calls[0]
    assert passed_session is session
    assert passed_id == experiment_id
    assert request.idempotency_key == "ar-exec-abc-x7k2:codex-worker:0"
    assert request.log_type == "codex-worker"
    assert request.content == "hello"


class _RecordingBatchV1:
    def __init__(self, names: list[str]) -> None:
        self.calls: list[dict] = []
        self._names = names

    def list_namespaced_job(self, **kwargs):
        self.calls.append(kwargs)
        items = [
            type("Job", (), {"metadata": type("M", (), {"name": name})()})()
            for name in self._names
        ]
        return type("Resp", (), {"items": items})()


def test_active_jobs_adapter_uses_the_executor_label() -> None:
    """launcher와 같은 상수를 써야 두 곳이 갈리지 않는다."""
    from agent_orchestration.launcher.jobs import EXPERIMENT_EXECUTOR_LABEL_SELECTOR

    api = _RecordingBatchV1(["ar-exec-abc"])

    names = KubernetesActiveJobs(api).list_active_job_names("ns")

    assert names == ["ar-exec-abc"]
    assert api.calls == [
        {"namespace": "ns", "label_selector": EXPERIMENT_EXECUTOR_LABEL_SELECTOR}
    ]


def test_run_forever_stops_when_the_shutdown_flag_is_set() -> None:
    """SIGTERM은 진행 중인 tick을 마친 뒤 빠져나가게 한다 — 쓰기 도중에 끊지 않는다."""
    ticks: list[int] = []
    stopping = {"value": False}

    def tick() -> list[str]:
        ticks.append(1)
        if len(ticks) == 3:
            stopping["value"] = True
        return []

    run_forever(tick, should_stop=lambda: stopping["value"], sleep=lambda _s: None,
                interval_sec=5)

    assert len(ticks) == 3


def test_run_forever_survives_a_failing_tick() -> None:
    """tick 하나가 죽어도 루프는 계속 돈다 — 죽으면 재시작 사이 로그가 빈다."""
    ticks: list[int] = []
    stopping = {"value": False}

    def tick() -> list[str]:
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("db down")
        if len(ticks) == 3:
            stopping["value"] = True
        return []

    run_forever(tick, should_stop=lambda: stopping["value"], sleep=lambda _s: None,
                interval_sec=5)

    assert len(ticks) == 3


def test_run_forever_sleeps_the_configured_interval_between_ticks() -> None:
    slept: list[float] = []
    ticks: list[int] = []
    stopping = {"value": False}

    def tick() -> list[str]:
        ticks.append(1)
        if len(ticks) == 2:
            stopping["value"] = True
        return []

    run_forever(tick, should_stop=lambda: stopping["value"],
                sleep=slept.append, interval_sec=7)

    assert slept == [7]


def test_run_forever_does_not_sleep_before_shutting_down() -> None:
    """종료가 정해진 뒤 주기만큼 더 자면 Pod 종료가 그만큼 늦어진다."""
    slept: list[float] = []

    run_forever(lambda: [], should_stop=lambda: True,
                sleep=slept.append, interval_sec=7)

    assert slept == []


def test_collector_settings_require_only_what_the_collector_uses(monkeypatch) -> None:
    """수집기는 Job을 만들지 않는다 — Job 생성 설정을 요구하면 안 된다.

    `LauncherSettings`를 재사용하면 `ORCH_EXECUTOR_IMAGE`까지 필수가 되고, 그것은
    digest 형식 검증을 통과해야 해서 **executor 릴리스마다 수집기 매니페스트를 따라
    고쳐야 한다.** 안 쓰는 값에 배포가 묶인다.
    """
    for name in (
        "ORCH_EXECUTOR_IMAGE",
        "ORCH_EXECUTOR_NODE_POOL",
        "ORCH_EXECUTOR_API_URL",
        "ORCH_GITHUB_APP_SECRET_NAME",
        "ORCH_GITHUB_REPOSITORY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://u:p@db/orchestration")
    monkeypatch.setenv("ORCH_JOB_NAMESPACE", "autoresearch-experiments")

    settings = LogCollectorSettings.from_environment()

    assert settings.database_url == "postgresql://u:p@db/orchestration"
    assert settings.job_namespace == "autoresearch-experiments"
    assert settings.log_collect_interval_sec == 5


def test_collector_settings_read_the_interval_override(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://u:p@db/orchestration")
    monkeypatch.setenv("ORCH_JOB_NAMESPACE", "ns")
    monkeypatch.setenv("ORCH_LOG_COLLECT_INTERVAL_SEC", "10")

    assert LogCollectorSettings.from_environment().log_collect_interval_sec == 10


def test_collector_settings_reject_a_missing_database_url(monkeypatch) -> None:
    """DB 없이 뜨면 매 tick 조용히 실패한다 — 기동 시점에 막는다."""
    from agent_orchestration.launcher.config import LauncherConfigError

    monkeypatch.delenv("ORCH_DATABASE_URL", raising=False)
    monkeypatch.setenv("ORCH_JOB_NAMESPACE", "ns")

    import pytest as _pytest

    with _pytest.raises(LauncherConfigError):
        LogCollectorSettings.from_environment()
