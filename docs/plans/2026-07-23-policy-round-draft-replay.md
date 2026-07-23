# 정책 라운드 draft 덤프·리플레이 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `simulate_policy_round`가 LLM 판정을 draft parquet + 사이드카 메타로
남기고, 그 판정을 재사용해 **커트라인만 바꿔 LLM 0콜로 재실행**할 수 있게 만들어
`click_threshold` 캘리브레이션 폐루프를 닫는다.

**Architecture:** 비싸고 비결정적인 LLM 판정 단계와 싸고 결정적인 커트라인 적용
단계를 산출물 경계로 분리한다. 판정은 기존 공유 함수
`write_action_log_draft_parquet`으로 저장하고(공유 스키마 무변경), 계보(`llm_model`)와
노출 결정 인자는 사이드카 JSON에 둔다. 리플레이는 LLM만 건너뛰고 리랭킹·노출
선택은 결정적으로 재계산하며, 판정이 노출을 다 덮지 못하면 fail-fast한다.

**Tech Stack:** Python 3.12, pandas, pyarrow/parquet, pydantic v2, pytest, uv

**Spec:** `docs/specs/2026-07-23-policy-round-draft-replay.md`
**이슈:** #267 **브랜치:** `feat/267-policy-round-draft-replay` (이미 체크아웃됨)

## Global Constraints

- 응답·커밋 메시지·주석·docstring은 **한국어 격식체**를 사용한다 (CLAUDE.md).
- 커밋 형식은 `<type>: #267 <한국어 설명>` (`.claude/docs/agent-workflow-reference.md`).
- `autoresearch/action_logs/`의 공유 계약(`ACTION_LOG_DRAFT_PARQUET_SCHEMA`,
  event log 스키마)과 `daily.py`의 shard/merge 경로는 **변경하지 않는다.**
- Python 함수는 반환 타입을 포함한 타입 힌트를 유지한다.
- 기능을 바꾸는 커밋에서 모듈 최상단 docstring을 함께 갱신한다.
- 비리플레이(기존) 실행 경로의 동작은 바뀌지 않아야 한다 — 기존 테스트 8건이
  수정 없이 통과해야 한다.
- 검증 명령은 `uv run python -m pytest`이다.
- 시크릿·로컬 데이터 경로·생성 데이터 파일을 커밋하지 않는다. 산출물은
  `data/generated/`(gitignore) 아래에만 쓴다.

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `src/pipeline/simulate_policy_round.py` | 덤프·리플레이·fail-fast·CLI 인자 해석 | 수정 |
| `src/pipeline/report_html.py` | 리포트 HTML에 replay 계보 노출 | 수정 (footer 1곳) |
| `tests/test_simulate_policy_round.py` | 덤프·리플레이·fail-fast 테스트 | 수정 (추가) |
| `docs/specs/2026-07-23-policy-round-draft-replay.md` | 메타 필드 정정 | 수정 |

새 모듈은 만들지 않는다. 추가되는 심볼은 모두 `simulate_policy_round.py`
안에 있으며, 이 파일은 현재 405줄로 한 컨텍스트에 들어온다. 덤프·리플레이는
이 배치의 입출력 책임이라 같은 파일에 두는 것이 응집도에 맞는다.

---

### Task 1: draft 덤프 (parquet + 사이드카 메타)

**Files:**
- Modify: `src/pipeline/simulate_policy_round.py`
- Modify: `docs/specs/2026-07-23-policy-round-draft-replay.md`
- Test: `tests/test_simulate_policy_round.py`

**Interfaces:**
- Consumes: 없음 (첫 태스크)
- Produces:
  - 상수 `DRAFTS_FILENAME = "action_log_drafts.parquet"`,
    `DRAFTS_META_FILENAME = "action_log_drafts_meta.json"`
  - `main(..., input_paths: Mapping[str, str] | None = None)` — 계보용 입력 경로
  - 사이드카 JSON 키: `llm_model`, `prompt_version`, `schema_version`,
    `exposure_args{seed,k,exploration_ratio,as_of}`, `policy_version`,
    `virtual_users`(입력 유저 수), `users`(노출 성공 유저 수), `drafts`, `inputs`

**설계 메모 — 왜 `virtual_users`와 `users`를 둘 다 남기는가:** 리포트의 `users`는
persona 누락으로 건너뛴 유저를 뺀 수다. 리플레이에서 `--max-users` 누락을 잡으려면
**입력 유저 수**를 비교해야 하므로 두 값을 분리해 기록한다. spec의 메타 예시도 이에
맞춰 정정한다.

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/test_simulate_policy_round.py` 끝에 추가:

```python
def test_round_dumps_drafts_and_meta(tmp_path, stub_reranker):
    """LLM 판정이 draft parquet + 사이드카 메타로 남아야 한다(캘리브레이션 입력)."""
    import json

    from autoresearch.action_logs.pipeline import read_action_log_draft_parquet
    from autoresearch.action_logs.schema import (
        ACTION_LOG_SCHEMA_VERSION,
        PROMPT_VERSION,
    )

    main(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        click_threshold=0.0,
        seed=42,
        as_of="2026-07-20 00:00:00",
        policy_version="stub-run",
        output_dir=str(tmp_path),
        input_paths={"personas": "demo/personas.csv"},
    )

    drafts = read_action_log_draft_parquet(tmp_path / "action_log_drafts.parquet")
    assert drafts
    assert all(0.0 <= d.click_propensity <= 1.0 for d in drafts)

    meta = json.loads((tmp_path / "action_log_drafts_meta.json").read_text(encoding="utf-8"))
    assert meta["llm_model"] == "fixture-rule-action-log"
    assert meta["prompt_version"] == PROMPT_VERSION
    assert meta["schema_version"] == ACTION_LOG_SCHEMA_VERSION
    assert meta["exposure_args"] == {
        "seed": 42,
        "k": 6,
        "exploration_ratio": 0.0,
        "as_of": "2026-07-20 00:00:00",
    }
    assert meta["policy_version"] == "stub-run"
    assert meta["virtual_users"] == 4
    assert meta["users"] == 4
    assert meta["drafts"] == len(drafts)
    assert meta["inputs"] == {"personas": "demo/personas.csv"}
    # click_threshold는 리플레이에서 바꾸는 값이므로 노출 인자에 없어야 한다.
    assert "click_threshold" not in meta["exposure_args"]
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `uv run python -m pytest tests/test_simulate_policy_round.py::test_round_dumps_drafts_and_meta -v`
Expected: FAIL — `TypeError: main() got an unexpected keyword argument 'input_paths'`

