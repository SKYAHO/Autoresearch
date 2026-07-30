# Action Log Streaming Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PR #415의 bounded single-mode streaming을 유지하면서 완료 시각 `generated_at`, bounded Parquet row groups, worker utilization, 운영 retention telemetry, quarantine 및 metadata 계약을 legacy와 호환되게 완성한다.

**Architecture:** 사용자별 event는 generation 중 Arrow IPC와 임시 JSONL에 기록하고, quarantine 검사를 통과한 뒤 완료 시각을 붙여 50,000-row Parquet row group으로 최종화한다. LLM worker 수와 active-user window를 `1:4`로 분리하고 Future는 worker 수만큼만 sliding 제출한다. 테스트 observer와 운영 structured telemetry는 동일한 retention snapshot을 사용한다.

**Tech Stack:** Python 3.11/3.12, `concurrent.futures`, PyArrow IPC/Parquet, Pydantic v2, pytest, ruff

## Global Constraints

- `generated_at`은 모든 사용자 생성과 quarantine 검사가 끝난 뒤 한 번 계산하며 legacy `EventLogBatch` 완료 시각 의미를 유지한다.
- 최종 Parquet row group은 정확히 최대 50,000 row이고 사용자 수에 비례해 늘어나지 않는다.
- generation 중 event와 JSONL은 run-local spool에 기록하고 전체 event를 Python list로 materialize하지 않는다.
- quarantine ratio 초과 시 quarantine JSONL만 최종 경로에 남기고 Parquet과 warehouse JSONL은 commit하지 않는다.
- `max_workers = max(1, request.max_concurrency)`이고 `max_active_users = 4 * max_workers`다.
- submitted Future는 최대 `max_workers`개이며 active user, draft, event buffer는 전체 사용자 수와 무관한 상한을 가진다.
- candidate provider는 coordinator thread에서 입력 사용자 순서대로 정확히 한 번 호출한다.
- 사용자의 모든 chunk가 모인 뒤 한 번만 click을 선정하고 사용자는 입력 순서대로 drain한다.
- Future 완료 순서와 무관하게 click, event 순서, seed timestamp, 전역 event ID sequence, warehouse/Parquet row 순서를 보존한다.
- `_retention_observer`와 운영 telemetry는 같은 snapshot을 사용하고 telemetry에는 user ID, persona, prompt, raw response, secret을 포함하지 않는다.
- `ACTION_LOG_TELEMETRY_INTERVAL_SEC`의 10~30초 검증과 `ACTION_LOG_TELEMETRY_DETAIL_MAX_WORK`의 detailed/aggregate 선택 효력을 유지한다.
- mutable exposure metadata는 drain된 사용자 entry를 제거하며 정상 streaming 종료 시 비어 있다. read-only mapping은 legacy fallback으로 소비하지 않는다.
- duplicate/missing user ID fallback, shard/checkpoint/merge, public CLI, Airflow KPO resource는 변경하지 않는다.
- production 코드보다 실패 테스트를 먼저 작성하고 각 RED와 GREEN 명령·출력을 report에 기록한다.
- 새 함수와 변경 함수는 반환형까지 구체적으로 표기하며 `Any`를 새로 도입하지 않는다.

## Planned File Structure

- Modify: `autoresearch/action_logs/pipeline.py` — transactional streaming sink, bounded coordinator, snapshot 생성과 single-mode 계약을 소유한다.
- Modify: `autoresearch/action_logs/observability.py` — streaming progress/detailed telemetry의 throttle과 metric 집계를 소유한다.
- Modify: `tests/test_action_logs_pipeline.py` — sink, concurrency, memory lifetime, metadata, quarantine, 결정론 회귀를 검증한다.
- Modify: `tests/test_action_logs_observability.py` — streaming telemetry payload, interval, detail threshold를 검증한다.
- Modify: `tests/test_action_logs_daily.py` — final Parquet schema/row-group validation 및 publish 회귀를 검증한다.
- Modify: `autoresearch/action_logs/daily.py` — module docstring에 single coordinator가 completion-time Parquet을 최종화한 뒤 staging 검증과 publish가 이어지는 경계를 기록한다. single CLI 인자와 shard 경로는 변경하지 않는다.

