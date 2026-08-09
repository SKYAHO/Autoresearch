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
    KubernetesPodLogs,
    collect_once,
    container_states,
    experiment_id_from_job_name,
    LogSink,
    PodLogReader,
    collect_container_logs,
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

    def __init__(self, *, raises=None) -> None:
        self.written: list[tuple[str, str, str]] = []
        self._raises = raises

    def write(self, *, idempotency_key: str, log_type: str, content: str) -> None:
        if self._raises is not None:
            raise self._raises
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

    def __init__(self) -> None:
        self.list_calls: list[dict] = []
        self.log_calls: list[dict] = []

    def list_namespaced_pod(self, **kwargs):
        self.list_calls.append(kwargs)
        return type("Resp", (), {"items": []})()

    def read_namespaced_pod_log(self, **kwargs) -> str:
        self.log_calls.append(kwargs)
        return "output"


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
        }
    ]


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