- [ ] **Step 3: import와 상수를 추가한다**

`src/pipeline/simulate_policy_round.py`의 import 블록을 수정한다.

기존:
```python
from autoresearch.action_logs.pipeline import (
    ActionLogGenerator,
    ExposureMetadata,
    _expand_events,
    generate_action_log_drafts,
    select_clicks_per_slate,
    write_event_log_parquet,
    write_event_log_warehouse_jsonl,
    write_quarantine_jsonl,
)
```

변경 후:
```python
from autoresearch.action_logs.pipeline import (
    ActionLogGenerator,
    ExposureMetadata,
    _expand_events,
    generate_action_log_drafts,
    read_action_log_draft_parquet,
    select_clicks_per_slate,
    write_action_log_draft_parquet,
    write_event_log_parquet,
    write_event_log_warehouse_jsonl,
    write_quarantine_jsonl,
)
```

파일 상단 `import` 다음 줄에 추가 (`from __future__ import annotations` 아래
표준 라이브러리 블록):
```python
from collections.abc import Mapping
from dataclasses import dataclass
```

`BASELINE = "baseline"` / `MODEL = "model"` 아래에 추가:
```python
DRAFTS_FILENAME = "action_log_drafts.parquet"
DRAFTS_META_FILENAME = "action_log_drafts_meta.json"
```

- [ ] **Step 4: 메타 writer를 추가한다**

`_to_candidate_videos` 함수 정의 아래에 추가:

```python
def _write_drafts_meta(
    path: Path,
    *,
    llm_model: str,
    exposure_args: Mapping[str, object],
    policy_version: str,
    virtual_users: int,
    users: int,
    drafts: int,
    input_paths: Mapping[str, str] | None,
) -> None:
    """draft parquet 옆에 계보와 노출 결정 인자를 사이드카 JSON으로 남긴다.

    llm_model을 draft parquet 컬럼이 아니라 사이드카에 두는 이유는
    ACTION_LOG_DRAFT_PARQUET_SCHEMA가 daily.py shard/merge와 공유하는 계약이기
    때문이다. click_threshold는 리플레이에서 바꾸는 값이므로 exposure_args에
    넣지 않는다.
    """
    payload = {
        "llm_model": llm_model,
        "prompt_version": PROMPT_VERSION,
        "schema_version": ACTION_LOG_SCHEMA_VERSION,
        "exposure_args": dict(exposure_args),
        "policy_version": policy_version,
        "virtual_users": virtual_users,
        "users": users,
        "drafts": drafts,
        "inputs": dict(input_paths or {}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
```

- [ ] **Step 5: `main()`에 `input_paths`를 받고 덤프를 수행한다**

`main()` 시그니처의 `output_dir` 다음에 추가:
```python
    output_dir: str = "data/generated/policy_round",
    input_paths: Mapping[str, str] | None = None,
) -> dict:
```

`draft_by_key` 를 만드는 블록(현재 `simulate_policy_round.py:210` 부근) 바로
**앞**에 덤프를 삽입한다.

기존:
```python
    draft_by_key: dict[tuple[str, str], ImpressionDraft] = {
        (d.user_id, d.video_id): d for d in draft_result.drafts
    }
```

변경 후:
```python
    exposure_args = {
        "seed": seed,
        "k": k,
        "exploration_ratio": exploration_ratio,
        "as_of": as_of,
    }
    _write_drafts_meta(
        Path(output_dir) / DRAFTS_META_FILENAME,
        llm_model=generator.model_name,
        exposure_args=exposure_args,
        policy_version=policy_version,
        virtual_users=len(virtual_users),
        users=len(exposures_by_user),
        drafts=len(draft_result.drafts),
        input_paths=input_paths,
    )
    write_action_log_draft_parquet(
        draft_result.drafts, Path(output_dir) / DRAFTS_FILENAME
    )

    draft_by_key: dict[tuple[str, str], ImpressionDraft] = {
        (d.user_id, d.video_id): d for d in draft_result.drafts
    }
```

- [ ] **Step 6: 테스트가 통과하는지 확인한다**

Run: `uv run python -m pytest tests/test_simulate_policy_round.py -v`
Expected: PASS — 신규 1건 포함 전부 통과 (기존 8건 무수정)

- [ ] **Step 7: spec의 메타 예시를 정정한다**

`docs/specs/2026-07-23-policy-round-draft-replay.md`의 사이드카 JSON 예시에서
```json
  "users": 100,
  "drafts": 1483,
```
를 다음으로 바꾼다:
```json
  "virtual_users": 100,
  "users": 100,
  "drafts": 1483,
```

같은 문서의 "로드된 유저 수가 메타의 `users`와 다르면 에러" 문장을 다음으로
바꾼다:

> 로드된 유저 수가 메타의 `virtual_users`와 다르면 에러 — `--max-users`를
> 빠뜨린 경우를 잡는다. (`users`는 persona 누락으로 건너뛴 유저를 제외한 수라
> 입력 규모 비교에는 `virtual_users`를 쓴다.)

- [ ] **Step 8: 커밋한다**

```bash
git add src/pipeline/simulate_policy_round.py tests/test_simulate_policy_round.py docs/specs/2026-07-23-policy-round-draft-replay.md
git commit -m "feat: #267 정책 라운드가 LLM 판정을 draft parquet과 메타로 덤프합니다"
```

---

### Task 2: 리플레이 — 판정 재사용과 커버리지 fail-fast

**Files:**
- Modify: `src/pipeline/simulate_policy_round.py`
- Test: `tests/test_simulate_policy_round.py`