## Sequential Dependency Graph

`Task 1 → Task 2 → Task 3 → Task 4`

- Task 1이 IPC spool과 성공/실패 commit API를 제공한다.
- Task 2가 Task 1을 사용하는 bounded sliding coordinator와 확장 snapshot을 제공한다.
- Task 3이 Task 2 snapshot과 work timing을 운영 telemetry에 연결한다.
- Task 4가 모든 계약을 daily 경로와 전체 회귀 수준에서 검증하고 문서를 정리한다.
- Task 1~3은 같은 `pipeline.py` 영역을 수정하므로 구현 subagent를 병렬 실행하지 않는다.

---

### Task 1: Transactional IPC Sink, Completion `generated_at`, and Parquet Row Groups

**Files:**
- Modify: `autoresearch/action_logs/pipeline.py`
- Test: `tests/test_action_logs_pipeline.py`

**Interfaces:**
- Produces:
  - `_EVENT_SPOOL_SCHEMA: pa.Schema` — `EVENT_LOG_PARQUET_SCHEMA`에서 `generated_at`을 제외한 schema
  - `_PARQUET_TARGET_ROW_GROUP_ROWS = 50_000`
  - `_StreamingActionLogWriter(request: EventGenerationRequest, model_name: str)`
  - `_StreamingActionLogWriter.write_events(events: list[EventLog]) -> None`
  - `_StreamingActionLogWriter.write_quarantine(records: list[QuarantineRecord]) -> None`
  - `_StreamingActionLogWriter.finalize_success(generated_at: str, buffered_events_observer: Callable[[int], None] | None = None) -> None`
  - `_StreamingActionLogWriter.finalize_quarantine_failure() -> None`
- Preserves:
  - legacy `write_event_log_parquet`, warehouse writer, quarantine writer
  - final event and JSONL row order
  - empty-event output schema

- [ ] **Step 1: Write the failing completion-time and row-group contract test.**

Replace the current per-user-row-group writer assertion with a consumer-visible test. It must monkeypatch `_PARQUET_TARGET_ROW_GROUP_ROWS` to `3`, write two ordered calls of two events, finalize with literal `"2026-07-30T09:00:00+00:00"`, and assert:

```python
assert parquet.num_row_groups == 2
assert parquet.metadata.row_group(0).num_rows == 3
assert parquet.metadata.row_group(1).num_rows == 1
assert parquet.read(columns=["event_id"]).column(0).to_pylist() == [
    first.event_id,
    second.event_id,
    third.event_id,
    fourth.event_id,
]
assert set(
    parquet.read(columns=["generated_at"]).column(0).to_pylist()
) == {"2026-07-30T09:00:00+00:00"}
```

Also assert warehouse JSONL order and quarantine JSONL order remain unchanged.

- [ ] **Step 2: Run the focused test and record RED.**

Run:

```bash
uv run python -m pytest tests/test_action_logs_pipeline.py::test_streaming_writer_finalizes_completion_time_and_bounded_row_groups -v
```

Expected: current writer rejects the new constructor/finalize API or produces one row group per `write_events()` call.

- [ ] **Step 3: Write the failing quarantine transactional-output test.**

Create a request whose three final paths do not exist, write one event and one quarantine record, call `finalize_quarantine_failure()`, exit the context, and assert:

```python
assert not Path(request.output_path).exists()
assert not Path(request.warehouse_output_path).exists()
assert Path(request.quarantine_output_path).read_text(encoding="utf-8").count("\n") == 1
```

Add an exception-path test that raises inside the context and asserts all run-local spool files are removed and no final path is created.

- [ ] **Step 4: Run both sink tests and record RED.**

Run:

```bash
uv run python -m pytest \
  tests/test_action_logs_pipeline.py::test_streaming_writer_finalizes_completion_time_and_bounded_row_groups \
  tests/test_action_logs_pipeline.py::test_streaming_writer_quarantine_failure_commits_only_quarantine \
  tests/test_action_logs_pipeline.py::test_streaming_writer_exception_removes_spools -v
```

Expected: missing transactional finalization behavior.

