# Action Log Single-Mode Memory Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Status (2026-07-30): Historical implementation record — not an active executable plan.**
> 아래 unchecked box, RED expectation, command, commit message는 당시 작업 과정을
> 보존할 뿐 재실행하면 안 됩니다. 2026-07-30 이후의 현재 계약 정본은
> [Action Log Streaming Review Remediation](../specs/2026-07-30-action-log-review-remediation.md)이며,
> 아래와 충돌하는 historical 설명은 모두 그 정본으로 supersede됩니다.

**Goal:** 단일 action log 실행이 완료된 사용자 객체를 즉시 해제하고 Parquet row group과 JSONL을 순차 기록해, 전체 파티션 크기와 무관한 active-user 메모리 상한을 갖게 합니다.

**Architecture:** 기존 `generate_action_log_batch()`와 `generate_action_log_drafts()`는 shard/checkpoint 및 외부 Python 호출의 legacy 계약으로 그대로 유지합니다. 새 `generate_action_log_single()`은 coordinator 스레드에서 원본 순서로 provider를 호출하고 최대 `4 * max_workers`명의 active user만 유지하며, 한 사용자의 모든 chunk가 끝난 뒤 원본 사용자 순서로 클릭 선정·이벤트 확장·파일 기록을 수행합니다. 중복 `user_id`나 변경 불가능한 exposure metadata는 의미 보존을 위해 legacy batch로 위임합니다. 이 상한과 row-group 계약은 승인된 [2026-07-30 remediation](../specs/2026-07-30-action-log-review-remediation.md)을 따른다.

**Tech Stack:** Python 3.11/3.12, `concurrent.futures`, Pydantic v2, PyArrow `ParquetWriter`/`ParquetFile`, pytest, ruff

## Global Constraints

- `generate_action_log_batch()`의 시그니처와 반환형, `generate_action_log_drafts()`의 shard/checkpoint/progress 동작, shard merge 경로는 변경하지 않습니다.
- 새 bounded 경로는 `run_daily_action_log()` 단일 모드에서만 사용합니다. `run_daily_action_log_shard()`와 `merge_daily_action_log_shards()`는 legacy 경로를 유지합니다.
- candidate provider는 worker가 아니라 coordinator 스레드에서 `virtual_users` 입력 순서대로 한 번씩 호출합니다.
- 한 사용자의 모든 candidate chunk 결과를 모은 뒤에만 `select_clicks_per_slate()`를 실행하고, 완료 사용자는 원본 입력 순서대로 drain합니다.
- click 선정, 한 사용자 내부 이벤트 순서, `seed` 기반 timestamp, KST 날짜가 포함된 전역 event-id 연속 sequence를 legacy 결과와 동일하게 유지합니다.
- quarantine record 순서·JSONL·비율 제한·에러 분류와 공개 CLI 인자/출력 계약을 유지합니다.
- 변경 가능한 exposure metadata는 사용자가 drain될 때 그 사용자 키를 `pop`해 해제합니다. 중복 `user_id` 또는 `collections.abc.MutableMapping`이 아닌 metadata는 legacy batch로 위임합니다.
- 단일 모드 Parquet은 사용자 경계와 무관하게 최대 50,000 rows의 row group으로 최종화하고 warehouse/quarantine JSONL은 열린 spool에 순차 기록합니다. 이 계약은 승인된 [2026-07-30 remediation](../specs/2026-07-30-action-log-review-remediation.md)이 정본입니다.
- 단일 모드 staging 검증은 전체 파일 `pq.read_table()` 대신 `pq.ParquetFile` schema 검사와 row-group 단위 `event_id`/`event_timestamp` 읽기를 사용합니다.
- `autoresearch.jobs.action_log`의 CLI 옵션·dispatch와 Autoresearch-airflow의 KPO memory request는 변경하지 않습니다.
- 런타임 모듈 변경과 같은 커밋에서 `pipeline.py`와 `daily.py` 최상단 docstring을 `[파이프라인]`·`[기능]`·`[비책임]` 형식으로 갱신합니다.
- 새 함수와 변경 함수는 반환형까지 구체적으로 표기하며 `Any`를 새로 도입하지 않습니다.

## Planned File Structure

- Modify: `autoresearch/action_logs/pipeline.py` — 단일 모드 active-user coordinator, 전역 event-id offset, streaming writer, 결과 요약형을 소유합니다.
- Modify: `autoresearch/action_logs/daily.py` — 단일 daily 실행을 새 coordinator에 연결하고 staging Parquet을 row-group 단위로 검증합니다.
- Modify: `tests/test_action_logs_pipeline.py` — legacy 동등성, bounded active-user, metadata 해제/fallback, quarantine, streaming 산출물을 검증합니다.
- Modify: `tests/test_action_logs_daily.py` — daily 연결, row-group staging 검증, 기존 publish/CLI 계약을 검증합니다.
- No change: `autoresearch/jobs/action_log.py`, `autoresearch/action_logs/schema.py`, shard/checkpoint 파일 형식, Autoresearch-airflow 저장소.

## Sequential Dependency Graph

`Task 1 → Task 2 → Task 3 → Task 4 → Task 5`

- Task 1이 event-id offset과 streaming writer 계약을 제공합니다.
- Task 2가 Task 1을 사용해 성공 경로 coordinator를 구현합니다.
- Task 3이 Task 2에 metadata, fallback, quarantine 의미를 완성합니다.
- Task 4는 완성된 Task 3 API만 daily 단일 경로에 연결합니다.
- Task 5는 앞선 네 task가 모두 리뷰·커밋된 뒤 최종 회귀 검증만 수행합니다.
- 같은 두 production 모듈을 연속 수정하므로 task를 병렬 실행하지 않습니다. 각 task의 GREEN 확인과 리뷰가 끝난 뒤 다음 task를 시작합니다.

---

### Task 1: 전역 Event ID Offset과 Streaming Output Writer

**Dependencies:** 없음. 이후 task가 사용할 가장 작은 파일 기록 단위를 먼저 고정합니다.

**Files:**
- Modify: `autoresearch/action_logs/pipeline.py`
- Test: `tests/test_action_logs_pipeline.py`

**Interfaces:**
- Produces:
  - `_expand_events(..., event_id_sequence_start: int = 0) -> list[EventLog]`
  - `_StreamingActionLogWriter(request: EventGenerationRequest, model_name: str)`
  - `_StreamingActionLogWriter.write_events(events: list[EventLog]) -> None`
  - `_StreamingActionLogWriter.write_quarantine(records: list[QuarantineRecord]) -> None`
  - `_StreamingActionLogWriter.finalize_success(generated_at: str) -> None`
  - `_StreamingActionLogWriter.finalize_quarantine_failure() -> None`
  - context-manager `__enter__() -> _StreamingActionLogWriter` / typed `__exit__(...) -> None`