**Interfaces:**
- Consumes: Task 1의 `DRAFTS_FILENAME`, `DRAFTS_META_FILENAME`, `_write_drafts_meta`
- Produces:
  - `@dataclass(frozen=True) class DraftReplay` — 필드
    `drafts: list[ImpressionDraft]`, `llm_model: str`,
    `exposure_args: Mapping[str, object]`
  - `main(..., generator: ActionLogGenerator | None = None, *, replay: DraftReplay | None = None)`
    — `generator`와 `replay`는 정확히 하나만 지정 (아니면 `ValueError`)
  - 리플레이에서 판정이 노출을 다 덮지 못하면 `ValueError`

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/test_simulate_policy_round.py` 끝에 추가:

```python
def _run_round(tmp_path, stub_reranker, **overrides):
    """덤프까지 수행하는 표준 라운드 실행 헬퍼."""
    kwargs = dict(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        click_threshold=0.0,
        seed=42,
        as_of="2026-07-20 00:00:00",
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )
    kwargs.update(overrides)
    return main(**kwargs)


def _load_replay(round_dir):
    """덤프된 판정과 계보를 DraftReplay로 되살린다."""
    import json

    from autoresearch.action_logs.pipeline import read_action_log_draft_parquet
    from src.pipeline.simulate_policy_round import (
        DRAFTS_FILENAME,
        DRAFTS_META_FILENAME,
        DraftReplay,
    )

    meta = json.loads((round_dir / DRAFTS_META_FILENAME).read_text(encoding="utf-8"))
    return DraftReplay(
        drafts=read_action_log_draft_parquet(round_dir / DRAFTS_FILENAME),
        llm_model=str(meta["llm_model"]),
        exposure_args=meta["exposure_args"],
    )


def test_replay_reproduces_identical_round(tmp_path, stub_reranker):
    """같은 커트라인으로 리플레이하면 LLM 없이 동일한 결과가 나와야 한다."""
    first_dir = tmp_path / "a"
    original = _run_round(
        first_dir, stub_reranker, generator=RuleBasedActionLogGenerator()
    )

    replayed = _run_round(
        tmp_path / "b",
        stub_reranker,
        generator=None,
        replay=_load_replay(first_dir),
        output_dir=str(tmp_path / "b"),
    )

    assert replayed["policies"] == original["policies"]
    assert replayed["dropped_exposures_without_judgment"] == 0


def test_replay_with_higher_threshold_reduces_clicks(tmp_path, stub_reranker):
    """판정을 재사용한 채 커트라인만 올리면 클릭이 줄어야 한다(캘리브레이션 전제)."""
    first_dir = tmp_path / "a"
    original = _run_round(
        first_dir, stub_reranker, generator=RuleBasedActionLogGenerator()
    )

    strict = _run_round(
        tmp_path / "b",
        stub_reranker,
        generator=None,
        replay=_load_replay(first_dir),
        click_threshold=1.0,  # 어떤 propensity도 넘을 수 없는 커트라인
        output_dir=str(tmp_path / "b"),
    )

    assert original["policies"]["model"]["clicks"] >= 1
    assert strict["policies"]["model"]["clicks"] == 0
    assert strict["policies"]["baseline"]["clicks"] == 0


def test_replay_fails_when_drafts_do_not_cover_exposures(tmp_path, stub_reranker):
    """판정이 노출을 다 덮지 못하면 조용히 넘기지 않고 실패해야 한다."""
    first_dir = tmp_path / "a"
    _run_round(first_dir, stub_reranker, generator=RuleBasedActionLogGenerator())

    replay = _load_replay(first_dir)
    from src.pipeline.simulate_policy_round import DraftReplay

    truncated = DraftReplay(
        drafts=replay.drafts[:-1],
        llm_model=replay.llm_model,
        exposure_args=replay.exposure_args,
    )

    with pytest.raises(ValueError, match="cover"):
        _run_round(
            tmp_path / "b",
            stub_reranker,
            generator=None,
            replay=truncated,
            output_dir=str(tmp_path / "b"),
        )


def test_replay_event_log_keeps_original_llm_model(tmp_path, stub_reranker):
    """리플레이 event log의 계보는 원본 판정 모델이어야 한다."""
    import pyarrow.parquet as pq

    first_dir = tmp_path / "a"
    _run_round(
        first_dir,
        stub_reranker,
        generator=RuleBasedActionLogGenerator(model_name="judge-v9"),
    )

    second_dir = tmp_path / "b"
    _run_round(
        second_dir,
        stub_reranker,
        generator=None,
        replay=_load_replay(first_dir),
        output_dir=str(second_dir),
    )

    table = pq.read_table(second_dir / "event_log.parquet").to_pandas()
    assert set(table["llm_model"].unique()) == {"judge-v9"}


def test_main_requires_exactly_one_of_generator_or_replay(tmp_path, stub_reranker):
    with pytest.raises(ValueError, match="정확히 하나"):
        _run_round(tmp_path / "a", stub_reranker, generator=None)

    first_dir = tmp_path / "b"
    _run_round(first_dir, stub_reranker, generator=RuleBasedActionLogGenerator())
    with pytest.raises(ValueError, match="정확히 하나"):
        _run_round(
            tmp_path / "c",
            stub_reranker,
            generator=RuleBasedActionLogGenerator(),
            replay=_load_replay(first_dir),
            output_dir=str(tmp_path / "c"),
        )
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `uv run python -m pytest tests/test_simulate_policy_round.py -k replay -v`
Expected: FAIL — `ImportError: cannot import name 'DraftReplay'`

- [ ] **Step 3: `DraftReplay`를 추가한다**

`DRAFTS_META_FILENAME` 상수 아래에 추가:

```python
@dataclass(frozen=True)
class DraftReplay:
    """저장된 LLM 판정과 그 계보.

    판정과 계보는 항상 함께 다뤄야 하므로(계보 없는 event log를 쓰지 않는다)
    한 값으로 묶는다. exposure_args는 판정 라운드의 노출 결정 인자이며 CLI가
    인자 상속·불일치 검사에 사용한다.
    """

    drafts: list[ImpressionDraft]
    llm_model: str
    exposure_args: Mapping[str, object]
```