- [ ] **Step 5: Implement Arrow IPC spooling and typed resource ownership.**

Use sibling temporary files for each target so `Path.replace()` stays on the target filesystem. The writer must:

```python
self._warehouse_file: TextIO | None = None
self._quarantine_file: TextIO | None = None
self._event_sink: pa.OSFile | None = None
self._event_stream: pa.ipc.RecordBatchStreamWriter | None = None
```

`write_events()` converts each user event list to `_EVENT_SPOOL_SCHEMA`, writes record batches to IPC, writes warehouse JSONL immediately to its spool, and retains no event list after returning. `write_quarantine()` writes only to its spool.

`__exit__()` closes all handles through `ExitStack`. If neither finalize method committed output, it removes every spool. Partial open failures close and remove already-created resources.

- [ ] **Step 6: Implement exact 50,000-row Parquet finalization.**

`finalize_success()` closes generation handles, opens the IPC stream, and slices input batches into an accumulator of at most `_PARQUET_TARGET_ROW_GROUP_ROWS`. For every flush:

1. construct a zero-copy table from accumulated record batches where possible;
2. append a `generated_at` string column filled with the supplied literal;
3. reorder/cast to `EVENT_LOG_PARQUET_SCHEMA`;
4. call `ParquetWriter.write_table()` once for that row group;
5. call `buffered_events_observer(0)` after releasing the table when an observer is present.

Before a flush, call the observer with the current buffered row count. Empty IPC input still creates a schema-correct empty Parquet. After Parquet close, atomically replace warehouse and quarantine targets. Mark the writer committed only after all three final paths exist.

`finalize_quarantine_failure()` closes handles, atomically replaces only the quarantine target, removes event/warehouse spools, and marks the writer committed.

- [ ] **Step 7: Move `generated_at` and quarantine gating to finalization.**

In `generate_action_log_single()` remove constructor-time `generated_at`. After all users drain:

```python
try:
    _raise_if_quarantine_count_exceeds(...)
except ActionLogGenerationError:
    writer.finalize_quarantine_failure()
    raise
generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
writer.finalize_success(generated_at)
```

This timestamp is computed after generation and quarantine validation but before final Parquet writing, matching legacy `EventLogBatch` semantics.

- [ ] **Step 8: Run sink GREEN and pipeline regression.**

Run:

```bash
uv run python -m pytest \
  tests/test_action_logs_pipeline.py::test_streaming_writer_finalizes_completion_time_and_bounded_row_groups \
  tests/test_action_logs_pipeline.py::test_streaming_writer_quarantine_failure_commits_only_quarantine \
  tests/test_action_logs_pipeline.py::test_streaming_writer_exception_removes_spools \
  tests/test_action_logs_pipeline.py::test_streaming_single_raises_after_writing_quarantine_when_ratio_exceeded \
  tests/test_action_logs_pipeline.py -q
```

Expected: all pipeline tests pass with no warnings.

- [ ] **Step 9: Commit Task 1.**

```bash
git add autoresearch/action_logs/pipeline.py tests/test_action_logs_pipeline.py
git commit -m "fix: action log 출력을 완료 시각으로 최종화"
```

---

### Task 2: Bounded Sliding Work Scheduler and Retention Snapshot

**Files:**
- Modify: `autoresearch/action_logs/pipeline.py`
- Test: `tests/test_action_logs_pipeline.py`

**Interfaces:**
- Consumes: Task 1 `_StreamingActionLogWriter`
- Produces:
  - `_STREAMING_ACTIVE_USER_MULTIPLIER = 4`
  - Future upper bound `max_workers`
  - active-user upper bound `4 * max_workers`
  - `_StreamingRetentionSnapshot` fields:

```python
phase: Literal["generating", "finalizing"]
active_users: int
buffered_drafts: int
buffered_events: int
in_flight_work: int
activated_users: int
total_users: int
submitted_work: int
total_work: int | None
completed_work: int
failed_work: int
pending_work: int | None
```

- Preserves: provider call order, user drain order, chunk order, event ID and seed decisions

- [ ] **Step 1: Write a failing head-of-line utilization test.**