- Preserves:
  - `_expand_events()`의 기존 호출은 default offset `0`으로 동일한 ID를 생성합니다.
  - `write_event_log_parquet()`, `write_event_log_warehouse_jsonl()`, `write_quarantine_jsonl()`은 legacy batch가 계속 사용합니다.

- [ ] **Step 1: event-id 시작 offset을 요구하는 실패 테스트를 작성합니다.**

`tests/test_action_logs_pipeline.py`에 아래 테스트를 추가합니다.

```python
def test_expand_events_respects_event_id_sequence_start() -> None:
    request = EventGenerationRequest(
        click_threshold=1.0,
        seed=7,
        history_end=_FIXED_END,
    )
    drafts = [
        ImpressionDraft(
            user_id="u1",
            video_id="v1",
            click_propensity=0.1,
            watch_fraction=0.5,
            would_like=False,
            duration_sec=100,
        )
    ]

    events = pipeline_module._expand_events(
        drafts,
        set(),
        request,
        event_id_sequence_start=17,
    )

    assert events[0].event_id.endswith("_00000017")
```

- [ ] **Step 2: RED를 확인합니다.**

Run:

```bash
uv run python -m pytest tests/test_action_logs_pipeline.py::test_expand_events_respects_event_id_sequence_start -v
```

Expected: `TypeError`로 `event_id_sequence_start` 인자를 받지 못해 실패합니다.

- [ ] **Step 3: 최소 event-id offset 구현을 추가합니다.**

`_expand_events()`의 keyword-only 인자와 초기 sequence만 변경합니다.

```python
def _expand_events(
    drafts: list[ImpressionDraft],
    clicked: set[int],
    request: EventGenerationRequest,
    *,
    metadata: Mapping[tuple[str, str], ExposureMetadata] | None = None,
    source: str = SOURCE_HISTORICAL,
    event_id_prefix: str = "evt",
    event_id_sequence_start: int = 0,
) -> list[EventLog]:
    # 기존 준비 로직 유지
    events: list[EventLog] = []
    seq = event_id_sequence_start
    # 기존 _emit 및 사용자별 확장 로직 유지
```

- [ ] **Step 4: offset GREEN과 기존 default 회귀를 함께 확인합니다.**

Run:

```bash
uv run python -m pytest tests/test_action_logs_pipeline.py::test_expand_events_respects_event_id_sequence_start tests/test_action_logs_pipeline.py::test_expand_events_event_ids_are_date_namespaced_and_unique tests/test_action_logs_pipeline.py::test_expand_events_without_metadata_is_unchanged -v
```

Expected: 세 테스트가 모두 PASS합니다.

- [ ] **Step 5: completion-time writer와 bounded row-group 실패 테스트를 작성합니다.**

이전 per-user row-group 예시는 **superseded** 되었으며 실행하지 않습니다. 현 계약은
IPC spool에 event/warehouse/quarantine을 append한 뒤,
`finalize_success("2026-07-30T09:00:00+00:00")`에서만 completion `generated_at`을
붙이는 것입니다. `_PARQUET_TARGET_ROW_GROUP_ROWS = 3`으로 고정한 네 event 테스트는
두 `write_events()` 호출과 무관하게 row group 크기 `3`, `1`, 입력 event/JSONL 순서,
그리고 모든 `generated_at`이 completion literal임을 검증해야 합니다.

- [ ] **Step 6: writer RED를 확인합니다.**

Run:

```bash
uv run python -m pytest tests/test_action_logs_pipeline.py::test_streaming_writer_finalizes_completion_time_and_bounded_row_groups -v
```

Expected: IPC spool/finalize API가 없거나 `write_events()` 경계마다 row group을 만들면 실패합니다.

- [ ] **Step 7: transactional IPC spool writer를 구현합니다.**

`_StreamingActionLogWriter(request, model_name)`는 event IPC, warehouse JSONL,
quarantine JSONL을 sibling spool에 열고 `write_events()`에서 최종 Parquet을 만들지
않습니다. quarantine ratio가 허용되면 `finalize_success(generated_at)`가 IPC를 순서대로
읽어 completion column을 붙이고 최대 50,000 rows 단위의 Parquet row group으로
최종화한 뒤 세 산출물을 publish합니다. ratio 초과면
`finalize_quarantine_failure()`가 quarantine만 publish합니다.

- [ ] **Step 8: writer GREEN과 기존 whole-batch writer 회귀를 확인합니다.**

Run:

```bash
uv run python -m pytest tests/test_action_logs_pipeline.py::test_streaming_writer_finalizes_completion_time_and_bounded_row_groups tests/test_action_logs_pipeline.py::test_parquet_matches_events -v
```

Expected: 두 테스트가 PASS합니다.

- [ ] **Step 9: Task 1을 커밋합니다.**

```bash
git add autoresearch/action_logs/pipeline.py tests/test_action_logs_pipeline.py
git commit -m "refactor: action log 스트리밍 writer 기반 추가"
```

---

### Task 2: Bounded Active-User Coordinator와 Legacy 동등성

**Dependencies:** Task 1의 `_StreamingActionLogWriter`와 `_expand_events(event_id_sequence_start=...)`가 GREEN이어야 합니다.

**Files:**
- Modify: `autoresearch/action_logs/pipeline.py`
- Test: `tests/test_action_logs_pipeline.py`

**Interfaces:**
- Produces:
  - `ActionLogSingleResult` frozen dataclass
  - `ActionLogSingleResult.execution_mode: Literal["streaming", "legacy"]`
  - `ActionLogSingleResult.summary -> dict[str, int | float]`
  - `_StreamingRetentionSnapshot` frozen dataclass:
    `phase: Literal["generating", "finalizing"]`, `active_users: int`,
    `buffered_drafts: int`, `buffered_events: int`, `in_flight_work: int`,
    `activated_users: int`, `total_users: int`, `submitted_work: int`,
    `total_work: int | None`, `completed_work: int`, `failed_work: int`,
    `pending_work: int | None`
  - `generate_action_log_single(..., *, candidate_provider: CandidateProvider | None = None, exposure_metadata: MutableMapping[tuple[str, str], ExposureMetadata] | None = None, _retention_observer: Callable[[_StreamingRetentionSnapshot], None] | None = None) -> ActionLogSingleResult`
- Internal state:
  - `_StreamingUserState`는 사용자 sequence, `user_id`, 원본 dict, 해당 사용자의 `_ActionLogWorkItem` 전부, chunk-index별 성공/격리 결과, 남은 chunk 수만 보관합니다.
- Consumes:
  - `_generate_action_log_work()`의 schema retry·에러 분류
  - `select_clicks_per_slate()`, `attach_exposure_tags()`, `_expand_events()`
  - Task 1의 writer