- [ ] **Step 4: `main()`을 리플레이 분기로 바꾼다**

시그니처를 변경한다.

기존:
```python
def main(
    personas: pd.DataFrame,
    virtual_users: list[dict],
    videos_raw: pd.DataFrame,
    events: pd.DataFrame,
    generator: ActionLogGenerator,
    reranker: Reranker | None = None,
    *,
```

변경 후:
```python
def main(
    personas: pd.DataFrame,
    virtual_users: list[dict],
    videos_raw: pd.DataFrame,
    events: pd.DataFrame,
    generator: ActionLogGenerator | None = None,
    reranker: Reranker | None = None,
    *,
    replay: DraftReplay | None = None,
```

docstring 바로 다음(`if reranker is None:` 앞)에 검증을 추가한다:

```python
    """정책 시뮬레이션 라운드를 실행하고 리포트 dict를 반환한다."""
    if (generator is None) == (replay is None):
        raise ValueError(
            "generator와 replay 중 정확히 하나만 지정해야 합니다 "
            "(replay는 저장된 판정을 재사용하므로 generator가 필요 없습니다)"
        )
    if reranker is None:
```

Task 1에서 만든 덤프 블록과 그 앞의 LLM 판정 블록(현재 `# 2) 유저별 합집합
후보로 LLM 판정 1회` 주석부터 `draft_by_key` 직전까지)을 다음으로 교체한다:

```python
    # 2) 판정 확보 — 신규 라운드는 LLM 1회, 리플레이는 저장된 판정 재사용
    request = EventGenerationRequest(
        click_threshold=click_threshold,
        candidates_per_user=max(1, 2 * k),
        seed=seed,
        chunk_size=chunk_size,
        max_concurrency=max_concurrency,
        output_path=str(Path(output_dir) / "event_log.parquet"),
        warehouse_output_path=str(Path(output_dir) / "event_log.jsonl"),
        quarantine_output_path=str(Path(output_dir) / "event_log_quarantine.jsonl"),
    )
    exposure_args = {
        "seed": seed,
        "k": k,
        "exploration_ratio": exploration_ratio,
        "as_of": as_of,
    }

    if replay is None:
        assert generator is not None  # 위 XOR 검증이 보장한다
        union_by_user: dict[str, list[dict]] = {}
        for user_id, both in exposures_by_user.items():
            seen: set[str] = set()
            union: list[dict] = []
            for exposure in both[MODEL] + both[BASELINE]:
                if exposure.video_id in seen:
                    continue
                seen.add(exposure.video_id)
                union.append(video_by_id[exposure.video_id])
            union_by_user[user_id] = union

        def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
            return union_by_user.get(str(virtual_user.get("user_id", "")), [])

        draft_result = generate_action_log_drafts(
            request, virtual_users, list(video_by_id.values()), generator,
            candidate_provider=provider,
        )
        drafts = draft_result.drafts
        quarantine = draft_result.quarantine
        llm_model = generator.model_name

        _write_drafts_meta(
            Path(output_dir) / DRAFTS_META_FILENAME,
            llm_model=llm_model,
            exposure_args=exposure_args,
            policy_version=policy_version,
            virtual_users=len(virtual_users),
            users=len(exposures_by_user),
            drafts=len(drafts),
            input_paths=input_paths,
        )
        write_action_log_draft_parquet(drafts, Path(output_dir) / DRAFTS_FILENAME)
    else:
        drafts = replay.drafts
        quarantine = []  # 이번 실행에서 새로 격리된 판정이 없다
        llm_model = replay.llm_model

    draft_by_key: dict[tuple[str, str], ImpressionDraft] = {
        (d.user_id, d.video_id): d for d in drafts
    }

    if replay is not None:
        missing = [
            (user_id, exposure.video_id)
            for user_id, both in exposures_by_user.items()
            for exposure in both[MODEL] + both[BASELINE]
            if (user_id, exposure.video_id) not in draft_by_key
        ]
        if missing:
            raise ValueError(
                f"replay drafts do not cover {len(missing)} exposure(s) "
                f"(first missing: {missing[0]}) — 노출 결정 인자나 유저 집합이 "
                "판정 라운드와 다를 수 있습니다"
            )
```

`request = EventGenerationRequest(...)` 를 위로 올렸으므로, 기존 `# 2)` 아래에
있던 원래 `request = EventGenerationRequest(...)` 정의는 **삭제한다**(중복 정의
금지).

- [ ] **Step 5: 나머지 `draft_result` 참조를 교체한다**

3단계 클릭 선정:
```python
    # 3) 합동 per-slate 선정 1회 → clicked (user, video) 키셋
    clicked_keys = {
        (drafts[i].user_id, drafts[i].video_id)
        for i in select_clicks_per_slate(drafts, click_threshold)
    }
```

6단계 저장:
```python
    write_event_log_parquet(batch, llm_model, output_path)
    write_event_log_warehouse_jsonl(batch, request.warehouse_output_path)
    write_quarantine_jsonl(quarantine, request.quarantine_output_path)
```

리포트의 `quarantined_chunks`:
```python
        "quarantined_chunks": len(quarantine),
```

- [ ] **Step 6: 테스트가 통과하는지 확인한다**

Run: `uv run python -m pytest tests/test_simulate_policy_round.py -v`
Expected: PASS — 기존 8건 + Task 1의 1건 + 신규 5건 전부 통과

- [ ] **Step 7: 커밋한다**

```bash
git add src/pipeline/simulate_policy_round.py tests/test_simulate_policy_round.py
git commit -m "feat: #267 저장된 판정을 재사용하는 리플레이 경로를 추가합니다"
```

---

### Task 3: CLI `--replay-drafts`와 인자 상속·불일치 fail-fast

**Files:**
- Modify: `src/pipeline/simulate_policy_round.py`
- Test: `tests/test_simulate_policy_round.py`