Use a controlled generator with `max_concurrency=2`, one chunk per user, and at least three users:

- user 0 sets `first_started` and waits on `release_first`;
- user 1 completes immediately;
- user 2 sets `third_started`;
- the test releases user 0 after a bounded wait.

Assert `third_started` became true before `release_first` was set by test cleanup. This catches the current behavior where user 2 is not activated until user 0 drains.

- [ ] **Step 2: Run the utilization test and record RED.**

Run:

```bash
uv run python -m pytest tests/test_action_logs_pipeline.py::test_streaming_single_keeps_workers_busy_behind_slow_head_user -v
```

Expected: `third_started` is false before the slow head is released.

- [ ] **Step 3: Extend the retention-bound test and record RED.**

Update the existing retention test to assert:

```python
assert max(item.active_users for item in snapshots) <= 4 * max_concurrency
assert max(item.in_flight_work for item in snapshots) <= max_concurrency
assert max(item.buffered_drafts for item in snapshots) <= (
    4 * max_concurrency * candidates_per_user
)
assert max(item.buffered_events for item in snapshots) <= 50_000
```

Also assert `activated_users` is monotonic, the final generating snapshot has
`pending_work == 0`, and the final snapshot has all count fields at zero where
ownership has been released.

Run:

```bash
uv run python -m pytest tests/test_action_logs_pipeline.py::test_streaming_single_retained_payload_is_bounded_by_active_users -v
```

Expected: current snapshot lacks the new fields or active-user contract.

- [ ] **Step 4: Implement separate active-user and Future windows.**

Use:

```python
max_workers = max(1, request.max_concurrency)
max_active_users = _STREAMING_ACTIVE_USER_MULTIPLIER * max_workers
```

Activation constructs state in input order and appends not-yet-submitted chunks to a coordinator-owned deterministic deque. `_submit_available_work()` submits from that deque only while `len(futures) < max_workers`. After every completion batch it submits replacement work before attempting ordered drain.

When ordered drain frees an active-user slot, fill the window from the provider and append the new chunks. Do not submit more than `max_workers` Future objects and do not activate beyond `max_active_users`.

- [ ] **Step 5: Populate the extended snapshot from real ownership state.**

Track submitted, completed, failed, activated, known work sequence and
provider-exhausted state. Before provider exhaustion set `total_work=None` and
`pending_work=None`; afterward set:

```python
total_work = next_work_sequence
pending_work = total_work - completed_work - in_flight_work
```

Include not-yet-submitted queued chunks in pending work. During Task 1 finalization call the same observer with `phase="finalizing"` and current row buffer. The private callback remains optional and receives no user identifiers.

- [ ] **Step 6: Run scheduler GREEN and deterministic regressions.**

Run:

```bash
uv run python -m pytest \
  tests/test_action_logs_pipeline.py::test_streaming_single_keeps_workers_busy_behind_slow_head_user \
  tests/test_action_logs_pipeline.py::test_streaming_single_retained_payload_is_bounded_by_active_users \
  tests/test_action_logs_pipeline.py::test_streaming_single_matches_legacy_when_later_work_finishes_first \
  tests/test_action_logs_pipeline.py::test_streaming_single_releases_completed_future_drafts_before_next_provider \
  tests/test_action_logs_pipeline.py -q
```

Expected: all pipeline tests pass with no warnings.

- [ ] **Step 7: Commit Task 2.**

```bash
git add autoresearch/action_logs/pipeline.py tests/test_action_logs_pipeline.py
git commit -m "fix: action log worker와 active user 상한 분리"
```

---

### Task 3: Streaming Operational Telemetry

**Files:**
- Modify: `autoresearch/action_logs/observability.py`
- Modify: `autoresearch/action_logs/pipeline.py`
- Test: `tests/test_action_logs_observability.py`
- Test: `tests/test_action_logs_pipeline.py`

**Interfaces:**
- Consumes: Task 2 snapshot and `_ActionLogCallResult` timing
- Produces:
  - `ActionLogStreamingTelemetryReporter`
  - event `action_log_streaming_progress`
  - existing event `action_log_micro_work_complete` for confirmed small runs
- Preserves: existing shard `ActionLogTelemetryReporter` behavior and environment validation