`exposure_metadata`의 annotation은 drain 시 entry를 제거하는 mutable 계약을 나타낸다.
런타임에서 read-only `Mapping` 또는 untyped caller가 전달한 비-`MutableMapping`은
legacy batch로 fallback하며 변경하지 않는다. `_retention_observer`는 private regression
hook이고, 같은 identifier-free snapshot은 operational DAG structured telemetry에도 전달된다.

- [ ] **Step 1: chunk 병렬 완료 순서와 무관한 legacy 동등성 실패 테스트를 작성합니다.**

`generated_at`은 실행 시각이라 비교에서 제외하고, 나머지 Parquet row 순서·ID·timestamp·click·JSONL 및 summary를 비교합니다.

```python
def test_streaming_single_matches_legacy_chunked_output_order_and_seed(tmp_path) -> None:
    users = _fixture_users(6)
    videos = build_fixture_video_records(40)
    legacy_request = _request(
        tmp_path / "legacy",
        chunk_size=4,
        max_concurrency=3,
    )
    streaming_request = _request(
        tmp_path / "streaming",
        chunk_size=4,
        max_concurrency=3,
    )

    legacy = generate_action_log_batch(
        legacy_request,
        users,
        videos,
        RuleBasedActionLogGenerator(),
    )
    streamed = pipeline_module.generate_action_log_single(
        streaming_request,
        users,
        videos,
        RuleBasedActionLogGenerator(),
    )

    columns = [
        name
        for name in pipeline_module.EVENT_LOG_PARQUET_SCHEMA.names
        if name != "generated_at"
    ]
    legacy_rows = pq.read_table(legacy_request.output_path, columns=columns).to_pylist()
    streamed_rows = pq.read_table(
        streaming_request.output_path,
        columns=columns,
    ).to_pylist()
    assert streamed.execution_mode == "streaming"
    assert streamed.summary == legacy.summary
    assert streamed_rows == legacy_rows
    assert Path(streaming_request.warehouse_output_path).read_text(
        encoding="utf-8"
    ) == Path(legacy_request.warehouse_output_path).read_text(encoding="utf-8")
```

- [ ] **Step 2: 모든 chunk를 모은 후 한 번만 click을 고르는 실패 테스트를 작성합니다.**

같은 사용자에게 `chunk_size=2`로 네 영상을 주고 각 chunk에 threshold 이상 후보가 있어도 전체 slate에서 최고 한 건만 click되어야 합니다.

```python
def test_streaming_single_selects_one_click_after_all_user_chunks_finish(
    tmp_path,
) -> None:
    videos = build_fixture_video_records(4)

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        return list(videos)

    result = pipeline_module.generate_action_log_single(
        _request(
            tmp_path,
            candidates_per_user=4,
            chunk_size=2,
            max_concurrency=2,
            click_threshold=0.0,
        ),
        _fixture_users(1),
        videos,
        RuleBasedActionLogGenerator(),
        candidate_provider=provider,
    )

    rows = pq.read_table(result_path := tmp_path / "e.parquet").to_pylist()
    assert result_path.is_file()
    assert sum(row["event_type"] == "click" for row in rows) == 1
```

- [ ] **Step 3: provider thread/order와 active-user 상한 실패 테스트를 작성합니다.**

첫 generator 호출 시점까지 provider가 전체 입력을 선materialize하지 않았음을 함께 고정합니다.

```python
def test_streaming_single_bounds_active_users_and_invokes_provider_on_coordinator(
    tmp_path,
) -> None:
    from threading import get_ident

    coordinator_thread = get_ident()
    provider_threads: list[int] = []
    provider_order: list[str] = []
    provider_calls_seen_by_generator: list[int] = []
    videos = build_fixture_video_records(2)

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        provider_threads.append(get_ident())
        provider_order.append(virtual_user["user_id"])
        return [videos[0]]

    class _RecordingGenerator(RuleBasedActionLogGenerator):
        def generate(self, virtual_user: dict, candidates: list[dict]) -> str:
            provider_calls_seen_by_generator.append(len(provider_order))
            return super().generate(virtual_user, candidates)

    users = _fixture_users(7)
    pipeline_module.generate_action_log_single(
        _request(
            tmp_path,
            candidates_per_user=1,
            max_concurrency=2,
        ),
        users,
        videos,
        _RecordingGenerator(),
        candidate_provider=provider,
    )

    assert provider_order == [user["user_id"] for user in users]
    assert set(provider_threads) == {coordinator_thread}
    assert min(provider_calls_seen_by_generator) <= 2
```

- [ ] **Step 4: retained draft/event payload의 구조적 상한 실패 테스트를 작성합니다.**

private observer는 active state에 실제로 저장된 draft 수와 writer에 넘기기 직전의
사용자-local event 수를 보고합니다. 전체 사용자 수를 크게 늘려도 draft 상한은
`4 * max_workers * candidates_per_user`, event 상한은 50,000 rows를 넘지 않아야
합니다. 이는 승인된 [2026-07-30 remediation](../specs/2026-07-30-action-log-review-remediation.md)의
최종 계약입니다.

```python
def test_streaming_single_retained_payload_is_bounded_by_active_users(
    tmp_path,
) -> None:
    users = _fixture_users(40)
    candidates_per_user = 7
    max_concurrency = 3
    snapshots: list[pipeline_module._StreamingRetentionSnapshot] = []

    pipeline_module.generate_action_log_single(
        _request(
            tmp_path,
            candidates_per_user=candidates_per_user,
            chunk_size=2,
            max_concurrency=max_concurrency,
            click_threshold=0.0,
        ),
        users,
        build_fixture_video_records(20),
        RuleBasedActionLogGenerator(),
        _retention_observer=snapshots.append,
    )

    assert snapshots
    assert max(item.active_users for item in snapshots) <= 4 * max_concurrency
    assert max(item.buffered_drafts for item in snapshots) <= (
        4 * max_concurrency * candidates_per_user
    )
    assert max(item.buffered_events for item in snapshots) <= 50_000
    assert any(item.buffered_drafts == 0 for item in snapshots)
```

observer는 테스트/진단용 private keyword-only seam이며 snapshot 자체만 전달합니다.
draft/event 객체나 active state를 외부에 넘겨 수명을 늘리지 않습니다. production
동작에서 observer가 `None`이면 추가 목록이나 이력은 만들지 않습니다.

- [ ] **Step 5: coordinator RED를 확인합니다.**

Run:

```bash
uv run python -m pytest tests/test_action_logs_pipeline.py::test_streaming_single_matches_legacy_chunked_output_order_and_seed tests/test_action_logs_pipeline.py::test_streaming_single_selects_one_click_after_all_user_chunks_finish tests/test_action_logs_pipeline.py::test_streaming_single_bounds_active_users_and_invokes_provider_on_coordinator tests/test_action_logs_pipeline.py::test_streaming_single_retained_payload_is_bounded_by_active_users -v
```