**Interfaces:**
- Consumes: Task 2의 `DraftReplay`, `main(replay=...)`
- Produces:
  - `DEFAULT_EXPOSURE_ARGS: dict[str, object]` = `{"seed": 42, "k": 10, "exploration_ratio": 0.1}`
  - `resolve_exposure_args(explicit, defaults, meta_exposure_args) -> dict[str, object]`
    — 미명시는 상속, 명시 불일치는 `ValueError`
  - `_read_drafts_meta(path: Path) -> dict` — 사이드카가 없으면 `FileNotFoundError`
  - CLI 플래그 `--replay-drafts <parquet>`

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/test_simulate_policy_round.py` 끝에 추가:

```python
def test_resolve_exposure_args_uses_defaults_without_meta():
    from src.pipeline.simulate_policy_round import resolve_exposure_args

    resolved = resolve_exposure_args(
        explicit={"seed": None, "k": 6, "exploration_ratio": None, "as_of": None},
        defaults={"seed": 42, "k": 10, "exploration_ratio": 0.1, "as_of": "now"},
        meta_exposure_args=None,
    )
    assert resolved == {"seed": 42, "k": 6, "exploration_ratio": 0.1, "as_of": "now"}


def test_resolve_exposure_args_inherits_meta_when_unspecified():
    from src.pipeline.simulate_policy_round import resolve_exposure_args

    meta = {"seed": 7, "k": 6, "exploration_ratio": 0.0, "as_of": "2026-07-20 00:00:00"}
    resolved = resolve_exposure_args(
        explicit={"seed": None, "k": None, "exploration_ratio": None, "as_of": None},
        defaults={"seed": 42, "k": 10, "exploration_ratio": 0.1, "as_of": "now"},
        meta_exposure_args=meta,
    )
    assert resolved == meta


def test_resolve_exposure_args_rejects_mismatch():
    from src.pipeline.simulate_policy_round import resolve_exposure_args

    meta = {"seed": 7, "k": 6, "exploration_ratio": 0.0, "as_of": "2026-07-20 00:00:00"}
    with pytest.raises(ValueError, match="seed"):
        resolve_exposure_args(
            explicit={"seed": 42, "k": None, "exploration_ratio": None, "as_of": None},
            defaults={"seed": 42, "k": 10, "exploration_ratio": 0.1, "as_of": "now"},
            meta_exposure_args=meta,
        )


def test_read_drafts_meta_requires_sidecar(tmp_path):
    from src.pipeline.simulate_policy_round import _read_drafts_meta

    with pytest.raises(FileNotFoundError, match="llm_model"):
        _read_drafts_meta(tmp_path / "action_log_drafts_meta.json")


def test_cli_replay_runs_without_generator(tmp_path, stub_reranker, monkeypatch):
    """CLI 리플레이는 --generator 없이 메타에서 인자를 상속해 동작해야 한다."""
    import json
    import sys

    import pyarrow as pa
    import pyarrow.parquet as pq

    from src.pipeline import simulate_policy_round as module

    # 입력 파일 준비
    personas_path = tmp_path / "personas.csv"
    _personas().to_csv(personas_path, index=False)
    videos_path = tmp_path / "videos.csv"
    _videos_raw().to_csv(videos_path, index=False)
    events_path = tmp_path / "events.csv"
    # 빈 프레임을 CSV로 왕복시키면 dtype이 전부 object로 추론돼 DuckDB의
    # user_id 비교가 깨진다. 실데이터와 같은 형태로 이력이 있는 프레임을 쓴다.
    _events_with_history().to_csv(events_path, index=False)
    users_path = tmp_path / "virtual_users.parquet"
    pq.write_table(pa.Table.from_pylist(_virtual_users()), users_path)

    monkeypatch.setattr(module, "load_reranker", lambda settings: stub_reranker)
    monkeypatch.setattr(module, "load_model_settings_from_environment", lambda: None)

    round_a = tmp_path / "round_a"
    argv = [
        "prog",
        "--personas", str(personas_path),
        "--virtual-users", str(users_path),
        "--videos", str(videos_path),
        "--events", str(events_path),
        "--generator", "rule-based",
        "--click-threshold", "0.0",
        "--k", "6",
        "--exploration-ratio", "0.0",
        "--as-of", "2026-07-20 00:00:00",
        "--output-dir", str(round_a),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    module._cli()

    meta = json.loads(
        (round_a / "action_log_drafts_meta.json").read_text(encoding="utf-8")
    )
    assert meta["exposure_args"]["k"] == 6

    # 리플레이 — k/seed/as-of/generator 모두 생략하고 메타에서 상속한다
    round_b = tmp_path / "round_b"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--personas", str(personas_path),
            "--virtual-users", str(users_path),
            "--videos", str(videos_path),
            "--events", str(events_path),
            "--replay-drafts", str(round_a / "action_log_drafts.parquet"),
            "--click-threshold", "0.0",
            "--output-dir", str(round_b),
        ],
    )
    module._cli()

    original = json.loads((round_a / "policy_round_report.json").read_text(encoding="utf-8"))
    replayed = json.loads((round_b / "policy_round_report.json").read_text(encoding="utf-8"))
    assert replayed["policies"] == original["policies"]


def test_cli_replay_rejects_generator_flag(tmp_path, monkeypatch):
    import sys

    from src.pipeline import simulate_policy_round as module

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--personas", "p.csv", "--virtual-users", "u.parquet",
            "--videos", "v.csv", "--events", "e.csv",
            "--replay-drafts", str(tmp_path / "action_log_drafts.parquet"),
            "--generator", "rule-based",
            "--click-threshold", "0.5",
        ],
    )
    with pytest.raises(SystemExit):
        module._cli()
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `uv run python -m pytest tests/test_simulate_policy_round.py -k "resolve_exposure or drafts_meta or cli_replay" -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_exposure_args'`

- [ ] **Step 3: 인자 해석기와 메타 reader를 추가한다**

`_write_drafts_meta` 아래에 추가:

```python
DEFAULT_EXPOSURE_ARGS: dict[str, object] = {"seed": 42, "k": 10, "exploration_ratio": 0.1}


def _read_drafts_meta(path: Path) -> dict:
    """draft 사이드카 메타를 읽는다.

    사이드카가 없으면 판정의 계보(llm_model)를 알 수 없고, 계보 없는 event log를
    쓰지 않는다는 규칙에 따라 실패한다.
    """
    if not path.is_file():
        raise FileNotFoundError(
            f"draft 사이드카 메타가 없습니다: {path} — "
            "계보(llm_model)를 알 수 없어 event log를 쓸 수 없습니다"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_exposure_args(
    explicit: Mapping[str, object | None],
    defaults: Mapping[str, object],
    meta_exposure_args: Mapping[str, object] | None,
) -> dict[str, object]:
    """노출 결정 인자를 확정한다.

    meta_exposure_args가 None(신규 라운드)이면 미명시 인자를 기본값으로 채운다.
    리플레이면 미명시 인자를 판정 라운드에서 상속하고, 명시한 인자가 판정
    라운드와 다르면 ValueError를 던진다 — 노출이 달라지면 저장된 판정이 노출을
    덮지 못하고, "같은 판정 분포에 커트라인을 적용한다"는 캘리브레이션 전제가
    깨지기 때문이다.
    """
    resolved: dict[str, object] = {}
    mismatches: list[str] = []
    for key, default in defaults.items():
        given = explicit.get(key)
        if meta_exposure_args is None:
            resolved[key] = default if given is None else given
            continue
        if key not in meta_exposure_args:
            raise ValueError(f"replay 메타에 노출 인자 '{key}'가 없습니다")
        inherited = meta_exposure_args[key]
        if given is None or given == inherited:
            resolved[key] = inherited
        else:
            mismatches.append(f"{key}: 지정={given!r}, 판정 라운드={inherited!r}")
    if mismatches:
        raise ValueError(
            "replay 인자가 판정 라운드와 다릅니다 — " + "; ".join(mismatches)
        )
    return resolved
```

- [ ] **Step 4: CLI를 수정한다**

`_cli()`의 인자 정의에서 기본값을 센티넬로 바꾸고 플래그를 추가한다.

기존:
```python
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--exploration-ratio", type=float, default=0.1)
    parser.add_argument("--click-threshold", type=float, required=True)
    parser.add_argument("--max-users", type=int, default=None)
    parser.add_argument("--seed", type=int, default=42)
```
변경 후:
```python
    parser.add_argument("--k", type=int, default=None, help="기본 10 (리플레이면 판정 라운드에서 상속)")
    parser.add_argument("--exploration-ratio", type=float, default=None, help="기본 0.1 (리플레이면 상속)")
    parser.add_argument("--click-threshold", type=float, required=True)
    parser.add_argument("--max-users", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None, help="기본 42 (리플레이면 상속)")
```

기존:
```python
    parser.add_argument("--generator", choices=["openrouter", "rule-based"], default="openrouter")
```
변경 후:
```python
    parser.add_argument(
        "--generator", choices=["openrouter", "rule-based"], default=None,
        help="기본 openrouter. --replay-drafts와 함께 쓸 수 없습니다",
    )
    parser.add_argument(
        "--replay-drafts", default=None,
        help="저장된 draft parquet 경로. 지정하면 LLM 호출 없이 커트라인만 다시 적용합니다",
    )
```

`args = parser.parse_args()` **바로 다음 줄**에 상호 배타 검사를 넣는다. 이
검사는 입력 파일 로드보다 **앞**에 있어야 한다 — 뒤에 두면 잘못된 조합이라도
파일 IO 오류가 먼저 터져 메시지가 묻힌다.

```python
    args = parser.parse_args()
    if args.replay_drafts is not None and args.generator is not None:
        parser.error("--generator는 --replay-drafts와 함께 쓸 수 없습니다 (저장된 판정을 재사용합니다)")
```

그 다음, 입력 로드 이후의 본문을 교체한다.

기존:
```python
    generator = (
        RuleBasedActionLogGenerator() if args.generator == "rule-based"
        else OpenRouterActionLogGenerator()
    )
    as_of = args.as_of or datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")

    report = main(
        personas=personas,
        virtual_users=virtual_users,
        videos_raw=videos_raw,
        events=events,
        generator=generator,
        k=args.k,
        exploration_ratio=args.exploration_ratio,
        click_threshold=args.click_threshold,
        seed=args.seed,
        chunk_size=args.chunk_size,
        max_concurrency=args.max_concurrency,
        policy_version=args.policy_version,
        as_of=as_of,
        output_dir=args.output_dir,
    )
```
변경 후:
```python
    replay = None
    generator = None
    meta_exposure_args = None
    if args.replay_drafts is not None:
        meta = _read_drafts_meta(Path(args.replay_drafts).with_name(DRAFTS_META_FILENAME))
        if len(virtual_users) != meta["virtual_users"]:
            parser.error(
                f"virtual user 수가 판정 라운드와 다릅니다 "
                f"(지정={len(virtual_users)}, 판정 라운드={meta['virtual_users']}) "
                "— --max-users를 확인하세요"
            )
        meta_exposure_args = meta["exposure_args"]
        replay = DraftReplay(
            drafts=read_action_log_draft_parquet(args.replay_drafts),
            llm_model=str(meta["llm_model"]),
            exposure_args=meta_exposure_args,
        )
    else:
        generator = (
            RuleBasedActionLogGenerator() if args.generator == "rule-based"
            else OpenRouterActionLogGenerator()
        )

    resolved = resolve_exposure_args(
        explicit={
            "seed": args.seed,
            "k": args.k,
            "exploration_ratio": args.exploration_ratio,
            "as_of": args.as_of,
        },
        defaults={
            **DEFAULT_EXPOSURE_ARGS,
            "as_of": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        },
        meta_exposure_args=meta_exposure_args,
    )

    report = main(
        personas=personas,
        virtual_users=virtual_users,
        videos_raw=videos_raw,
        events=events,
        generator=generator,
        replay=replay,
        k=int(resolved["k"]),
        exploration_ratio=float(resolved["exploration_ratio"]),
        click_threshold=args.click_threshold,
        seed=int(resolved["seed"]),
        chunk_size=args.chunk_size,
        max_concurrency=args.max_concurrency,
        policy_version=args.policy_version,
        as_of=str(resolved["as_of"]),
        output_dir=args.output_dir,
        input_paths={
            "personas": args.personas,
            "virtual_users": args.virtual_users,
            "videos": args.videos,
            "events": args.events,
        },
    )
```