- [ ] **Step 1: Write failing aggregate streaming telemetry tests.**

In `tests/test_action_logs_observability.py`, use `caplog` and a controllable monotonic clock to instantiate the wished-for reporter, emit start/record/finish states, and assert literal JSON fields:

```python
assert progress["event"] == "action_log_streaming_progress"
assert progress["phase"] == "generating"
assert progress["active_users"] == 4
assert progress["buffered_drafts"] == 48
assert progress["buffered_events"] == 0
assert progress["in_flight_work"] == 2
assert progress["activated_users"] == 4
assert progress["total_users"] == 100
assert progress["submitted_work"] == 4
assert progress["total_work"] is None
assert progress["completed_work"] == 2
assert progress["failed_work"] == 0
assert "pending_work" in progress
```

Verify start and finish are forced and intermediate progress respects
`ACTION_LOG_TELEMETRY_INTERVAL_SEC=10`.

- [ ] **Step 2: Run aggregate telemetry RED.**

Run:

```bash
uv run python -m pytest tests/test_action_logs_observability.py::test_streaming_telemetry_emits_retention_and_progress_fields -v
```

Expected: `ActionLogStreamingTelemetryReporter` does not exist.

- [ ] **Step 3: Write failing detail-threshold tests.**

Create two reporters with `detail_max_work=2`:

- exact final total 2: two buffered work metrics produce two
  `action_log_micro_work_complete` events in work-sequence order;
- submitted work reaches 3: buffered detail is discarded and no detailed event appears.

Assert aggregate latency is based on the bounded interval window and that no log payload contains `user_id`, `raw_text`, or prompt data.

- [ ] **Step 4: Run detail telemetry RED.**

Run:

```bash
uv run python -m pytest \
  tests/test_action_logs_observability.py::test_streaming_telemetry_emits_details_only_after_small_total_is_known \
  tests/test_action_logs_observability.py::test_streaming_telemetry_discards_details_above_threshold -v
```

Expected: missing streaming reporter behavior.

- [ ] **Step 5: Implement `ActionLogStreamingTelemetryReporter`.**

Reuse `_env_int`, `_env_float`, `_latency_percentiles`, `_average`, and
`emit_action_log_event`. Add an `include_none_fields: bool = False` keyword to
`emit_action_log_event`; existing callers keep filtering `None`, while streaming
progress passes `include_none_fields=True` so unknown `pending_work` is JSON
`null`.

The reporter:

- validates the same environment settings as the shard reporter;
- keeps only the current aggregate interval latency window;
- buffers at most `detail_max_work` work metric dictionaries;
- clears detailed metrics permanently once submitted work exceeds the threshold;
- emits buffered detailed events only after provider exhaustion makes exact total
  work known and total is within the threshold;
- forces start and finish progress events.

- [ ] **Step 6: Connect pipeline snapshots and work results to telemetry.**

At single generation start restore the existing `"Starting action log draft generation"`
log. Construct the streaming reporter before activation. Every `_observe()` passes the
same count values to both `_retention_observer` and telemetry. Every completed
`_ActionLogCallResult` passes queue, request, parse and total elapsed metrics to
`record_work()`.

Use `telemetry.detailed_candidate` for `_generate_action_log_work()` context only
while detailed buffering remains possible. Emit no identifiers or raw model data.
Force a final `phase="finalizing"` snapshot after Task 1 output finalization releases
its row buffer.

- [ ] **Step 7: Run telemetry GREEN and legacy observability regression.**

Run:

```bash
uv run python -m pytest \
  tests/test_action_logs_observability.py \
  tests/test_action_logs_pipeline.py::test_streaming_single_emits_operational_retention_telemetry \
  tests/test_action_logs_pipeline.py -q
```

Expected: all selected tests pass and legacy shard telemetry tests remain unchanged.

- [ ] **Step 8: Commit Task 3.**

```bash
git add \
  autoresearch/action_logs/observability.py \
  autoresearch/action_logs/pipeline.py \
  tests/test_action_logs_observability.py \
  tests/test_action_logs_pipeline.py
git commit -m "fix: single action log retention telemetry 복구"
```