Expected: `generate_action_log_single`이 없어 `AttributeError` 또는 import 실패로 RED입니다.

- [ ] **Step 6: 반환 summary 계약을 구현합니다.**

```python
@dataclass(frozen=True)
class ActionLogSingleResult:
    execution_mode: Literal["streaming", "legacy"]
    total_events: int
    impressions: int
    clicks: int
    quarantined_users: int
    api_error: int
    invalid_json: int
    schema_fail: int

    @property
    def summary(self) -> dict[str, int | float]:
        return {
            "total_events": self.total_events,
            "impressions": self.impressions,
            "clicks": self.clicks,
            "ctr": round(self.clicks / self.impressions, 4)
            if self.impressions
            else 0.0,
            "quarantined_users": self.quarantined_users,
            "api_error": self.api_error,
            "invalid_json": self.invalid_json,
            "schema_fail": self.schema_fail,
        }
```

- [ ] **Step 7: active-user state와 성공 경로 coordinator를 구현합니다.**

`collections.deque`를 사용하고 아래 알고리즘을 정확히 구현합니다.

```python
@dataclass
class _StreamingUserState:
    user_sequence: int
    user_id: str
    virtual_user: dict
    work: list[_ActionLogWorkItem]
    drafts_by_chunk: dict[int, list[ImpressionDraft]]
    quarantine_by_chunk: dict[int, QuarantineRecord]
    remaining_chunks: int
```

`generate_action_log_single()`의 성공 경로 순서는 다음과 같습니다.

1. `max_workers = max(1, request.max_concurrency)`, `max_active_users = 4 * max_workers`와 `ThreadPoolExecutor(max_workers=max_workers)`를 만듭니다. 이 최종 상한은 승인된 [2026-07-30 remediation](../specs/2026-07-30-action-log-review-remediation.md)을 따른다.
2. coordinator가 다음 `virtual_user`를 꺼내 `random.Random(f"{request.seed}:{user_id}")`를 만들고, `candidate_provider` 또는 기존 `build_candidates()`를 호출합니다.
3. 그 사용자 candidates를 기존 `_chunked()`로 전부 나누고, global `work_sequence` 순서로 `work_{work_sequence:08d}`를 부여합니다.
4. active state의 chunk는 deterministic unsent-work deque에 넣고, Future 수가 `max_workers`보다 작은 동안에만 submit합니다. active state 수가 `max_active_users`가 될 때까지만 다음 provider를 호출하며, 완료 Future마다 replacement를 먼저 submit합니다.
5. future 완료 결과는 `(state, chunk_index, work_sequence)`로 찾아 state의 `drafts_by_chunk` 또는 `quarantine_by_chunk`에 저장합니다. 모든 완료 work의 queue/request/parse/total timing은 bounded aggregate interval에 수집합니다. worker detailed context는 provider 소진 뒤 정확한 total work가 확인되고 small-run threshold 이하일 때만 enable하며, detailed event도 그 확인 뒤 finish에서만 emit합니다.
6. deque 맨 앞 state의 `remaining_chunks == 0`일 때만 drain합니다. chunk index 오름차순으로 drafts와 quarantine을 조립하고, 모든 drafts에 대해 `select_clicks_per_slate()`를 한 번 호출합니다.
7. `_expand_events(..., event_id_sequence_start=next_event_sequence)`로 이벤트를 만들고 writer에 기록한 뒤 `next_event_sequence += len(events)`로 갱신합니다.
8. summary counter만 누적하고 state의 work/candidates/drafts/events 참조를 deque에서 제거합니다. 그 뒤에만 다음 사용자를 활성화합니다.
9. 입력이 끝나고 active deque와 futures가 모두 빌 때까지 4~8을 반복합니다.
10. generation/drain 뒤에도 writer context를 열린 채로 quarantine ratio guard를 실행합니다. guard가 실패하면 `finalize_quarantine_failure()`로 quarantine만 commit한 뒤 원래 `ActionLogGenerationError`를 다시 발생시킵니다.
11. guard가 성공하면 completion `generated_at`을 한 번 계산하고 `finalize_success(generated_at, ...)`로 IPC spool을 최종 Parquet/JSONL로 commit합니다.
12. active state 변경, work 결과 저장, 사용자-local event 생성 직전/기록 직후와 finalization 중에 observer가 있으면 현재 ownership 수량 snapshot을 보냅니다. private hook과 DAG structured telemetry는 같은 snapshot을 사용하고, 기록 직후 snapshot의 `buffered_events`는 0이어야 합니다.

이 구조의 retained working set은 `virtual_users` 입력 목록 자체를 제외하면 `max_active_users × 한 사용자 candidates/chunks/results`와 executor worker 수에 의해 제한됩니다. 결과 `EventLogBatch.events`나 전체 `drafts_by_index`는 만들지 않습니다. 단일 경로는 progress/checkpoint callback을 받지 않으며, 그 계약은 기존 `generate_action_log_drafts()`에 남깁니다.

- [ ] **Step 8: Task 2 GREEN과 legacy API 회귀를 확인합니다.**

Run:

```bash
uv run python -m pytest tests/test_action_logs_pipeline.py::test_streaming_single_matches_legacy_chunked_output_order_and_seed tests/test_action_logs_pipeline.py::test_streaming_single_selects_one_click_after_all_user_chunks_finish tests/test_action_logs_pipeline.py::test_streaming_single_bounds_active_users_and_invokes_provider_on_coordinator tests/test_action_logs_pipeline.py::test_streaming_single_retained_payload_is_bounded_by_active_users tests/test_action_logs_pipeline.py::test_chunked_parallel_matches_single_call tests/test_action_logs_pipeline.py::test_schema_retry_final_failure_is_quarantined -v
```

Expected: 새 네 테스트와 기존 두 legacy 테스트가 모두 PASS합니다.

- [ ] **Step 9: Task 2를 커밋합니다.**

```bash
git add autoresearch/action_logs/pipeline.py tests/test_action_logs_pipeline.py
git commit -m "feat: 단일 action log active-user 스트리밍 추가"
```

---

### Task 3: Exposure Metadata 해제, Legacy Fallback, Quarantine 의미 보존

**Dependencies:** Task 2의 성공 경로 coordinator와 `ActionLogSingleResult`가 GREEN이어야 합니다.

**Files:**
- Modify: `autoresearch/action_logs/pipeline.py`
- Test: `tests/test_action_logs_pipeline.py`

**Interfaces:**
- Produces:
  - `_has_duplicate_user_ids(virtual_users: list[dict]) -> bool`
  - `_consume_user_exposure_metadata(metadata: MutableMapping[tuple[str, str], ExposureMetadata], user_id: str) -> dict[tuple[str, str], ExposureMetadata]`
  - `_single_result_from_legacy(result: EventGenerationResult) -> ActionLogSingleResult`
  - `_raise_if_quarantine_count_exceeds(quarantine_count: int, total_work: int, request: EventGenerationRequest, user_count: int) -> None`
