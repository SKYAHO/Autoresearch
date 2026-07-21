# 일일 폐루프 완성 구현 계획 (#222)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 일일 action-log CLI(single·shard)가 #221의 모델 노출 provider를 기본 사용하고, 노출 태그가 draft를 타고 shard→merge를 건너 최종 event log에 조인되게 한다.

**Architecture:** 태그는 `ImpressionDraft`에 additive optional 필드 4개로 실어 운반한다(merge 재조립 금지 — 인자 drift가 조용히 잘못된 태그를 만들기 때문). `_expand_events`는 외부 metadata가 없으면 draft에 실린 태그로부터 맵을 복원한다(fallback)이므로 merge는 무변경이다. provider 구성(BQ)은 공개 CLI(`autoresearch/jobs/action_log.py`)가 model 모드에서만 `src.pipeline`을 지연 import해 수행하고, daily에는 `candidate_provider_factory` seam으로 주입한다. Source spec: `docs/specs/2026-07-22-daily-closed-loop.md`.

**Tech Stack:** Python 3.12, pydantic, pyarrow, google-cloud-bigquery(지연), pytest.

## Global Constraints

- 브랜치: `feat/222-daily-closed-loop` (이슈 #222에서 생성).
- 한국어 격식체 docstring, 모든 함수 타입 힌트(반환 포함).
- `autoresearch/action_logs/`는 BQ·`src` 비의존 순수 유지 — `src.pipeline` 지연 import는 `autoresearch/jobs/action_log.py`의 model 모드 factory 안에서만.
- 스키마 확장은 additive optional(None 기본) — 구 shard draft·체크포인트 하위 호환.
- **함정(메모리 기록)**: additive 스키마 확장은 정확 동등 단언과 충돌할 수 있다 — 구현 전 `grep -rn "model_dump()\|set(row" tests/test_action_logs_pipeline.py tests/test_action_logs_daily.py`로 draft row 키 집합 `==` 단언을 찾아 함께 갱신한다.
- LLM 프롬프트에 태그 비노출 — 태그는 draft 필드로만 운반, 후보 dict는 불변.
- `--exposure-source` 기본 `model`(single·shard), merge는 거부. 휴리스틱은 명시 플래그 폴백. rankings dt = `--partition-date` (dt 정합).
- 기존 Python 호출 하위 호환: daily 함수 신규 파라미터는 전부 keyword-only 기본 None.
- 테스트는 실 BQ·실 LLM 미접속(rule_based generator, factory 모킹). 테스트 명령: `uv run python -m pytest tests/<파일>.py -v` (WSL — timeout 상향).
- 계약 변경은 `docs/specs/2026-07-13-public-batch-execution-contract.md` 갱신 필수(batch-contract-v1 유지 — 인자 additive).

## 파일 구조 (최종)

```
autoresearch/action_logs/schema.py        # 수정: ImpressionDraft 태그 4필드 (Task 1)
autoresearch/action_logs/pipeline.py      # 수정: DRAFT 스키마 4컬럼, attach/복원 헬퍼, _expand_events fallback (Task 1), generate_action_log_batch 배선 (Task 2)
autoresearch/action_logs/daily.py         # 수정: single·shard factory seam + shard attach (Task 3)
autoresearch/jobs/action_log.py           # 수정: --exposure-source/--recommendations-table + factory 구성 (Task 4)
src/pipeline/model_exposure_provider.py   # 수정: resolve_recommendations_table_id (Task 4)
docs/specs/2026-07-13-public-batch-execution-contract.md  # 수정: 계약 등재 (Task 4)
tests/test_action_logs_schema_policy.py   # 수정 (Task 1)
tests/test_action_logs_pipeline.py        # 수정 (Task 1, 2)
tests/test_action_logs_daily.py           # 수정 (Task 3)
tests/test_action_log_job.py              # 수정 (Task 4)
tests/test_model_exposure_provider.py     # 수정 (Task 4)
```

---

### Task 1: draft 태그 운반 계약 (스키마 + attach/복원 + expand fallback)

**Files:**
- Modify: `autoresearch/action_logs/schema.py` (ImpressionDraft)
- Modify: `autoresearch/action_logs/pipeline.py` (ACTION_LOG_DRAFT_PARQUET_SCHEMA:181, `attach_exposure_tags`·`_exposure_metadata_from_drafts` 신규, `_expand_events` fallback)
- Test: `tests/test_action_logs_schema_policy.py`, `tests/test_action_logs_pipeline.py`

**Interfaces:**
- Produces (Task 2·3이 사용):
  - `ImpressionDraft.exposure_source: Literal["model","trending","random"] | None = None`, `exposure_rank: int | None (ge=1)`, `exposure_ctr_score: float | None`, `policy_version: str | None`
  - `attach_exposure_tags(drafts: list[ImpressionDraft], metadata: Mapping[tuple[str, str], ExposureMetadata]) -> list[ImpressionDraft]`
  - `_expand_events`: `metadata=None`이면 draft 태그로부터 맵 복원(policy="model", is_exploration=source=="random"); 외부 metadata 인자는 우선(기존 동작 불변)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_action_logs_pipeline.py`에 추가

```python
def _tagged_draft(**overrides) -> ImpressionDraft:
    base = dict(
        user_id="u1", video_id="v1", click_propensity=0.9,
        watch_fraction=0.4, would_like=False, duration_sec=100,
        exposure_source="model", exposure_rank=3, exposure_ctr_score=0.7,
        policy_version="run-a",
    )
    base.update(overrides)
    return ImpressionDraft(**base)


def test_draft_exposure_tags_roundtrip_parquet(tmp_path):
    drafts = [
        _tagged_draft(),
        _tagged_draft(video_id="v2", exposure_source="random",
                      exposure_rank=9, exposure_ctr_score=None),
    ]
    path = tmp_path / "drafts.parquet"
    write_action_log_draft_parquet(drafts, path)
    restored = read_action_log_draft_parquet(path)
    assert [d.exposure_source for d in restored] == ["model", "random"]
    assert restored[0].exposure_rank == 3 and restored[0].policy_version == "run-a"


def test_legacy_draft_parquet_without_tag_columns_reads_untagged(tmp_path):
    legacy_fields = [
        f for f in ACTION_LOG_DRAFT_PARQUET_SCHEMA
        if f.name not in ("exposure_source", "exposure_rank",
                          "exposure_ctr_score", "policy_version")
    ]
    row = {"user_id": "u1", "video_id": "v1", "click_propensity": 0.9,
           "watch_fraction": 0.4, "would_like": False, "duration_sec": 100}
    path = tmp_path / "legacy.parquet"
    pq.write_table(pa.Table.from_pylist([row], schema=pa.schema(legacy_fields)), path)
    restored = read_action_log_draft_parquet(path)
    assert restored[0].exposure_source is None


def test_attach_exposure_tags_leaves_unmapped_drafts_untagged():
    metadata = {
        ("u1", "v1"): ExposureMetadata(
            policy="model", rank=3, ctr_score=0.7, is_exploration=False,
            policy_version="run-a", exposure_source="model",
        )
    }
    plain = _tagged_draft(exposure_source=None, exposure_rank=None,
                          exposure_ctr_score=None, policy_version=None)
    other = _tagged_draft(video_id="vX", exposure_source=None, exposure_rank=None,
                          exposure_ctr_score=None, policy_version=None)
    tagged = attach_exposure_tags([plain, other], metadata)
    assert tagged[0].exposure_source == "model" and tagged[0].exposure_rank == 3
    assert tagged[1].exposure_source is None


def test_expand_events_joins_tags_from_draft_fallback(tmp_path):
    request = _request(tmp_path)
    drafts = [_tagged_draft(), _tagged_draft(video_id="v2", exposure_source="random",
                                             exposure_rank=2, exposure_ctr_score=None)]
    result = expand_action_log_drafts(request, drafts, [])
    impressions = [e for e in result.batch.events if e.event_type == "impression"]
    by_video = {e.video_id: e for e in impressions}
    assert by_video["v1"].exposure_source == "model"
    assert by_video["v1"].policy == "model" and by_video["v1"].rank == 3
    assert by_video["v1"].ctr_score == 0.7 and by_video["v1"].policy_version == "run-a"
    assert by_video["v2"].is_exploration is True
```

(기존 `_request(tmp_path)` 헬퍼 재사용. import에 `ACTION_LOG_DRAFT_PARQUET_SCHEMA`, `attach_exposure_tags`, `write_action_log_draft_parquet`, `read_action_log_draft_parquet`, `ExposureMetadata`, `ImpressionDraft`, `expand_action_log_drafts` 추가.)

- [ ] **Step 2: 실패 확인**

Run: `uv run python -m pytest tests/test_action_logs_pipeline.py -v -k "exposure_tags or legacy_draft or draft_fallback"`
Expected: FAIL — `exposure_source` unexpected keyword / `attach_exposure_tags` ImportError

- [ ] **Step 3: 구현**

`schema.py` — `ImpressionDraft`의 `duration_sec` 아래에:

```python
    # 노출 태그 (#222 폐루프): provider가 남긴 출처·순위·점수·계보를 draft에
    # 실어 shard→merge를 건너 운반한다. 휴리스틱 라운드·구 shard는 전부 None.
    exposure_source: Literal["model", "trending", "random"] | None = None
    exposure_rank: int | None = Field(default=None, ge=1)
    exposure_ctr_score: float | None = None
    policy_version: str | None = None
```

`pipeline.py` — ① `ACTION_LOG_DRAFT_PARQUET_SCHEMA`(:181) 말미에:

```python
        pa.field("exposure_source", pa.string()),
        pa.field("exposure_rank", pa.int64()),
        pa.field("exposure_ctr_score", pa.float64()),
        pa.field("policy_version", pa.string()),
```

(checkpoint 스키마는 `*ACTION_LOG_DRAFT_PARQUET_SCHEMA` spread라 자동 상속.)

② `ExposureMetadata` 정의 아래에 헬퍼 2개:

```python
def attach_exposure_tags(
    drafts: list[ImpressionDraft],
    metadata: Mapping[tuple[str, str], ExposureMetadata],
) -> list[ImpressionDraft]:
    """provider가 남긴 노출 태그를 draft에 심는다(맵에 없는 draft는 무태그 유지)."""
    tagged: list[ImpressionDraft] = []
    for draft in drafts:
        meta = metadata.get((draft.user_id, draft.video_id))
        if meta is None or meta.exposure_source is None:
            tagged.append(draft)
            continue
        tagged.append(
            draft.model_copy(
                update={
                    "exposure_source": meta.exposure_source,
                    "exposure_rank": meta.rank,
                    "exposure_ctr_score": meta.ctr_score,
                    "policy_version": meta.policy_version,
                }
            )
        )
    return tagged


def _exposure_metadata_from_drafts(
    drafts: list[ImpressionDraft],
) -> dict[tuple[str, str], ExposureMetadata]:
    """draft에 실려 온 태그를 ExposureMetadata 맵으로 복원한다(merge/fallback 경로)."""
    metadata: dict[tuple[str, str], ExposureMetadata] = {}
    for draft in drafts:
        if draft.exposure_source is None:
            continue
        metadata[(draft.user_id, draft.video_id)] = ExposureMetadata(
            policy="model",
            rank=draft.exposure_rank if draft.exposure_rank is not None else 0,
            ctr_score=draft.exposure_ctr_score,
            is_exploration=draft.exposure_source == "random",
            policy_version=draft.policy_version,
            exposure_source=draft.exposure_source,
        )
    return metadata
```

③ `_expand_events` 본문 최상단(기존 `end = request.history_end` 위)에:

```python
    if metadata is None:
        # draft에 실려 온 태그가 있으면 그것으로 조인한다(merge 경로 무변경 —
        # 외부 metadata 인자(정책 시뮬레이션 라운드)는 그대로 우선).
        embedded = _exposure_metadata_from_drafts(drafts)
        metadata = embedded or None
```

- [ ] **Step 4: Global Constraints의 함정 grep 수행** — draft row 키 집합 정확 동등 단언이 발견되면 4개 키를 추가 갱신한다(발견 없으면 통과).

- [ ] **Step 5: 통과 확인**

Run: `uv run python -m pytest tests/test_action_logs_pipeline.py tests/test_action_logs_schema_policy.py -v`
Expected: PASS (전체)

- [ ] **Step 6: Commit**

```bash
git add autoresearch/action_logs/schema.py autoresearch/action_logs/pipeline.py \
  tests/test_action_logs_pipeline.py tests/test_action_logs_schema_policy.py
git commit -m "feat: draft에 노출 태그 운반 계약 추가 (#222)"
```

---

### Task 2: `generate_action_log_batch` 배선 (single 경로)

**Files:**
- Modify: `autoresearch/action_logs/pipeline.py` (`generate_action_log_batch`:1046)
- Test: `tests/test_action_logs_pipeline.py`

**Interfaces:**
- Consumes: Task 1 `attach_exposure_tags`
- Produces (Task 3이 사용): `generate_action_log_batch(..., *, candidate_provider: CandidateProvider | None = None, exposure_metadata: Mapping[tuple[str, str], ExposureMetadata] | None = None)` — exposure_metadata는 **공유 가변 맵**(provider 호출이 진행되며 채워짐)이므로 draft 생성 후에 참조한다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_batch_attaches_provider_exposure_tags(tmp_path):
    users, videos = _fixture_users(2), build_fixture_video_records(10)
    metadata: dict[tuple[str, str], ExposureMetadata] = {}

    def provider(virtual_user: dict, user_rng) -> list[dict]:
        picked = videos[:3]
        for position, video in enumerate(picked, start=1):
            metadata[(virtual_user["user_id"], str(video["video_id"]))] = (
                ExposureMetadata(
                    policy="model", rank=position, ctr_score=0.5,
                    is_exploration=False, policy_version="run-a",
                    exposure_source="model",
                )
            )
        return picked

    result = generate_action_log_batch(
        _request(tmp_path), users, videos, RuleBasedActionLogGenerator(),
        candidate_provider=provider, exposure_metadata=metadata,
    )
    impressions = [e for e in result.batch.events if e.event_type == "impression"]
    assert impressions and all(e.exposure_source == "model" for e in impressions)
    assert all(e.policy_version == "run-a" for e in impressions)
```

(기존 `_fixture_users`·`build_fixture_video_records`·`_request` 헬퍼 재사용.)

- [ ] **Step 2: 실패 확인**

Run: `uv run python -m pytest tests/test_action_logs_pipeline.py::test_batch_attaches_provider_exposure_tags -v`
Expected: FAIL — unexpected keyword `candidate_provider`

- [ ] **Step 3: 구현** — `generate_action_log_batch` 시그니처·본문:

```python
def generate_action_log_batch(
    request: EventGenerationRequest,
    virtual_users: list[dict],
    videos: list[dict],
    generator: ActionLogGenerator,
    progress_callback: ActionLogProgressCallback | None = None,
    *,
    candidate_provider: CandidateProvider | None = None,
    exposure_metadata: Mapping[tuple[str, str], ExposureMetadata] | None = None,
) -> EventGenerationResult:
    """유저 단위 격리 생성 → 전역 2% 정규화 → 조립 → 파일 저장을 실행한다.

    exposure_metadata는 candidate_provider 호출이 진행되며 채워지는 공유 맵일 수
    있으므로(#221 ModelExposureRound), draft 생성이 끝난 뒤에 참조한다.
    """

    draft_result = generate_action_log_drafts(
        request,
        virtual_users,
        videos,
        generator,
        progress_callback,
        candidate_provider=candidate_provider,
    )
    drafts = draft_result.drafts
    if exposure_metadata is not None:
        drafts = attach_exposure_tags(drafts, exposure_metadata)
    result = expand_action_log_drafts(
        request,
        drafts,
        draft_result.quarantine,
    )
```

(이하 파일 저장 로직 불변 — `result` 사용부 그대로.)

- [ ] **Step 4: 통과 확인**

Run: `uv run python -m pytest tests/test_action_logs_pipeline.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add autoresearch/action_logs/pipeline.py tests/test_action_logs_pipeline.py
git commit -m "feat: generate_action_log_batch에 provider·태그 배선 (#222)"
```

---

### Task 3: daily single·shard factory seam

**Files:**
- Modify: `autoresearch/action_logs/daily.py` (`run_daily_action_log`:788, `run_daily_action_log_shard`:928)
- Test: `tests/test_action_logs_daily.py`

**Interfaces:**
- Consumes: Task 1 `attach_exposure_tags`, Task 2 배선
- Produces (Task 4가 사용): 두 함수의 신규 keyword-only 파라미터

```python
candidate_provider_factory: Callable[
    [list[dict]],
    tuple[CandidateProvider, Mapping[tuple[str, str], ExposureMetadata]],
] | None = None
```

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_action_logs_daily.py`에 추가 (기존 daily 테스트의 파티션 픽스처 헬퍼를 재사용해 작성하되, 없으면 아래처럼 인라인 구성)

```python
def _closed_loop_factory(videos: list[dict]):
    metadata: dict[tuple[str, str], ExposureMetadata] = {}

    def provider(virtual_user: dict, user_rng) -> list[dict]:
        picked = videos[:3]
        user_id = str(virtual_user.get("user_id", ""))
        for position, video in enumerate(picked, start=1):
            metadata[(user_id, str(video["video_id"]))] = ExposureMetadata(
                policy="model", rank=position, ctr_score=0.5,
                is_exploration=False, policy_version="run-a",
                exposure_source="model",
            )
        return picked

    return provider, metadata


def test_daily_single_joins_exposure_tags_into_final_parquet(tmp_path):
    paths = _daily_fixture_paths(tmp_path)  # 기존 픽스처 헬퍼(YouTube·VU parquet 구성)
    result = run_daily_action_log(
        partition_date=_PARTITION,
        youtube_base_path=paths.youtube,
        virtual_users_path=paths.users,
        output_base_path=paths.output,
        candidate_provider_factory=_closed_loop_factory,
        overwrite=True,
    )
    assert result["status"] == "succeeded"
    table = pq.read_table(_dt_file(paths.output))
    sources = set(table.column("exposure_source").to_pylist())
    assert "model" in sources


def test_daily_shard_then_merge_carries_exposure_tags(tmp_path):
    paths = _daily_fixture_paths(tmp_path)
    run_daily_action_log_shard(
        partition_date=_PARTITION, shard_index=0, shard_count=1,
        youtube_base_path=paths.youtube, virtual_users_path=paths.users,
        shard_output_base_path=paths.shards, progress_base_path=paths.progress,
        candidate_provider_factory=_closed_loop_factory,
    )
    merge_result = run_daily_action_log_merge(  # 기존 merge 진입점 시그니처 준수
        partition_date=_PARTITION, shard_count=1,
        shard_output_base_path=paths.shards, output_base_path=paths.output,
    )
    assert merge_result["status"] == "succeeded"
    table = pq.read_table(_dt_file(paths.output))
    assert "model" in set(table.column("exposure_source").to_pylist())
```

(주의: 픽스처 헬퍼·merge 진입점의 실제 이름·필수 인자는 파일 내 기존 shard/merge 테스트를 열어 **그 패턴을 그대로 따른다** — 위 코드는 계약을 보여주는 골격이며, 단언 3종(succeeded·single 태그·merge 태그)은 반드시 유지한다.)

- [ ] **Step 2: 실패 확인**

Run: `uv run python -m pytest tests/test_action_logs_daily.py -v -k "exposure"`
Expected: FAIL — unexpected keyword `candidate_provider_factory`

- [ ] **Step 3: 구현**

`daily.py` import에 `attach_exposure_tags`, `CandidateProvider`, `ExposureMetadata` 추가 (`from collections.abc import Callable, Mapping` 병용).

`run_daily_action_log` — 시그니처에 파라미터 추가 후, videos 로드 직후:

```python
    candidate_provider: CandidateProvider | None = None
    exposure_metadata: Mapping[tuple[str, str], ExposureMetadata] | None = None
    if candidate_provider_factory is not None:
        candidate_provider, exposure_metadata = candidate_provider_factory(videos)
```

`generate_action_log_batch(...)` 호출에 `candidate_provider=candidate_provider, exposure_metadata=exposure_metadata` 추가.

`run_daily_action_log_shard` — 동일 파라미터·동일 factory 호출을 videos 로드 직후에 두고, `generate_action_log_drafts(...)` 호출에 `candidate_provider=candidate_provider` 추가, draft 쓰기 직전(:1137 부근)을:

```python
            drafts_to_write = result.drafts
            if exposure_metadata is not None:
                drafts_to_write = attach_exposure_tags(result.drafts, exposure_metadata)
            write_action_log_draft_parquet(drafts_to_write, draft_path)
```

(merge 경로는 Task 1의 `_expand_events` fallback으로 무변경.)

- [ ] **Step 4: 통과 확인**

Run: `uv run python -m pytest tests/test_action_logs_daily.py -v`
Expected: PASS (기존 + 신규 2)

- [ ] **Step 5: Commit**

```bash
git add autoresearch/action_logs/daily.py tests/test_action_logs_daily.py
git commit -m "feat: daily single·shard에 노출 provider factory seam 추가 (#222)"
```

---

### Task 4: 공개 CLI 배선 + 계약 문서

**Files:**
- Modify: `autoresearch/jobs/action_log.py` (parser·검증·`_run`·factory 구성)
- Modify: `src/pipeline/model_exposure_provider.py` (`resolve_recommendations_table_id` 신규)
- Modify: `docs/specs/2026-07-13-public-batch-execution-contract.md`
- Test: `tests/test_action_log_job.py`, `tests/test_model_exposure_provider.py`

**Interfaces:**
- Consumes: Task 3 `candidate_provider_factory`, #221 `load_user_rankings`·`make_model_exposure_provider`
- Produces:
  - CLI 인자 `--exposure-source {model,heuristic}`(single·shard 기본 model, merge 거부), `--recommendations-table <bare name>`(model 모드만)
  - `resolve_recommendations_table_id(table: str | None) -> str` — `{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}.{table or env CTR_TRAINING_BQ_RECOMMENDATIONS_TABLE or "user_recommendations"}` (기본값 문자열의 단일 출처)

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_model_exposure_provider.py`:

```python
def test_resolve_recommendations_table_id_defaults_and_override(monkeypatch):
    monkeypatch.delenv("CTR_TRAINING_BQ_RECOMMENDATIONS_TABLE", raising=False)
    assert resolve_recommendations_table_id(None).endswith(".user_recommendations")
    assert resolve_recommendations_table_id("alt_table").endswith(".alt_table")
    monkeypatch.setenv("CTR_TRAINING_BQ_RECOMMENDATIONS_TABLE", "env_table")
    assert resolve_recommendations_table_id(None).endswith(".env_table")
```

`tests/test_action_log_job.py` (기존 parser/_run 테스트 패턴 준수):

```python
def test_exposure_source_defaults_to_model_for_single_and_shard():
    args = _parse_valid_single_args()  # 기존 헬퍼/패턴 재사용
    assert args.exposure_source == "model"


def test_merge_rejects_exposure_arguments():
    with pytest.raises(BatchArgumentError):
        _parse_args_for_mode("merge", "--exposure-source", "model")
    with pytest.raises(BatchArgumentError):
        _parse_args_for_mode("merge", "--recommendations-table", "t")


def test_heuristic_mode_rejects_recommendations_table():
    with pytest.raises(BatchArgumentError):
        _parse_args_for_mode(
            "single", "--exposure-source", "heuristic", "--recommendations-table", "t"
        )


def test_run_passes_factory_only_in_model_mode(monkeypatch):
    captured: dict = {}

    def _fake_daily(**kwargs):
        captured.update(kwargs)
        return {"status": "succeeded"}

    monkeypatch.setattr(action_log, "run_daily_action_log", _fake_daily)
    action_log._run(_parse_valid_single_args())  # 기본 model
    assert captured["candidate_provider_factory"] is not None

    captured.clear()
    action_log._run(_parse_valid_single_args("--exposure-source", "heuristic"))
    assert captured["candidate_provider_factory"] is None
```

(파서 헬퍼의 실제 이름·필수 인자 조합은 파일 내 기존 테스트를 따른다 — 단언 4종은 유지.)

- [ ] **Step 2: 실패 확인**

Run: `uv run python -m pytest tests/test_action_log_job.py tests/test_model_exposure_provider.py -v -k "exposure or resolve"`
Expected: FAIL — 인자 부재 / ImportError

- [ ] **Step 3: 구현**

`src/pipeline/model_exposure_provider.py`:

```python
def resolve_recommendations_table_id(table: str | None) -> str:
    """user_recommendations 대상 테이블의 정규화된 id를 만든다(기본값 단일 출처)."""
    import os

    from src.pipeline.build_training_dataset import BIGQUERY_DATASET, BIGQUERY_PROJECT

    resolved = table or os.environ.get(
        "CTR_TRAINING_BQ_RECOMMENDATIONS_TABLE", "user_recommendations"
    )
    return f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}.{resolved}"
```

`autoresearch/jobs/action_log.py` — ① parser에:

```python
    parser.add_argument("--exposure-source", choices=("model", "heuristic"))
    parser.add_argument("--recommendations-table")
```

② `_validate_args`: single·shard이면 `args.exposure_source = args.exposure_source or "model"`, heuristic인데 `--recommendations-table`이 있으면 `BatchArgumentError`; merge이면 `_reject(args, "exposure_source", "recommendations_table")`.

③ factory 구성(모듈 함수 — 테스트에서 monkeypatch 가능):

```python
def _build_candidate_provider_factory(args: argparse.Namespace):
    """model 모드에서만 src.pipeline을 지연 import해 노출 provider factory를 만든다."""
    if args.exposure_source != "model":
        return None

    def factory(videos: list[dict]):
        from google.cloud import bigquery

        from src.pipeline.build_training_dataset import BIGQUERY_PROJECT
        from src.pipeline.model_exposure_provider import (
            load_user_rankings,
            make_model_exposure_provider,
            resolve_recommendations_table_id,
        )

        table_id = resolve_recommendations_table_id(args.recommendations_table)
        client = bigquery.Client(project=BIGQUERY_PROJECT)
        rankings = load_user_rankings(client, table_id, args.partition_date)
        round_ = make_model_exposure_provider(
            rankings,
            videos,
            candidates_per_user=args.candidates_per_user,
            personalized_ratio=args.personalized_ratio,
            popular_ratio=args.popular_ratio,
            exploration_ratio=args.exploration_ratio,
        )
        return round_.provider, round_.metadata

    return factory
```

④ `_run`의 single·shard 분기에 `candidate_provider_factory=_build_candidate_provider_factory(args),` 추가.

⑤ 계약 문서: action_log 명령 섹션에 두 인자, 기본 `model`, BQ 의존(모델 모드), dt 정합, 파티션 부재 시 exit 1, `--exposure-source heuristic` 폴백 절차, Airflow 선행 의존(daily_recommendations) 인계 노트를 추가한다.

- [ ] **Step 4: 통과 확인**

Run: `uv run python -m pytest tests/test_action_log_job.py tests/test_model_exposure_provider.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add autoresearch/jobs/action_log.py src/pipeline/model_exposure_provider.py \
  docs/specs/2026-07-13-public-batch-execution-contract.md \
  tests/test_action_log_job.py tests/test_model_exposure_provider.py
git commit -m "feat: action_log CLI 모델 노출 기본 전환 및 계약 등재 (#222)"
```

---

### Task 5: 최종 검증

- [ ] **Step 1: 전체 스위트** — Run: `uv run python -m pytest -q` / Expected: 전부 PASS (기준: 609 passed + 신규)
- [ ] **Step 2: spec 대조** — `docs/specs/2026-07-22-daily-closed-loop.md`의 목표 3개·설계 결정 3개·에러 처리·인계 항목이 테스트로 커버되는지 확인, 어긋나면 문서 또는 코드 정정
- [ ] **Step 3: 계획 체크박스 갱신 후 Commit + Push**

```bash
git add docs/plans/2026-07-22-daily-closed-loop.md
git commit -m "docs: 일일 폐루프 계획 체크박스 갱신 (#222)"
git push origin feat/222-daily-closed-loop
```

## Self-Review 결과

- spec 목표 1(CLI 기본 전환)=Task 4, 목표 2(태그 운반)=Task 1·2·3, 목표 3(계약 문서)=Task 4 — 커버.
- 결정 1(draft 운반)=Task 1, 결정 2(factory seam·지연 import)=Task 3·4, 결정 3(cutover·폴백)=Task 4 — 커버.
- Task 3·4의 테스트 골격은 기존 픽스처·헬퍼 이름에 의존하므로 "파일 내 기존 패턴을 따른다"로 명시했고, 유지해야 할 단언을 별도로 고정했다 — 구현자가 이름만 치환하면 된다.
- 타입 일관성: `candidate_provider_factory` 시그니처가 Task 3(Produces)과 Task 4(Consumes)에서 동일함을 확인.
- 미커버(의도): exposure_source별 CTR 리포트, Airflow DAG 편성 — spec 비범위와 일치.