`resolve_exposure_args`의 defaults에 `as_of`를 매번 넣는 이유는 `--as-of`
기본값이 "현재 시각"이라 리플레이에서 상속이 없으면 **매번** 불일치가 되기
때문이다.

- [ ] **Step 5: 테스트가 통과하는지 확인한다**

Run: `uv run python -m pytest tests/test_simulate_policy_round.py -v`
Expected: PASS — 전체 통과

- [ ] **Step 6: 커밋한다**

```bash
git add src/pipeline/simulate_policy_round.py tests/test_simulate_policy_round.py
git commit -m "feat: #267 CLI --replay-drafts와 노출 인자 상속·불일치 검사를 추가합니다"
```

---

### Task 4: 리포트 계보 노출과 문서 갱신

**Files:**
- Modify: `src/pipeline/simulate_policy_round.py`
- Modify: `src/pipeline/report_html.py`
- Test: `tests/test_simulate_policy_round.py`

**Interfaces:**
- Consumes: Task 2의 `replay`, `llm_model` 지역 변수
- Produces: 리포트 dict에 `replay: bool`, `llm_model: str` 키 추가

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/test_simulate_policy_round.py` 끝에 추가:

```python
def test_report_records_replay_provenance(tmp_path, stub_reranker):
    """산출물만 보고 원본 판정 라운드와 리플레이를 구분할 수 있어야 한다."""
    first_dir = tmp_path / "a"
    original = _run_round(
        first_dir,
        stub_reranker,
        generator=RuleBasedActionLogGenerator(model_name="judge-v9"),
    )
    assert original["replay"] is False
    assert original["llm_model"] == "judge-v9"

    second_dir = tmp_path / "b"
    replayed = _run_round(
        second_dir,
        stub_reranker,
        generator=None,
        replay=_load_replay(first_dir),
        output_dir=str(second_dir),
    )
    assert replayed["replay"] is True
    assert replayed["llm_model"] == "judge-v9"

    html = (second_dir / "policy_round_report.html").read_text(encoding="utf-8")
    assert "judge-v9" in html
    assert "replay" in html