- Extends:
  - `generate_action_log_single()`이 streaming/legacy 선택과 quarantine counter를 완성합니다.
- Preserves:
  - 기존 `_raise_if_quarantine_exceeds()`는 list 기반 legacy 호출을 유지하고 내부에서 count helper를 재사용할 수 있습니다.

- [ ] **Step 1: mutable metadata가 사용자 drain마다 해제되는 실패 테스트를 작성합니다.**

provider가 사용자별 두 키를 추가하고 다음 provider 호출 직전 크기를 기록합니다. active-user limit가 2이면 이미 drain된 사용자 키가 남아서는 안 되며 종료 시 map은 비어야 합니다.

```python
def test_streaming_single_consumes_mutable_exposure_metadata_per_drained_user(
    tmp_path,
) -> None:
    metadata: dict[tuple[str, str], ExposureMetadata] = {}
    sizes_before_provider: list[int] = []
    videos = build_fixture_video_records(2)

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        sizes_before_provider.append(len(metadata))
        user_id = virtual_user["user_id"]
        for rank, video in enumerate(videos, start=1):
            metadata[(user_id, str(video["video_id"]))] = ExposureMetadata(
                policy="model",
                rank=rank,
                ctr_score=0.5,
                is_exploration=False,
                policy_version="run-a",
                exposure_source="model",
            )
        return list(videos)

    request = _request(
        tmp_path,
        candidates_per_user=2,
        max_concurrency=2,
    )
    result = pipeline_module.generate_action_log_single(
        request,
        _fixture_users(6),
        videos,
        RuleBasedActionLogGenerator(),
        candidate_provider=provider,
        exposure_metadata=metadata,
    )

    assert result.execution_mode == "streaming"
    assert max(sizes_before_provider) <= 2
    assert metadata == {}
    rows = pq.read_table(request.output_path, columns=["exposure_source"]).to_pylist()
    assert {row["exposure_source"] for row in rows} == {"model"}
```

`sizes_before_provider <= 2`는 앞선 active 사용자 중 drain되지 않은 최대 한 명의 두 metadata key만 남을 수 있다는 뜻입니다.

- [ ] **Step 2: duplicate ID와 read-only metadata의 legacy fallback 실패 테스트를 작성합니다.**

테스트 import에 `MappingProxyType`을 추가합니다.

```python
def test_single_falls_back_to_legacy_for_duplicate_user_id(tmp_path) -> None:
    users = _fixture_users(2)
    users[1]["user_id"] = users[0]["user_id"]
    result = pipeline_module.generate_action_log_single(
        _request(tmp_path),
        users,
        build_fixture_video_records(10),
        RuleBasedActionLogGenerator(),
    )

    assert result.execution_mode == "legacy"


def test_single_falls_back_to_legacy_for_read_only_exposure_metadata(
    tmp_path,
) -> None:
    backing: dict[tuple[str, str], ExposureMetadata] = {}
    metadata = MappingProxyType(backing)
    videos = build_fixture_video_records(2)

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        user_id = virtual_user["user_id"]
        backing[(user_id, str(videos[0]["video_id"]))] = ExposureMetadata(
            policy="model",
            rank=1,
            ctr_score=0.5,
            is_exploration=False,
            policy_version="run-a",
            exposure_source="model",
        )
        return [videos[0]]

    request = _request(tmp_path, candidates_per_user=1)
    result = pipeline_module.generate_action_log_single(
        request,
        _fixture_users(2),
        videos,
        RuleBasedActionLogGenerator(),
        candidate_provider=provider,
        exposure_metadata=metadata,
    )

    assert result.execution_mode == "legacy"
    assert set(
        pq.read_table(
            request.output_path,
            columns=["exposure_source"],
        ).column("exposure_source").to_pylist()
    ) == {"model"}
```

- [ ] **Step 3: quarantine 순서·에러 수와 임계 초과 출력을 요구하는 실패 테스트를 작성합니다.**

한 generator가 원본 사용자 순서 중 `vu_0001`, `vu_0003`을 invalid JSON으로 만들고, 낮은 제한에서는 동일한 quarantine JSONL을 남긴 뒤 `ActionLogGenerationError`를 발생시켜야 합니다.

```python
def test_streaming_single_preserves_quarantine_order_counts_and_file(
    tmp_path,
) -> None:
    class _TwoBadUsers(RuleBasedActionLogGenerator):
        def generate(self, virtual_user: dict, videos: list[dict]) -> str:
            if virtual_user["user_id"] in {"vu_0001", "vu_0003"}:
                return "{broken"
            return super().generate(virtual_user, videos)

    request = _request(
        tmp_path,
        candidates_per_user=2,
        max_concurrency=3,
        max_quarantine_ratio=0.5,
    )
    result = pipeline_module.generate_action_log_single(
        request,
        _fixture_users(5),
        build_fixture_video_records(4),
        _TwoBadUsers(),
    )

    quarantined = [
        json.loads(line)
        for line in Path(request.quarantine_output_path)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert result.summary["quarantined_users"] == 2
    assert result.summary["invalid_json"] == 2
    assert [row["user_id"] for row in quarantined] == ["vu_0001", "vu_0003"]


def test_streaming_single_raises_after_writing_quarantine_when_ratio_exceeded(
    tmp_path,
) -> None:
    class _AllBad(RuleBasedActionLogGenerator):
        def generate(self, virtual_user: dict, videos: list[dict]) -> str:
            return "{broken"

    request = _request(
        tmp_path,
        candidates_per_user=2,
        max_quarantine_ratio=0.25,
    )
    with pytest.raises(
        ActionLogGenerationError,
        match="quarantine ratio 1.00 exceeds",
    ):
        pipeline_module.generate_action_log_single(
            request,
            _fixture_users(3),
            build_fixture_video_records(4),
            _AllBad(),
        )

    assert len(
        Path(request.quarantine_output_path)
        .read_text(encoding="utf-8")
        .splitlines()
    ) == 3
```

- [ ] **Step 4: Task 3 RED를 확인합니다.**

Run:

```bash
uv run python -m pytest tests/test_action_logs_pipeline.py::test_streaming_single_consumes_mutable_exposure_metadata_per_drained_user tests/test_action_logs_pipeline.py::test_single_falls_back_to_legacy_for_duplicate_user_id tests/test_action_logs_pipeline.py::test_single_falls_back_to_legacy_for_read_only_exposure_metadata tests/test_action_logs_pipeline.py::test_streaming_single_preserves_quarantine_order_counts_and_file tests/test_action_logs_pipeline.py::test_streaming_single_raises_after_writing_quarantine_when_ratio_exceeded -v
```

Expected: metadata가 해제되지 않거나 fallback mode/quarantine count helper가 없어 최소 한 테스트가 FAIL합니다.