---

### Task 4: Metadata Contract, Daily Integration, and Full Regression

**Files:**
- Modify: `autoresearch/action_logs/pipeline.py`
- Modify: `autoresearch/action_logs/daily.py`
- Test: `tests/test_action_logs_pipeline.py`
- Test: `tests/test_action_logs_daily.py`
- Modify: `docs/plans/2026-07-29-action-log-memory-retention.md`

**Interfaces:**
- Consumes: Tasks 1~3 complete single-mode API
- Produces: documented mutable metadata contract and reviewed end-to-end behavior
- Preserves: public CLI, daily publish, last-known-good, shard/checkpoint/merge

- [ ] **Step 1: Strengthen metadata and completion-time behavior tests.**

Keep the real mutable metadata tests and read-only `MappingProxyType` fallback test.
Add a frozen-clock integration test that gives generation start and completion different
literal timestamps and asserts final daily Parquet rows contain only the completion
timestamp. The test must call the real single daily path; mock only filesystem/network
boundaries already mocked by neighboring daily tests.

- [ ] **Step 2: Run the new daily test and record RED if integration is incomplete.**

Run:

```bash
uv run python -m pytest tests/test_action_logs_daily.py::test_single_daily_publishes_completion_generated_at_without_full_materialization -v
```

Expected: PASS if Tasks 1~3 fully connected the path; if it passes immediately, record it
as a characterization/integration confirmation and do not claim a new TDD production
cycle from it.

- [ ] **Step 3: Finalize mutable metadata and observer documentation.**

Change `generate_action_log_single()` annotation to:

```python
exposure_metadata: (
    MutableMapping[tuple[str, str], ExposureMetadata] | None
) = None
```

Keep the runtime `isinstance(..., MutableMapping)` fallback for untyped/read-only callers.
The docstring must state that mutable entries are removed per drained user and the mapping
is empty after normal streaming completion; read-only mappings use legacy without
mutation. Document `_retention_observer` as a private regression hook whose snapshot is
also emitted to DAG structured telemetry.

Update the 2026-07-29 implementation plan constraints that currently require one Parquet
row group per user and `max_active_users == max_concurrency`; point to the approved
2026-07-30 remediation spec and record the final `4x`/50,000-row contracts.

Update the `daily.py` module docstring without changing executable logic: state that the
single coordinator commits completion-time Parquet/JSONL into the daily temporary
directory, after which daily performs row-group staging validation and last-known-good
publish. Keep the existing `[파이프라인]`, `[기능]`, `[비책임]` structure.

- [ ] **Step 4: Run focused pipeline and daily suites.**

Run:

```bash
uv run python -m pytest tests/test_action_logs_pipeline.py -v
uv run python -m pytest tests/test_action_logs_observability.py -v
uv run python -m pytest tests/test_action_logs_daily.py -v
```

Expected: all tests pass with no warnings.

- [ ] **Step 5: Run repository verification.**

Run:

```bash
uv run python -m pytest -v
uv run --no-sync ruff check autoresearch tests tools
git diff --check
```

Expected: full suite passes, ruff reports no errors, and diff check emits no output.

- [ ] **Step 6: Commit Task 4.**

```bash
git add \
  autoresearch/action_logs/pipeline.py \
  autoresearch/action_logs/daily.py \
  tests/test_action_logs_pipeline.py \
  tests/test_action_logs_daily.py \
  docs/plans/2026-07-29-action-log-memory-retention.md
git commit -m "fix: action log streaming 계약과 daily 회귀 정리"
```

## Final Review and PR Follow-up

After all four task reviews are clean:

1. Run a Sol xhigh whole-branch review against the PR merge base.
2. Fix all Critical/Important findings in one fix wave and run one scoped re-review.
3. Re-run pipeline, observability, daily, full pytest, ruff, and `git diff --check`.
4. Push `fix/393-action-log-memory-retention`.
5. Reply to each Claude inline thread with test/implementation evidence.
6. Resolve only threads whose requested behavior is implemented or whose trade-off is
   explicitly documented with evidence.
7. Verify PR HEAD, checks, unresolved thread count, and mergeability before reporting.