```

- [ ] **Step 2: 테스트가 실패하는지 확인한다**

Run: `uv run python -m pytest tests/test_simulate_policy_round.py::test_report_records_replay_provenance -v`
Expected: FAIL — `KeyError: 'replay'`

- [ ] **Step 3: 리포트 dict에 계보를 추가한다**

`src/pipeline/simulate_policy_round.py`의 `report = {` 블록에서
`"policy_version": policy_version,` 다음 줄에 추가:

```python
        "replay": replay is not None,
        "llm_model": llm_model,
```

- [ ] **Step 4: HTML footer에 계보를 노출한다**

`src/pipeline/report_html.py`의 footer 문단을 수정한다.

기존:
```python
<p class="meta">exploration CTR (model): {escape(_pct(explo_ctr))} ·
skipped users: {len(report["skipped_users"])} ·
dropped exposures: {report["dropped_exposures_without_judgment"]} ·
quarantined chunks: {report["quarantined_chunks"]}</p></div>
```
변경 후:
```python
<p class="meta">exploration CTR (model): {escape(_pct(explo_ctr))} ·
skipped users: {len(report["skipped_users"])} ·
dropped exposures: {report["dropped_exposures_without_judgment"]} ·
quarantined chunks: {report["quarantined_chunks"]} ·
replay: {str(bool(report.get("replay", False))).lower()} ·
llm_model={escape(str(report.get("llm_model", "-")))}</p></div>
```

`.get()`을 쓰는 이유는 이 렌더러가 계보 키 없는 기존 리포트 dict(테스트 fixture
포함)도 그대로 렌더링해야 하기 때문이다.

- [ ] **Step 5: MLflow 파라미터에 replay를 남긴다**

`_cli()`의 `log_parameters({...})` 에서 `"round_type": "policy_simulation",`
다음 줄에 추가:

```python
                    "replay": report["replay"],
                    "llm_model": report["llm_model"],
```

- [ ] **Step 6: 모듈 docstring을 갱신한다**

`src/pipeline/simulate_policy_round.py` 최상단 docstring을 다음으로 교체한다:

```python
"""정책 시뮬레이션 라운드 배치.

baseline(키워드 휴리스틱) vs model(Reranker Top-K) 정책을 같은 유저·영상
pool에서 병행 노출하고, LLM 판정(합집합 1회)·합동 커트라인 판정을 거쳐 정책
태깅된 event log와 비교 리포트를 산출한다.

이 모듈이 담당하는 구간은 "노출 결정 → 판정 확보 → 커트라인 적용 → event log
산출"이다. 판정을 만드는 LLM 호출 규약과 클릭 선정 규칙 자체는
`autoresearch.action_logs`가 소유하며, 학습·평가와 GCS 적재는 담당하지 않는다.

제공 기능:

- 유저별 두 정책 노출 결정과 스코어링 진단 수집
- LLM 판정 1회 실행(합집합 후보)과 판정 덤프
  (`action_log_drafts.parquet` + 계보·노출 인자 사이드카
  `action_log_drafts_meta.json`) — `click_threshold` 캘리브레이션 입력
- 저장된 판정 리플레이(`--replay-drafts`) — LLM 호출 없이 커트라인만 다시
  적용하며, 판정이 노출을 다 덮지 못하면 fail-fast한다
- 정책별 event log(parquet/JSONL)·quarantine·비교 리포트(JSON/HTML) 산출

주의: 두 정책이 같은 (user, video)를 노출하면 동일 판정을 공유하되 이벤트
행은 정책별로 분리 생성된다. 재학습 등 downstream은 반드시 policy 컬럼으로
필터링해야 한다(정책 간 attribution 오염 방지).

spec: docs/specs/2026-07-20-policy-simulation-round.md,
      docs/specs/2026-07-23-policy-round-draft-replay.md
"""
```

- [ ] **Step 7: 전체 테스트를 실행한다**

Run: `uv run python -m pytest`
Expected: PASS — 저장소 전체 통과 (`report_html` 관련 기존 테스트 포함)

- [ ] **Step 8: 커밋한다**

```bash
git add src/pipeline/simulate_policy_round.py src/pipeline/report_html.py tests/test_simulate_policy_round.py
git commit -m "docs: #267 리포트에 replay 계보를 남기고 모듈 docstring을 갱신합니다"
```

---

### Task 5: 실제 캘리브레이션 실행 (100유저, 유료 LLM 1회)

**Files:**
- Modify: `docs/specs/2026-07-23-policy-round-draft-replay.md` — "실행 결과" 절 추가
- 산출물은 `data/generated/`(gitignore)에만 쓴다 — **커밋하지 않는다**

**Interfaces:**
- Consumes: Task 3의 CLI, `autoresearch.jobs.click_threshold_calibrate`
- Produces: 추천 커트라인과 그 값으로 재실행한 정책별 실측 CTR

> **비용 주의:** ①은 유저 100명 × 후보 ~15건의 실제 OpenRouter 호출입니다.
> 이 태스크만 유료이며, ②③은 LLM 0콜입니다.

- [ ] **Step 1: 판정 라운드를 실행한다 (유료)**

```bash
eval "$(grep '^export OPENROUTER_API_KEY=' ~/.bashrc)" && \
uv run --env-file .env --no-sync python -m src.pipeline.simulate_policy_round \
  --personas data/generated/demo_subset/personas.csv \
  --virtual-users data/generated/demo_subset/virtual_users.parquet \
  --videos data/generated/demo_subset/videos.csv \
  --events data/generated/demo_subset/events.csv \
  --generator openrouter --click-threshold 0.5 --max-users 100 \
  --output-dir data/generated/round_a
```

Expected: `data/generated/round_a/`에 `action_log_drafts.parquet`,
`action_log_drafts_meta.json`, `event_log.parquet`, `policy_round_report.json`이
생성되고, 리포트의 `users`가 100(또는 persona 누락분을 뺀 수)이다.

- [ ] **Step 2: 캘리브레이션을 실행한다 (LLM 0콜)**

```bash
uv run --no-sync python -m autoresearch.jobs.click_threshold_calibrate \
  --draft-path data/generated/round_a/action_log_drafts.parquet \
  --target-ctr 0.015
```

Expected: `{"status": "succeeded", "recommended_threshold": <값>, "achieved_ctr": ~0.015, ...}`

`{"status": "failed", "error_type": "ValueError"}`가 나오면 목표 CTR이 상한
(`users / impressions`)을 넘은 것이다. 출력의 `users`·`impressions`로 상한을
계산해 `--target-ctr`을 낮춘다.

- [ ] **Step 3: 추천 커트라인으로 리플레이한다 (LLM 0콜)**

```bash
uv run --env-file .env --no-sync python -m src.pipeline.simulate_policy_round \
  --personas data/generated/demo_subset/personas.csv \
  --virtual-users data/generated/demo_subset/virtual_users.parquet \
  --videos data/generated/demo_subset/videos.csv \
  --events data/generated/demo_subset/events.csv \
  --replay-drafts data/generated/round_a/action_log_drafts.parquet \
  --click-threshold <Step 2의 recommended_threshold> --max-users 100 \
  --output-dir data/generated/round_a_calibrated
```

Expected: 리포트의 `replay`가 `true`, `dropped_exposures_without_judgment`가 0,
`policies.baseline.ctr`과 `policies.model.ctr`이 출력된다.

- [ ] **Step 4: 결과를 spec에 기록한다**

`docs/specs/2026-07-23-policy-round-draft-replay.md` 끝에 "실행 결과 (2026-07-23)"
절을 추가하고 다음을 표로 적는다: 판정 라운드의 유저·노출·draft 수,
`--target-ctr`, `recommended_threshold`, `achieved_ctr`(합집합 기준),
리플레이 후 `policies.baseline.ctr` / `policies.model.ctr`, `overlap_jaccard_mean`.

정책별 실측 CTR이 목표와 크게 다르면 Step 2~3을 다른 `--target-ctr`로 반복하고,
반복 횟수와 최종 선택값도 함께 적는다.

- [ ] **Step 5: 커밋한다**

```bash
git add docs/specs/2026-07-23-policy-round-draft-replay.md
git commit -m "docs: #267 100유저 캘리브레이션 실행 결과를 기록합니다"
```

`git status`로 `data/generated/` 산출물이 스테이징되지 않았는지 확인한다.

---

### Task 6: PR 생성

- [ ] **Step 1: 전체 검증을 실행한다**

```bash
uv run python -m pytest
git diff --check
```
Expected: 전부 통과, `git diff --check` 출력 없음

- [ ] **Step 2: 푸시하고 PR을 연다**

```bash
git push -u origin feat/267-policy-round-draft-replay
gh pr create --base main --fill-first
```

PR 본문에 `Closes #267`과 Task 5의 실측 CTR 표를 포함한다.

---

## 검증 체크리스트

- [ ] 기존 테스트 8건이 수정 없이 통과한다 (비리플레이 경로 동작 무변경)
- [ ] 판정 라운드가 `action_log_drafts.parquet` + 사이드카 메타를 남긴다
- [ ] 같은 커트라인 리플레이가 원본과 동일한 `policies` 리포트를 만든다
- [ ] 커트라인을 올리면 클릭이 줄어든다 (판정 재사용 확인)
- [ ] draft 누락 시 `ValueError`로 실패한다
- [ ] `--seed` 불일치 명시 시 실패하고, 미명시는 메타에서 상속한다
- [ ] 사이드카 메타가 없으면 리플레이가 실패한다
- [ ] 리플레이 event log의 `llm_model`이 원본 판정 모델과 같다
- [ ] 리포트 JSON·HTML에 `replay`와 `llm_model`이 노출된다
- [ ] `data/generated/` 산출물이 커밋되지 않았다