- [ ] **Step 5: streaming eligibility와 legacy adapter를 구현합니다.**

```python
def _has_duplicate_user_ids(virtual_users: list[dict]) -> bool:
    user_ids = [str(user.get("user_id", "")) for user in virtual_users]
    return len(user_ids) != len(set(user_ids))


def _single_result_from_legacy(
    result: EventGenerationResult,
) -> ActionLogSingleResult:
    summary = result.summary
    return ActionLogSingleResult(
        execution_mode="legacy",
        total_events=int(summary["total_events"]),
        impressions=int(summary["impressions"]),
        clicks=int(summary["clicks"]),
        quarantined_users=int(summary["quarantined_users"]),
        api_error=int(summary["api_error"]),
        invalid_json=int(summary["invalid_json"]),
        schema_fail=int(summary["schema_fail"]),
    )
```

`generate_action_log_single()` 시작 시 아래 둘 중 하나면 즉시 기존 함수에 위임하고 adapter 결과를 반환합니다.

```python
if _has_duplicate_user_ids(virtual_users) or (
    exposure_metadata is not None
    and not isinstance(exposure_metadata, MutableMapping)
):
    legacy = generate_action_log_batch(
        request,
        virtual_users,
        videos,
        generator,
        candidate_provider=candidate_provider,
        exposure_metadata=exposure_metadata,
    )
    return _single_result_from_legacy(legacy)
```

이 검사는 provider 실행 전에 수행합니다. 중복 ID는 legacy의 user-id grouping/click 의미를 보존하고, read-only mapping은 provider가 채운 전체 map을 마지막에 읽는 기존 의미를 보존합니다.

- [ ] **Step 6: 사용자 metadata를 drain 시점에 소비합니다.**

```python
def _consume_user_exposure_metadata(
    metadata: MutableMapping[tuple[str, str], ExposureMetadata],
    user_id: str,
) -> dict[tuple[str, str], ExposureMetadata]:
    keys = [key for key in metadata if key[0] == user_id]
    return {key: metadata.pop(key) for key in keys}
```

state drain에서 drafts를 조립한 직후 이 helper를 호출하고 반환 map으로 `attach_exposure_tags()`를 수행합니다. 성공 draft가 없는 사용자도 helper를 호출해 실패 chunk의 provider metadata를 해제합니다. 입력 ID가 unique이므로 이후 사용자의 metadata를 제거하지 않습니다.

- [ ] **Step 7: list를 보관하지 않는 quarantine guard와 counter를 완성합니다.**

```python
def _raise_if_quarantine_count_exceeds(
    quarantine_count: int,
    total_work: int,
    request: EventGenerationRequest,
    user_count: int,
) -> None:
    if not total_work:
        return
    quarantine_ratio = quarantine_count / total_work
    if quarantine_ratio <= request.max_quarantine_ratio:
        return
    raise ActionLogGenerationError(
        f"quarantine ratio {quarantine_ratio:.2f} exceeds max_quarantine_ratio "
        f"{request.max_quarantine_ratio:.2f} "
        f"(quarantined={quarantine_count}, total_chunks={total_work}, "
        f"users={user_count})"
    )
```

streaming drain은 chunk-index 순으로 quarantine record를 writer spool에 즉시 쓰고 `quarantined_users`, `api_error`, `invalid_json`, `schema_fail` counter만 올립니다. generation/drain 뒤 writer context가 아직 열린 상태에서 count helper를 호출합니다. 임계 초과면 `finalize_quarantine_failure()`가 quarantine만 최종 경로로 transactional commit하고 Parquet/warehouse spool은 제거한 뒤 원래 오류를 다시 발생시킵니다. 허용 범위면 completion `generated_at`으로 `finalize_success()`가 Parquet, warehouse JSONL, quarantine JSONL을 함께 transactional commit합니다. 기존 `_raise_if_quarantine_exceeds()`는 먼저 `write_quarantine_jsonl()`을 호출하는 legacy 동작을 유지한 채 count helper를 재사용합니다.

- [ ] **Step 8: Task 3 GREEN과 legacy quarantine 회귀를 확인합니다.**

Run:

```bash
uv run python -m pytest tests/test_action_logs_pipeline.py::test_streaming_single_consumes_mutable_exposure_metadata_per_drained_user tests/test_action_logs_pipeline.py::test_single_falls_back_to_legacy_for_duplicate_user_id tests/test_action_logs_pipeline.py::test_single_falls_back_to_legacy_for_read_only_exposure_metadata tests/test_action_logs_pipeline.py::test_streaming_single_preserves_quarantine_order_counts_and_file tests/test_action_logs_pipeline.py::test_streaming_single_raises_after_writing_quarantine_when_ratio_exceeded tests/test_action_logs_pipeline.py::test_user_isolation_quarantines_bad_row tests/test_action_logs_pipeline.py::test_total_failure_raises_and_writes_quarantine tests/test_action_logs_pipeline.py::test_batch_attaches_provider_exposure_tags -v
```

Expected: 새 다섯 테스트와 기존 세 legacy 테스트가 모두 PASS합니다.

- [ ] **Step 9: Task 3을 커밋합니다.**

```bash
git add autoresearch/action_logs/pipeline.py tests/test_action_logs_pipeline.py
git commit -m "fix: 단일 action log 완료 사용자 객체 해제"
```

---

### Task 4: Daily Single 연결과 Row-Group Staging 검증

**Dependencies:** Task 3의 `generate_action_log_single()`이 success, fallback, quarantine 경로에서 모두 GREEN이어야 합니다.

**Files:**
- Modify: `autoresearch/action_logs/daily.py`
- Modify: `autoresearch/action_logs/pipeline.py` (module responsibility docstring)
- Test: `tests/test_action_logs_daily.py`

**Interfaces:**
- Consumes:
  - `generate_action_log_single(...) -> ActionLogSingleResult`
- Produces:
  - `_validate_staged_event_parquet(path: str | Path, partition_date: date) -> None`
- Preserves:
  - `run_daily_action_log(...) -> dict[str, object]`
  - 기존 skip-before-generator, overwrite, last-known-good publish, quarantine warning, CLI dispatch
  - shard/checkpoint/merge 함수 본문

- [ ] **Step 1: daily 단일 실행이 최대 50,000-row group으로 최종화하는 테스트를 작성합니다.**

기존 `test_run_daily_action_log_writes_dt_partition`은 유지하고 별도 구조 테스트를 추가합니다.

```python
def test_run_daily_action_log_coalesces_target_row_groups(
    tmp_path,
) -> None:
    partition_date = date(2026, 7, 1)
    virtual_users_path = tmp_path / "virtual_users.parquet"
    youtube_base = tmp_path / "youtube"
    output_base = tmp_path / "action_log"
    _write_virtual_users(virtual_users_path, count=4)
    _write_youtube_partition(youtube_base, partition_date)

    summary = run_daily_action_log(
        partition_date=partition_date,
        youtube_base_path=str(youtube_base),
        virtual_users_path=str(virtual_users_path),
        output_base_path=str(output_base),
        candidates_per_user=3,
        click_threshold=0.2,
        max_concurrency=2,
        chunk_size=1,
        generator_name="rule_based",
    )

    parquet = pq.ParquetFile(
        output_base / "dt=2026-07-01" / "part-0.parquet"
    )
    assert summary["users"] == 4
    assert parquet.num_row_groups == 1
```

- [ ] **Step 2: whole-file read 없이 row-group 검증하는 실패 테스트를 작성합니다.**

`_StreamingActionLogWriter`로 최종화된 bounded-row-group Parquet을 만든 후
`pq.read_table`을 금지해 새 helper가 `ParquetFile.read_row_group()`만 쓰는지 확인합니다.

```python
def test_validate_staged_event_parquet_reads_row_groups_without_read_table(
    tmp_path,
    monkeypatch,
) -> None:
    partition_date = date(2026, 7, 1)
    request = daily_module._build_request(
        partition_date=partition_date,
        tmp_dir=tmp_path,
        candidates_per_user=1,
        click_threshold=0.2,
        personalized_ratio=0.7,
        popular_ratio=0.2,
        exploration_ratio=0.1,
        seed=1,
        max_concurrency=1,
        chunk_size=0,
        max_quarantine_ratio=0.5,
        history_end=None,
    )
    events = [
        EventLog(
            event_id=f"evt_20260701_{index:08d}",
            event_timestamp=datetime(2026, 7, 1, 3 + index, tzinfo=UTC),
            user_id=f"u{index}",
            event_type="impression",
            video_id=f"v{index}",
            source="historical",
        )
        for index in range(2)
    ]
    with pipeline_module._StreamingActionLogWriter(
        request=request,
        model_name="test-model",
    ) as writer:
        writer.write_events([events[0]])
        writer.write_events([events[1]])
        writer.finalize_success("2026-07-30T09:00:00+00:00")

    monkeypatch.setattr(
        daily_module.pq,
        "read_table",
        lambda *args, **kwargs: pytest.fail("whole-file read is forbidden"),
    )
    daily_module._validate_staged_event_parquet(
        request.output_path,
        partition_date,
    )
```

테스트 import에 `EventLog`와 `autoresearch.action_logs.pipeline as pipeline_module`을 추가합니다.

- [ ] **Step 3: row-group validator가 schema와 날짜 위반을 거부하는 실패 테스트를 작성합니다.**

기존 `test_run_daily_action_log_rejects_timestamp_outside_partition_date`는 end-to-end 날짜 위반을 계속 검증합니다. 새 helper에는 unrelated schema를 직접 전달합니다.

```python
def test_validate_staged_event_parquet_rejects_unrelated_schema(tmp_path) -> None:
    path = tmp_path / "bad.parquet"
    pq.write_table(pa.table({"unexpected": [1]}), path)

    with pytest.raises(ValueError, match="schema does not match"):
        daily_module._validate_staged_event_parquet(
            path,
            date(2026, 7, 1),
        )
```

- [ ] **Step 4: Task 4 RED를 확인합니다.**

Run:

```bash
uv run python -m pytest tests/test_action_logs_daily.py::test_run_daily_action_log_coalesces_small_output_into_one_target_row_group tests/test_action_logs_daily.py::test_validate_staged_event_parquet_reads_row_groups_without_read_table tests/test_action_logs_daily.py::test_validate_staged_event_parquet_rejects_unrelated_schema -v
```

Expected: historical RED가 해결된 뒤에는 세 테스트가 PASS하며, daily는 최종화된
bounded row group만 staging validator에 전달합니다.

- [ ] **Step 5: row-group staging validator를 구현합니다.**

`daily.py`에 다음 구현을 추가합니다.

```python
def _validate_staged_event_parquet(
    path: str | Path,
    partition_date: date,
) -> None:
    try:
        parquet = pq.ParquetFile(path)
    except Exception as exc:  # noqa: BLE001 - pyarrow errors vary by file failure
        raise ValueError("staged final parquet is unreadable") from exc
    if not parquet.schema_arrow.equals(EVENT_LOG_PARQUET_SCHEMA):
        raise ValueError(
            "staged final parquet schema does not match action log contract"
        )
    for row_group_index in range(parquet.num_row_groups):
        table = parquet.read_row_group(
            row_group_index,
            columns=["event_id", "event_timestamp"],
        )
        for row in table.to_pylist():
            timestamp = row["event_timestamp"]
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            event_date = timestamp.astimezone(_KST).date()
            if event_date != partition_date:
                raise ValueError(
                    "event_timestamp outside partition_date "
                    f"(event_id={row['event_id']}, event_date={event_date}, "
                    f"partition_date={partition_date})"
                )
```

schema 확인 후 필요한 두 column만 row group별로 읽습니다. 최종 streaming 경로에서는 row group이 최대 50,000 rows이므로 검증 메모리도 이 상한으로 제한됩니다. 이는 승인된 [2026-07-30 remediation](../specs/2026-07-30-action-log-review-remediation.md)의 계약입니다.

- [ ] **Step 6: `run_daily_action_log()`만 새 single API에 연결합니다.**

`daily.py` import에서 `generate_action_log_batch`를 `generate_action_log_single`로 바꾸고 단일 실행 블록을 아래처럼 수정합니다.

```python
result = generate_action_log_single(
    request,
    virtual_users,
    videos,
    generator,
    candidate_provider=candidate_provider,
    exposure_metadata=exposure_metadata,
)
```

기존 `_validate_event_partition_dates(result.batch.events, partition_date)`와 `pq.read_table(request.output_path)` 두 줄을 제거하고 아래 한 줄로 교체합니다.

```python
_validate_staged_event_parquet(request.output_path, partition_date)
```

return dict의 `**result.summary`와 generator close, `ActionLogGenerationError` 시 quarantine publish, staging publish 순서는 유지합니다. `run_daily_action_log_shard()`와 `merge_daily_action_log_shards()`의 기존 `generate_action_log_drafts()`/`expand_action_log_drafts()`/`pq.read_table()` 코드는 건드리지 않습니다.

- [ ] **Step 7: 두 runtime module docstring을 같은 task에서 갱신합니다.**

`pipeline.py` 최상단은 다음 책임을 포함합니다.

```python
"""Virtual user 후보를 LLM 판정에서 action log 산출물로 변환한다.

[파이프라인] 노출 후보 조립 다음, action log 파티션 publish 이전 구간에서
LLM 판정·클릭 선정·이벤트 확장·로컬 산출물 기록을 담당한다.

[기능] shard/checkpoint가 사용하는 legacy draft/batch 계약과 단일 모드의
bounded active-user 스트리밍, Parquet row-group 및 JSONL 기록을 제공한다.

[비책임] 일일 partition 검증·publish는 autoresearch/action_logs/daily.py,
공개 CLI dispatch는 autoresearch/jobs/action_log.py, KPO resource 설정은
SKYAHO/Autoresearch-airflow가 소유한다.
"""
```

위 block은 현재 `pipeline.py` module docstring의 정확한 사본이다. streaming의
telemetry 책임은 현재 `generate_action_log_single()` docstring과 구현이 명시한다:
`_retention_observer`는 private regression hook이고, observer에 전달하는 것과 같은
identifier-free retention snapshot이 DAG structured telemetry에도 전달된다. 이 설명은
bounded active-user, completion-time finalization, shard/checkpoint 비책임을 바꾸지 않는다.

`daily.py` 최상단은 다음 책임을 포함합니다.

```python
"""Daily action log partition 실행과 산출물 publish를 조정한다.

[파이프라인] 공개 action log batch CLI와 LLM 판정 pipeline 사이에서 일일 입력
partition을 읽고 single 또는 shard 실행을 선택한 뒤 final partition을 publish한다.

[기능] 단일 coordinator가 daily temporary directory에 completion-time Parquet/JSONL을
최종 commit한 뒤 row-group staging 검증과 last-known-good publish를 수행하며, 기존
shard/checkpoint/merge 실행도 제공한다.

[비책임] LLM 판정·클릭·이벤트 의미는 autoresearch/action_logs/pipeline.py,
CLI 인자 계약은 autoresearch/jobs/action_log.py, Airflow KPO resource는
SKYAHO/Autoresearch-airflow가 소유한다.
"""
```

- [ ] **Step 8: Task 4 GREEN과 single/shard/publish/CLI 회귀를 확인합니다.**

Run:

```bash
uv run python -m pytest tests/test_action_logs_daily.py::test_run_daily_action_log_coalesces_small_output_into_one_target_row_group tests/test_action_logs_daily.py::test_validate_staged_event_parquet_reads_row_groups_without_read_table tests/test_action_logs_daily.py::test_validate_staged_event_parquet_rejects_unrelated_schema tests/test_action_logs_daily.py::test_run_daily_action_log_writes_dt_partition tests/test_action_logs_daily.py::test_run_daily_action_log_rejects_timestamp_outside_partition_date tests/test_action_logs_daily.py::test_single_quarantine_publish_failure_warns_and_keeps_final_success tests/test_action_logs_daily.py::test_single_failed_overwrite_preserves_previous_final tests/test_action_logs_daily.py::test_shard_merge_matches_single_run_event_contract tests/test_action_logs_daily.py::test_daily_single_joins_exposure_tags_into_final_parquet tests/test_action_logs_daily.py::test_daily_shard_then_merge_carries_exposure_tags tests/test_action_logs_daily.py::test_cli_parses_click_threshold tests/test_action_logs_daily.py::test_cli_requires_click_threshold -v
```

Expected: 새 세 테스트와 기존 single/shard/publish/CLI 테스트가 모두 PASS합니다.

- [ ] **Step 9: Task 4를 커밋합니다.**

```bash
git add autoresearch/action_logs/pipeline.py autoresearch/action_logs/daily.py tests/test_action_logs_daily.py
git commit -m "fix: daily action log를 사용자 단위로 flush"
```

---

### Task 5: 전체 회귀와 변경 범위 검증

**Dependencies:** Task 1~4가 순서대로 GREEN이고 각 task 리뷰가 완료되어야 합니다.

**Files:**
- Verify only: `autoresearch/action_logs/pipeline.py`
- Verify only: `autoresearch/action_logs/daily.py`
- Verify only: `tests/test_action_logs_pipeline.py`
- Verify only: `tests/test_action_logs_daily.py`

**Interfaces:** 새 인터페이스를 추가하지 않습니다. 이 task는 구현·테스트·문서 범위가 승인 설계와 일치하는지 증명합니다.

- [ ] **Step 1: user가 지정한 pipeline suite를 실행합니다.**

Run:

```bash
uv run python -m pytest tests/test_action_logs_pipeline.py -v
```

Expected: 모든 pipeline 테스트가 PASS합니다.

- [ ] **Step 2: user가 지정한 related daily suite를 실행합니다.**

Run:

```bash
uv run python -m pytest tests/test_action_logs_daily.py -v
```

Expected: single, shard, checkpoint, merge, publish, CLI 테스트가 모두 PASS합니다.

- [ ] **Step 3: user가 지정한 lint를 실행합니다.**

Run:

```bash
uv run --no-sync ruff check autoresearch tests tools
```

Expected: 위반 없이 종료 코드 0입니다.

- [ ] **Step 4: user가 지정한 whitespace 검증을 실행합니다.**

Run:

```bash
git diff --check
```

Expected: 출력 없이 종료 코드 0입니다.

- [ ] **Step 5: 실행 시간과 환경이 허용하면 user가 지정한 full suite를 실행합니다.**

Run:

```bash
uv run python -m pytest -v
```

Expected: 전체 suite가 PASS합니다. 환경 제약으로 실행하지 못하면 PR 검증 기록에 실행하지 못한 구체적 이유와 Step 1~4 결과를 남깁니다.

- [ ] **Step 6: 범위와 메모리 보존 경계를 diff로 확인합니다.**

Run:

```bash
git diff --stat origin/main...HEAD
git diff origin/main...HEAD -- autoresearch/action_logs/pipeline.py autoresearch/action_logs/daily.py tests/test_action_logs_pipeline.py tests/test_action_logs_daily.py
```

확인 기준:

- production 변경은 `pipeline.py`, `daily.py`뿐입니다.
- `generate_action_log_batch()`와 shard/checkpoint/merge API가 삭제·변경되지 않았습니다.
- 단일 streaming coordinator에는 파티션 전체 `work`, `drafts`, `events`, quarantine list가 없습니다.
- active deque는 `4 * max_workers` 사용자 이하이고 submitted Future는 `max_workers` 이하이며, provider 호출과 drain 순서가 입력 순서를 따릅니다. 이는 승인된 [2026-07-30 remediation](../specs/2026-07-30-action-log-review-remediation.md)의 최종 계약입니다.
- mutable metadata는 drain마다 `pop`되고 duplicate/read-only 조건은 legacy로 갑니다.
- 단일 staging 검증에 전체 파일 `pq.read_table(request.output_path)`가 없습니다.
- `jobs/action_log.py`, action log schema, Airflow resource request 변경이 없습니다.

- [ ] **Step 7: 최종 구현 커밋 상태를 확인합니다.**

```bash
git status --short
git log --oneline -4
```

Expected: 계획 문서 이외 구현 worktree가 clean이고 Task 1~4의 작은 커밋이 순서대로 보입니다.
