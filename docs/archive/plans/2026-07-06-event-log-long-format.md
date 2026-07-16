# Event Log wide→long 전환 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 기존 wide 포맷 `action_logs/`(한 row=한 impression에 `clicked`/`liked` 라벨 눌러담음)를, event_type 4종(`impression`/`click`/`view`/`like`)을 각각 별도 행으로 남기는 long 이벤트 스트림으로 전환한다.

**Architecture:** LLM은 (유저×후보영상)마다 `click_propensity`/`watch_fraction`/`would_like`만 판단하고, 코드가 (1) 전역 상위 2% impression을 "클릭"으로 선정한 뒤 (2) 노출마다 `impression` 1행을, 선정분엔 `click`+`view`(+`would_like`면 `like`) 행을 추가로 확장한다. 라벨(`clicked`)은 로그에 없고 downstream이 join으로 파생한다.

**Tech Stack:** Python 3.12(venv `arpy`), pydantic v2, pyarrow(parquet), pytest, ruff.

## Global Constraints

- 실행/테스트는 WSL venv `arpy`(3.12)로만: `source ~/…/arpy/bin/activate` 또는 `arpy/bin/python -m pytest …`. 시스템 python3(3.10)은 프로젝트 실행 불가.
- SSOT: `docs/guides/agent-simulator-spec.md`. 설계 근거: `docs/archive/specs/2026-07-06-event-log-long-format-design.md`.
- MVP 8컬럼만: `event_id, event_timestamp, user_id, event_type, video_id, watch_time_sec, rank(항상 null), source(historical)`. `session_id/request_id/exposure_type/query/search event`는 Phase 2 보류(구현 금지).
- `rank`는 Phase 1에서 **항상 null**. `source`는 **항상 `historical`**.
- 코드가 정확한 클릭 비율을 결정(LLM은 비율을 정하지 않음). 전역 CTR = `round(target_ctr × 총 impression 수)`.
- 유저 단위 장애 격리 + quarantine(`api_error`/`invalid_json`/`schema_fail`) + 전량실패 가드(`max_quarantine_ratio`)는 **그대로 유지**한다.
- 각 Task는 venv에서 `ruff check autoresearch tests`가 깨끗해야 완료. 커밋은 Task 단위.

---

### Task 1: EventLog long 스키마 + Draft/summary 전환 (`schema.py`)

`clicked`/`liked`/`search_keyword`/`exposure_type` 컬럼과 `enforce_no_click_constraints`를 제거하고, `event_type` 단일 컬럼(값 4종)과 "view만 watch_time_sec 보유" 검증기로 교체한다. `EventLogBatch.summary`는 impression/click 행 수로 CTR을 계산한다.

**Files:**
- Modify: `autoresearch/action_logs/schema.py`
- Test: `tests/test_action_logs_pipeline.py` (schema 단위 테스트만 이 Task에서 교체)

**Interfaces:**
- Produces:
  - `EventLog(event_id: str, event_timestamp: datetime, user_id: str, event_type: Literal["impression","click","view","like"], video_id: str, watch_time_sec: int | None = None, rank: int | None = None, source: Literal["historical","online_simulated"] = "historical")`. 검증: `view`면 `watch_time_sec` non-null(≥0), 그 외 event_type이면 `watch_time_sec is None`.
  - `EventLog.to_warehouse_row() -> dict` (8 도메인 컬럼).
  - `ImpressionDraft(user_id: str, video_id: str, click_propensity: float, watch_fraction: float, would_like: bool, duration_sec: int)` — `search_keyword`/`exposure_type` 제거.
  - `EventLogBatch.summary -> dict` 키: `total_events, impressions, clicks, ctr`.
  - 상수 `SOURCE_HISTORICAL`, `ACTION_LOG_SCHEMA_VERSION`, `PROMPT_VERSION` 유지. `EXPOSURE_TOP_RANKED`/`EXPOSURE_EXPLORATION` **삭제**.

- [ ] **Step 1: schema 단위 테스트를 long 포맷으로 교체 (failing)**

`tests/test_action_logs_pipeline.py`에서 기존 `test_eventlog_rejects_click0_with_watch_or_like`(144–148행)를 아래 두 테스트로 **교체**한다. 파일 상단 import는 그대로 (`from autoresearch.action_logs.schema import EventGenerationRequest, EventLog`), 필요한 심볼 추가는 Step에서 안내.

```python
def test_eventlog_watch_time_only_for_view():
    now = datetime(2026, 7, 1, tzinfo=UTC)
    # view는 watch_time_sec 필수(>=0)
    ev = EventLog(event_id="e", event_timestamp=now, user_id="u",
                  event_type="view", video_id="v", watch_time_sec=42)
    assert ev.watch_time_sec == 42 and ev.rank is None and ev.source == "historical"
    # impression/click/like는 watch_time_sec=None (기본값)
    for et in ("impression", "click", "like"):
        assert EventLog(event_id="e", event_timestamp=now, user_id="u",
                        event_type=et, video_id="v").watch_time_sec is None
    # view인데 watch_time_sec 누락 -> 거부
    with pytest.raises(ValidationError):
        EventLog(event_id="e", event_timestamp=now, user_id="u",
                 event_type="view", video_id="v")
    # 비-view인데 watch_time_sec 채움 -> 거부
    with pytest.raises(ValidationError):
        EventLog(event_id="e", event_timestamp=now, user_id="u",
                 event_type="impression", video_id="v", watch_time_sec=5)


def test_batch_summary_ctr_from_impression_and_click_rows():
    from autoresearch.action_logs.schema import EventLogBatch
    now = datetime(2026, 7, 1, tzinfo=UTC)

    def _ev(et, wt=None):
        return EventLog(event_id="e", event_timestamp=now, user_id="u",
                        event_type=et, video_id="v", watch_time_sec=wt)

    events = [_ev("impression"), _ev("impression"), _ev("click"), _ev("view", 10), _ev("like")]
    batch = EventLogBatch(
        schema_version="s", prompt_version="p",
        request=EventGenerationRequest(), events=events,
    )
    s = batch.summary
    assert s["impressions"] == 2 and s["clicks"] == 1
    assert s["total_events"] == 5
    assert s["ctr"] == round(1 / 2, 4)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `arpy/bin/python -m pytest tests/test_action_logs_pipeline.py::test_eventlog_watch_time_only_for_view tests/test_action_logs_pipeline.py::test_batch_summary_ctr_from_impression_and_click_rows -v`
Expected: FAIL — 현재 `EventLog`는 `event_type`을 모르고 `clicked` 필수라 `TypeError`/`ValidationError`.

- [ ] **Step 3: `schema.py`의 `EventLog`를 long 포맷으로 교체**

`EXPOSURE_TOP_RANKED`/`EXPOSURE_EXPLORATION` 상수(19–21행)를 삭제하고, `EventLog` 클래스(24–74행) 전체를 아래로 교체한다.

```python
class EventLog(BaseModel):
    """한 row = 한 이벤트. event_type ∈ {impression, click, view, like}.

    라벨(clicked)은 저장하지 않는다. clicked는 downstream이 impression↔click join으로
    파생한다. 노출마다 impression 1행, 클릭 선정분엔 click/view(+like) 행이 추가된다.
    설계: docs/archive/specs/2026-07-06-event-log-long-format-design.md
    """

    event_id: str
    event_timestamp: datetime
    user_id: str
    event_type: Literal["impression", "click", "view", "like"]
    video_id: str
    watch_time_sec: int | None = None
    rank: int | None = None
    source: Literal["historical", "online_simulated"] = SOURCE_HISTORICAL

    @model_validator(mode="after")
    def watch_time_only_for_view(self) -> "EventLog":
        """watch_time_sec는 view 이벤트일 때만 non-null(>=0), 그 외엔 null이어야 한다."""

        if self.event_type == "view":
            if self.watch_time_sec is None or self.watch_time_sec < 0:
                raise ValueError("view event requires watch_time_sec >= 0")
        elif self.watch_time_sec is not None:
            raise ValueError(f"{self.event_type} event must have watch_time_sec=None")
        return self

    def to_warehouse_row(self) -> dict[str, object]:
        """Data Warehouse 적재용 flat row(타임스탬프는 ISO 문자열)."""

        return {
            "event_id": self.event_id,
            "event_timestamp": self.event_timestamp.isoformat(),
            "user_id": self.user_id,
            "event_type": self.event_type,
            "video_id": self.video_id,
            "watch_time_sec": self.watch_time_sec,
            "rank": self.rank,
            "source": self.source,
        }
```

- [ ] **Step 4: `ImpressionDraft`에서 search_keyword·exposure_type 제거**

`ImpressionDraft`(원본 77–87행)를 아래로 교체한다.

```python
class ImpressionDraft(BaseModel):
    """LLM 판단 결과(전역 2% 정규화 전 중간 산출물). 저장되지 않는다.

    draft 1건 = 후보(노출) 1건 = impression 1행에 대응한다.
    """

    user_id: str
    video_id: str
    click_propensity: float = Field(ge=0.0, le=1.0)
    watch_fraction: float = Field(ge=0.0, le=1.0)
    would_like: bool
    duration_sec: int = Field(ge=1)
```

- [ ] **Step 5: `EventLogBatch.summary`를 impression/click 행 기준으로 교체**

`summary` property(원본 147–157행)를 아래로 교체한다.

```python
    @property
    def summary(self) -> dict[str, float]:
        """총 event 수, impression/click 행 수, 전역 CTR(clicks/impressions)을 계산한다."""

        impressions = sum(1 for e in self.events if e.event_type == "impression")
        clicks = sum(1 for e in self.events if e.event_type == "click")
        return {
            "total_events": len(self.events),
            "impressions": impressions,
            "clicks": clicks,
            "ctr": round(clicks / impressions, 4) if impressions else 0.0,
        }
```

- [ ] **Step 6: 테스트 통과 + ruff 확인**

Run: `arpy/bin/python -m pytest tests/test_action_logs_pipeline.py::test_eventlog_watch_time_only_for_view tests/test_action_logs_pipeline.py::test_batch_summary_ctr_from_impression_and_click_rows -v && arpy/bin/python -m ruff check autoresearch/action_logs/schema.py`
Expected: 2 passed, ruff clean.

- [ ] **Step 7: Commit**

```bash
git add autoresearch/action_logs/schema.py tests/test_action_logs_pipeline.py
git commit -m "refactor: EventLog wide→long 스키마(event_type 4종, view만 watch_time) 전환 (이슈 #57)"
```

---

### Task 2: candidate가 exposure_type 라벨 없이 영상 목록만 반환 (`candidate.py`)

로그에 `exposure_type`을 남기지 않으므로 후보 구성의 반환 형태를 `list[tuple[dict, str]]`에서 `list[dict]`로 단순화한다. 관련/exploration 혼합 로직(80/20)은 후보 구성 다양성을 위해 그대로 두되, 라벨만 제거한다.

**Files:**
- Modify: `autoresearch/action_logs/candidate.py`
- Test: `tests/test_action_logs_pipeline.py` (candidate 단위 테스트 추가)

**Interfaces:**
- Consumes: 없음(Task 1 상수 삭제 반영).
- Produces: `build_candidates(virtual_user: dict, videos: list[dict], candidates_per_user: int, exploration_ratio: float, rng: random.Random) -> list[dict]` — 노출 대상 video dict 목록(길이 `min(candidates_per_user, len(videos))`, `video_id` 중복 없음). `_user_keywords`/`_video_text`/`_relevance_score` 시그니처 불변(llm_generator가 재사용).

- [ ] **Step 1: candidate 테스트 추가 (failing)**

`tests/test_action_logs_pipeline.py` 하단(마지막 테스트 뒤)에 추가. 파일 상단에 import 추가: `from autoresearch.action_logs.candidate import build_candidates` 와 `import random`(이미 없으면).

```python
def test_build_candidates_returns_video_dicts_no_exposure_label():
    users = _fixture_users(1)
    videos = build_fixture_video_records(40)
    got = build_candidates(users[0], videos, candidates_per_user=20,
                           exploration_ratio=0.2, rng=random.Random(1))
    assert len(got) == 20
    assert all(isinstance(v, dict) and "video_id" in v for v in got)  # tuple 아님
    assert len({v["video_id"] for v in got}) == 20  # dedup
    # pool보다 큰 요청은 pool 크기로 클램프
    assert len(build_candidates(users[0], videos[:5], 20, 0.2, random.Random(1))) == 5
    assert build_candidates(users[0], [], 20, 0.2, random.Random(1)) == []
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `arpy/bin/python -m pytest "tests/test_action_logs_pipeline.py::test_build_candidates_returns_video_dicts_no_exposure_label" -v`
Expected: FAIL — 현재 `build_candidates`가 `(dict, str)` 튜플 목록을 반환해 `"video_id" in v` 가 False.

- [ ] **Step 3: `build_candidates`를 list[dict] 반환으로 교체**

`candidate.py`의 import 블록(10–13행)에서 `EXPOSURE_EXPLORATION`/`EXPOSURE_TOP_RANKED` import를 삭제한다(Task 1에서 상수 제거됨). 그리고 `build_candidates`(56–104행)를 아래로 교체한다.

```python
def build_candidates(
    virtual_user: dict,
    videos: list[dict],
    candidates_per_user: int,
    exploration_ratio: float,
    rng: random.Random,
) -> list[dict]:
    """유저 1명의 노출 batch를 video dict 목록으로 구성한다.

    관련 후보(키워드 겹침 상위) + exploration 랜덤을 섞되, exposure_type 라벨은
    로그에 남기지 않으므로 반환하지 않는다. pool이 요청 수보다 작으면 가능한 만큼만.
    """
    if not videos:
        return []

    n_total = min(candidates_per_user, len(videos))
    n_explore = min(round(n_total * exploration_ratio), n_total)
    n_relevant = n_total - n_explore

    keywords = _user_keywords(virtual_user)
    scored = [
        (
            _relevance_score(keywords, _video_text(v)),
            int(v.get("view_count", 0) or 0),
            idx,
            v,
        )
        for idx, v in enumerate(videos)
    ]
    # 관련도 desc, 동점은 조회수 desc, 그다음 idx로 안정 정렬.
    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))

    relevant = [t[3] for t in scored[:n_relevant]]
    remaining = [t[3] for t in scored[n_relevant:]]
    rng.shuffle(remaining)
    exploration = remaining[:n_explore]

    candidates = relevant + exploration
    rng.shuffle(candidates)

    logger.debug(
        "Built candidates",
        extra={
            "user_id": virtual_user.get("user_id"),
            "n_relevant": len(relevant),
            "n_exploration": len(exploration),
        },
    )
    return candidates
```

- [ ] **Step 4: 테스트 통과 + ruff 확인**

Run: `arpy/bin/python -m pytest "tests/test_action_logs_pipeline.py::test_build_candidates_returns_video_dicts_no_exposure_label" -v && arpy/bin/python -m ruff check autoresearch/action_logs/candidate.py`
Expected: 1 passed, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add autoresearch/action_logs/candidate.py tests/test_action_logs_pipeline.py
git commit -m "refactor: candidate가 exposure_type 라벨 없이 영상 목록만 반환 (이슈 #57)"
```

---

### Task 3: LLM generator에서 search_keyword 제거 (`llm_generator.py`)

MVP는 search를 다루지 않으므로 system harness·prompt 출력 스펙·RuleBased fixture에서 `search_keyword`를 제거한다. `click_propensity`/`watch_fraction`/`would_like`는 유지.

**Files:**
- Modify: `autoresearch/action_logs/llm_generator.py`
- Test: `tests/test_action_logs_pipeline.py` (RuleBased 출력 형태 테스트 추가)

**Interfaces:**
- Consumes: `_user_keywords`/`_video_text`/`_relevance_score`(candidate, 불변).
- Produces: `RuleBasedActionLogGenerator().generate(vu, videos) -> str` — `{"judgments":[{"video_id","click_propensity","watch_fraction","would_like"}, …]}` (search_keyword 키 없음). `model_name` 속성 유지. `OpenRouterActionLogGenerator`/`build_action_log_prompt` 시그니처 불변.

- [ ] **Step 1: RuleBased 출력 형태 테스트 추가 (failing)**

`tests/test_action_logs_pipeline.py` 하단에 추가.

```python
def test_rulebased_judgments_have_no_search_keyword():
    users = _fixture_users(1)
    videos = build_fixture_video_records(6)
    raw = RuleBasedActionLogGenerator().generate(users[0], videos)
    data = json.loads(raw)
    assert len(data["judgments"]) == 6
    for j in data["judgments"]:
        assert set(j) == {"video_id", "click_propensity", "watch_fraction", "would_like"}
        assert 0.0 <= j["click_propensity"] <= 1.0
        assert isinstance(j["would_like"], bool)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `arpy/bin/python -m pytest "tests/test_action_logs_pipeline.py::test_rulebased_judgments_have_no_search_keyword" -v`
Expected: FAIL — 현재 judgment에 `search_keyword` 키가 있어 `set(j) == {...}` 불일치.

- [ ] **Step 3: system harness에서 search_keyword 줄 제거**

`ACTION_LOG_SYSTEM_HARNESS`(24–33행)에서 아래 한 줄을 삭제한다.

```python
- search_keyword: 이 영상에 도달하게 한 사용자 관심 키워드 하나(없으면 null).
```

- [ ] **Step 4: prompt 출력 스펙에서 search_keyword 제거**

`build_action_log_prompt`(73–93행)의 JSON 예시와 제약 줄을 교체한다. 예시 객체를

```python
  {{"video_id": "...", "click_propensity": 0.0, "watch_fraction": 0.0, "would_like": false}}
```

로 바꾸고, 제약의 마지막 줄

```python
- would_like 은 true/false, search_keyword 는 문자열 또는 null.
```

을

```python
- would_like 은 true/false.
```

로 바꾼다.

- [ ] **Step 5: RuleBased judgment에서 search_keyword 제거**

`RuleBasedActionLogGenerator.generate`(102–120행)의 judgment append(111–119행)를 아래로 교체한다.

```python
            judgments.append(
                {
                    "video_id": v["video_id"],
                    "click_propensity": round(propensity, 3),
                    "watch_fraction": round(min(1.0, propensity + 0.1), 3),
                    "would_like": propensity > 0.7,
                }
            )
```

- [ ] **Step 6: 테스트 통과 + ruff 확인**

Run: `arpy/bin/python -m pytest "tests/test_action_logs_pipeline.py::test_rulebased_judgments_have_no_search_keyword" -v && arpy/bin/python -m ruff check autoresearch/action_logs/llm_generator.py`
Expected: 1 passed, ruff clean.

- [ ] **Step 7: Commit**

```bash
git add autoresearch/action_logs/llm_generator.py tests/test_action_logs_pipeline.py
git commit -m "refactor: LLM generator에서 search_keyword 제거(MVP는 search 미지원) (이슈 #57)"
```

---

### Task 4: 파이프라인 이벤트 확장(_expand_events) + long parquet 스키마 (`pipeline.py`)

`_build_user_drafts`를 list[dict] 후보에 맞추고, `_assemble_events`를 `_expand_events`로 교체한다(노출마다 impression 1행, 클릭 선정분엔 click/view/like 확장, timestamp 단조 증가). parquet 스키마·`_event_rows`를 long 컬럼으로 갱신하고 통합 테스트를 재작성한다.

**Files:**
- Modify: `autoresearch/action_logs/pipeline.py`
- Test: `tests/test_action_logs_pipeline.py` (통합 테스트 전면 재작성)

**Interfaces:**
- Consumes: `EventLog`/`ImpressionDraft`/`EventLogBatch`(Task 1), `build_candidates -> list[dict]`(Task 2), `RuleBasedActionLogGenerator`(Task 3), `nominal_duration_sec`(video_source, 불변).
- Produces: `generate_action_log_batch(request, virtual_users, videos, generator) -> EventGenerationResult`(시그니처 불변). `EVENT_LOG_PARQUET_SCHEMA`(long 컬럼). `_expand_events(drafts, clicked, request) -> list[EventLog]`. `ActionLogGenerationError` 유지.

- [ ] **Step 1: `EVENT_LOG_PARQUET_SCHEMA`를 long 컬럼으로 교체**

`pipeline.py`의 `EVENT_LOG_PARQUET_SCHEMA`(51–69행)를 아래로 교체한다. pyarrow 필드는 기본 nullable이라 `watch_time_sec`/`rank`의 None을 담는다.

```python
EVENT_LOG_PARQUET_SCHEMA = pa.schema(
    [
        pa.field("event_id", pa.string()),
        pa.field("event_timestamp", pa.timestamp("us", tz="UTC")),
        pa.field("user_id", pa.string()),
        pa.field("event_type", pa.string()),
        pa.field("video_id", pa.string()),
        pa.field("watch_time_sec", pa.int64()),
        pa.field("rank", pa.int64()),
        pa.field("source", pa.string()),
        pa.field("schema_version", pa.string()),
        pa.field("prompt_version", pa.string()),
        pa.field("llm_model", pa.string()),
        pa.field("generated_at", pa.string()),
    ]
)
```

- [ ] **Step 2: `_build_user_drafts`를 list[dict] 후보에 맞게 교체**

`_build_user_drafts`(78–117행)를 아래로 교체한다(exposure/search_keyword 제거, `candidates: list[dict]`).

```python
def _build_user_drafts(
    virtual_user: dict,
    candidates: list[dict],
    raw_text: str,
) -> list[ImpressionDraft]:
    """LLM raw judgments를 파싱해 후보별 ImpressionDraft를 만든다.

    json.JSONDecodeError -> invalid_json. 구조/타입 오류(ValueError/KeyError/TypeError/
    AttributeError/ValidationError) -> schema_fail. 판단이 누락된 후보는 비클릭 노출로 채운다.
    """
    data = json.loads(raw_text)  # invalid_json
    judgments = data["judgments"]  # KeyError/TypeError
    jmap = {str(j["video_id"]): j for j in judgments}

    user_id = str(virtual_user.get("user_id", ""))
    drafts: list[ImpressionDraft] = []
    for video in candidates:
        vid = video["video_id"]
        j = jmap.get(vid)
        if j is None:
            prop, frac, like = 0.0, 0.0, False
        else:
            prop = _clamp01(j.get("click_propensity", 0.0))
            frac = _clamp01(j.get("watch_fraction", 0.0))
            like = bool(j.get("would_like", False))
        drafts.append(
            ImpressionDraft(
                user_id=user_id,
                video_id=vid,
                click_propensity=prop,
                watch_fraction=frac,
                would_like=like,
                duration_sec=nominal_duration_sec(vid),
            )
        )
    return drafts
```

- [ ] **Step 3: `_generate_drafts_isolated`의 후보 사용부 수정**

`_generate_drafts_isolated`(120–178행) 내부에서 후보를 영상 목록으로 다루도록 두 줄을 고친다.
- `videos_only = [v for v, _ in candidates]` (142행) → `videos_only = candidates`
- `drafts.extend(_build_user_drafts(virtual_user, candidates, raw_text))` (157행)는 그대로 두되, 인자 `candidates`가 이제 list[dict]임을 확인한다(변경 없음).

- [ ] **Step 4: `_assemble_events`를 `_expand_events`로 교체**

`_assemble_events`(193–249행) 전체를 아래 `_expand_events`로 교체한다. 노출마다 impression 1행을 만들고, 클릭 선정분엔 impression 직후 시각에 click→view(→would_like면 like)를 단조 증가로 붙인다. 일일 상한은 **impression 기준**. impression에 최소 1시간 여유(hours 1~23)를 둬 후속 이벤트가 `history_end`를 넘지 않게 한다.

```python
def _expand_events(
    drafts: list[ImpressionDraft],
    clicked: set[int],
    request: EventGenerationRequest,
) -> list[EventLog]:
    """draft + 클릭 결정 → long EventLog 스트림.

    노출마다 impression 1행. 클릭 선정분엔 같은 세션 흐름으로 click/view(+like)를
    impression 직후(초 단위 단조 증가)에 배치한다. 일일 상한은 impression 기준.
    """
    end = request.history_end
    if end.tzinfo is None:
        end = end.replace(tzinfo=UTC)

    by_user: dict[str, list[int]] = defaultdict(list)
    for idx, draft in enumerate(drafts):
        by_user[draft.user_id].append(idx)

    events: list[EventLog] = []
    seq = 0

    def _emit(timestamp, user_id, event_type, video_id, watch=None):
        nonlocal seq
        events.append(
            EventLog(
                event_id=f"evt_{seq:08d}",
                event_timestamp=timestamp,
                user_id=user_id,
                event_type=event_type,
                video_id=video_id,
                watch_time_sec=watch,
                rank=None,
                source=SOURCE_HISTORICAL,
            )
        )
        seq += 1

    for user_id, indices in by_user.items():
        urng = random.Random(f"{request.seed}:ts:{user_id}")
        days = list(range(request.history_days))
        urng.shuffle(days)
        order = list(indices)
        urng.shuffle(order)
        cap = request.max_events_per_user_per_day
        for position, idx in enumerate(order):
            draft = drafts[idx]
            day = days[(position // cap) % len(days)]
            impression_ts = end - timedelta(
                days=day,
                hours=urng.randint(1, 23),  # 1h+ 여유로 후속 이벤트가 end를 넘지 않게
                minutes=urng.randint(0, 59),
                seconds=urng.randint(0, 59),
            )
            _emit(impression_ts, user_id, "impression", draft.video_id)
            if idx not in clicked:
                continue
            click_ts = impression_ts + timedelta(seconds=urng.randint(1, 30))
            _emit(click_ts, user_id, "click", draft.video_id)
            watch = max(1, round(draft.watch_fraction * draft.duration_sec))
            view_ts = click_ts + timedelta(seconds=urng.randint(1, 5))
            _emit(view_ts, user_id, "view", draft.video_id, watch=watch)
            if draft.would_like:
                like_ts = view_ts + timedelta(seconds=urng.randint(1, max(2, watch)))
                _emit(like_ts, user_id, "like", draft.video_id)
    return events
```

- [ ] **Step 5: `_event_rows`를 long 컬럼으로 교체**

`_event_rows`(252–276행)의 row dict(258–274행)를 아래로 교체한다.

```python
        rows.append(
            {
                "event_id": event.event_id,
                "event_timestamp": event.event_timestamp,
                "user_id": event.user_id,
                "event_type": event.event_type,
                "video_id": event.video_id,
                "watch_time_sec": event.watch_time_sec,
                "rank": event.rank,
                "source": event.source,
                "schema_version": batch.schema_version,
                "prompt_version": batch.prompt_version,
                "llm_model": model_name,
                "generated_at": batch.generated_at,
            }
        )
```

- [ ] **Step 6: `generate_action_log_batch`의 조립 호출명 교체**

`generate_action_log_batch`(308–362행) 내 `events = _assemble_events(drafts, clicked, request)`(329행)를 `events = _expand_events(drafts, clicked, request)`로 바꾼다.

- [ ] **Step 7: 통합 테스트 전면 재작성 (failing)**

`tests/test_action_logs_pipeline.py`의 통합 테스트(67–139행: `test_end_to_end_*`, `test_timestamps_*`, `test_per_user_daily_cap_*`, `test_parquet_*`, `test_user_isolation_*`, `test_total_failure_*`)를 아래로 **교체**한다. `_fixture_users`/`_request` 헬퍼와 다른 Task에서 추가한 단위 테스트는 유지한다.

```python
def test_end_to_end_long_event_stream(tmp_path):
    users, videos = _fixture_users(6), build_fixture_video_records(40)
    result = generate_action_log_batch(_request(tmp_path), users, videos, RuleBasedActionLogGenerator())
    events = result.batch.events

    impressions = [e for e in events if e.event_type == "impression"]
    clicks = [e for e in events if e.event_type == "click"]
    views = [e for e in events if e.event_type == "view"]
    likes = [e for e in events if e.event_type == "like"]

    assert len(impressions) == 6 * 20  # 유저당 후보 20 (pool 40)
    assert result.summary["impressions"] == 6 * 20
    assert len(clicks) == round(0.05 * len(impressions))  # 전역 CTR 정규화(여기선 5%)
    assert result.summary["clicks"] == len(clicks)
    assert len(views) == len(clicks)  # 클릭 선정분마다 view 1행
    assert len(likes) <= len(clicks)  # like는 would_like일 때만
    # view만 watch_time_sec>0, 그 외 event_type은 None
    for e in events:
        if e.event_type == "view":
            assert e.watch_time_sec is not None and e.watch_time_sec > 0
        else:
            assert e.watch_time_sec is None
        assert e.rank is None and e.source == "historical"
    # 클릭 선정 video는 impression·click·view를 모두 가진다
    clicked_keys = {(e.user_id, e.video_id) for e in clicks}
    imp_keys = {(e.user_id, e.video_id) for e in impressions}
    view_keys = {(e.user_id, e.video_id) for e in views}
    assert clicked_keys <= imp_keys and clicked_keys == view_keys
    assert (tmp_path / "e.parquet").exists()
    assert result.summary["quarantined_users"] == 0


def test_click_session_timestamps_are_monotonic(tmp_path):
    users, videos = _fixture_users(6), build_fixture_video_records(40)
    result = generate_action_log_batch(_request(tmp_path), users, videos, RuleBasedActionLogGenerator())
    # (user, video)별로 event_type 순서대로 timestamp가 단조 증가하는지
    by_key: dict = {}
    for e in result.batch.events:
        by_key.setdefault((e.user_id, e.video_id), []).append(e)
    order = {"impression": 0, "click": 1, "view": 2, "like": 3}
    for group in by_key.values():
        group.sort(key=lambda e: order[e.event_type])
        ts = [e.event_timestamp for e in group]
        assert ts == sorted(ts)  # impression <= click <= view <= like


def test_timestamps_within_history_window(tmp_path):
    users, videos = _fixture_users(4), build_fixture_video_records(40)
    result = generate_action_log_batch(_request(tmp_path), users, videos, RuleBasedActionLogGenerator())
    lo = _FIXED_END - timedelta(days=30)
    for event in result.batch.events:
        assert lo <= event.event_timestamp <= _FIXED_END


def test_per_user_daily_impression_cap_respected(tmp_path):
    users, videos = _fixture_users(1), build_fixture_video_records(40)
    result = generate_action_log_batch(
        _request(tmp_path, candidates_per_user=30, max_events_per_user_per_day=5, history_days=30),
        users, videos, RuleBasedActionLogGenerator(),
    )
    per_day: dict = {}
    for event in result.batch.events:
        if event.event_type != "impression":
            continue  # 상한은 impression 기준
        key = (event.user_id, event.event_timestamp.date())
        per_day[key] = per_day.get(key, 0) + 1
    assert max(per_day.values()) <= 5


def test_parquet_matches_events(tmp_path):
    users, videos = _fixture_users(3), build_fixture_video_records(40)
    result = generate_action_log_batch(_request(tmp_path), users, videos, RuleBasedActionLogGenerator())
    table = pq.read_table(tmp_path / "e.parquet")
    assert table.num_rows == result.summary["total_events"]
    assert set(table.column_names) >= {"event_id", "event_timestamp", "event_type", "watch_time_sec"}
    assert "clicked" not in table.column_names and "exposure_type" not in table.column_names
    warehouse = [json.loads(line) for line in (tmp_path / "e.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(warehouse) == result.summary["total_events"]
    assert set(warehouse[0]) == {
        "event_id", "event_timestamp", "user_id", "event_type",
        "video_id", "watch_time_sec", "rank", "source",
    }


def test_user_isolation_quarantines_bad_row(tmp_path):
    class _OneBadUserGen(RuleBasedActionLogGenerator):
        def generate(self, virtual_user, videos):
            if virtual_user["user_id"] == "vu_0001":
                return "{not valid json"
            return super().generate(virtual_user, videos)

    users, videos = _fixture_users(6), build_fixture_video_records(40)
    result = generate_action_log_batch(_request(tmp_path), users, videos, _OneBadUserGen())
    assert result.summary["quarantined_users"] == 1
    assert result.summary["invalid_json"] == 1
    q_lines = (tmp_path / "q.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(q_lines[0])["error_type"] == "invalid_json"


def test_total_failure_raises_and_writes_quarantine(tmp_path):
    class _AllBadGen(RuleBasedActionLogGenerator):
        def generate(self, virtual_user, videos):
            return "{not valid json"

    users, videos = _fixture_users(6), build_fixture_video_records(40)
    with pytest.raises(ActionLogGenerationError):
        generate_action_log_batch(_request(tmp_path), users, videos, _AllBadGen())
    assert len((tmp_path / "q.jsonl").read_text(encoding="utf-8").splitlines()) == 6
    assert not (tmp_path / "e.parquet").exists()
```

- [ ] **Step 8: 테스트 실패 확인(파이프라인 미교체 상태에서)**

이 Step은 Step 1–6을 이미 적용한 상태로 진행하므로, 곧바로 전체 스위트를 돌려 통과를 확인한다(아래 Step 9). 만약 TDD 순서를 엄격히 지키려면 Step 7의 테스트를 Step 1 앞에 두고 FAIL을 먼저 확인해도 된다.

- [ ] **Step 9: 전체 스위트 통과 + ruff 확인**

Run: `arpy/bin/python -m pytest tests/test_action_logs_pipeline.py -v && arpy/bin/python -m ruff check autoresearch tests`
Expected: 전체 통과(단위 5 + 통합 7 내외), ruff clean.

- [ ] **Step 10: Commit**

```bash
git add autoresearch/action_logs/pipeline.py tests/test_action_logs_pipeline.py
git commit -m "refactor: 파이프라인 이벤트 확장(_expand_events)·long parquet 스키마 + 테스트 재작성 (이슈 #57)"
```

---

### Task 5: SSOT 문서를 long 설계로 갱신 (`docs/guides/agent-simulator-spec.md`)

현재 SSOT는 wide + `clicked` 컬럼을 서술해 코드와 충돌한다. long 설계(event_type 도입, 라벨은 학습셋에서 파생, Phase 1 rank=null)로 갱신한다.

**Files:**
- Modify: `docs/guides/agent-simulator-spec.md`

**Interfaces:** 없음(문서).

- [ ] **Step 1: 현재 SSOT 확인**

`docs/guides/agent-simulator-spec.md`를 읽어(에디터 또는 `cat docs/guides/agent-simulator-spec.md`) events 테이블 스키마·노출/라벨 규칙 서술 위치를 파악한다. 기존 QA 리포트는 `docs/archive/reports/action-log-qa-리포트.md`.

- [ ] **Step 2: events 스키마 서술을 long 8컬럼으로 교체**

events 테이블 컬럼 목록을 `event_id, event_timestamp, user_id, event_type(impression/click/view/like), video_id, watch_time_sec(view만), rank(Phase1 null), source(historical)`로 교체한다. `clicked`/`liked`/`search_keyword`/`exposure_type` 컬럼 서술을 삭제한다.

- [ ] **Step 3: 라벨·규칙 서술 교체**

다음을 명시한다: (1) 라벨(`clicked`)은 로그에 없고 `impression LEFT JOIN click`으로 downstream 학습셋에서 파생, (2) 노출마다 impression 1행·클릭 선정분엔 click/view(+like), (3) 일일 상한은 impression 기준, (4) `session_id/request_id/exposure_type/query/search event`는 Phase 2 보류. 설계 근거로 `docs/archive/specs/2026-07-06-event-log-long-format-design.md`를 링크한다.

- [ ] **Step 4: Commit**

```bash
git add docs/guides/agent-simulator-spec.md
git commit -m "docs: AGENT_SIMULATOR_SPEC를 long event log 설계로 갱신 (이슈 #57)"
```

---

### Task 6: QA 재생성 + 리포트 갱신 (검증, mistral-nemo)

`mistral-nemo`로 vu 10명 × KR 200영상을 재실행해 long 이벤트 로그를 생성하고 지표를 확인한다. **API 키(`OPENROUTER_API_KEY`)와 실제 입력 데이터가 필요한 수동 검증 Task**다. 키/데이터가 없으면 이 Task는 건너뛰고 사유를 기록한다.

**Files:**
- Create/Modify: `autoresearch/action_logs/docs/` 하위 QA 리포트(기존 리포트 갱신).

**Interfaces:** 없음(실행/검증).

- [ ] **Step 1: 입력 가용성 확인**

Run: `test -n "$OPENROUTER_API_KEY" && echo has-key || echo no-key` 및 KR TrendingVideo parquet·생성된 virtual_users jsonl 경로 확인. 없으면 이 Task를 SKIP으로 표기하고 종료(플랜의 나머지는 이미 검증됨).

- [ ] **Step 2: 배치 실행**

`OpenRouterActionLogGenerator(model_name="mistralai/mistral-nemo")`로 vu 10 × video 200을 `generate_action_log_batch`에 넣어 실행한다(기존 실행 스크립트/엔트리포인트 재사용). 산출: `asset/action_log/event_log.parquet`, warehouse/quarantine jsonl.

- [ ] **Step 3: 지표 확인**

확인 항목: 총 impression 수, click 행수/CTR≈2%, view watch_time 분포, event_type별 행수, 격리 0, parquet 스키마 검증 통과, timestamp가 window 내·세션 단조.

- [ ] **Step 4: 리포트 갱신 + Commit**

`docs/archive/reports/action-log-qa-리포트.md`를 long 지표로 갱신하고 커밋한다.

```bash
git add autoresearch/action_logs/docs/
git commit -m "docs: long event log QA 재생성 리포트 갱신 (이슈 #57)"
```

---

## Self-Review

**Spec coverage (설계 §별 대응):**
- §3 events 8컬럼 → Task 1(EventLog) + Task 4(parquet/warehouse).
- §3 이벤트 semantic/PK-FK, view=실제 시청 → Task 1 검증기 + Task 4 통합 테스트(clicked_keys==view_keys).
- §4 이벤트 생성 규칙(impression 항상, 클릭분 click/view/like, timestamp 단조) → Task 4 `_expand_events` + 단조 테스트.
- §4 일일 상한 impression 기준 → Task 4 daily cap 테스트(impression만 카운트).
- §4 `enforce_no_click_constraints` 불필요 → Task 1에서 삭제(대체 검증기).
- §5 전역 2% 정규화(코드가 비율 결정) → 기존 `_clicked_indices` 유지, Task 4 CTR 테스트.
- §6 LLM 필드에서 search_keyword 제거 → Task 3.
- §8 candidate 반환 단순화 → Task 2.
- §8 summary CTR = clicks/impressions → Task 1.
- §9 SSOT 갱신 → Task 5.
- §10 테스트 항목 → Task 1–4 테스트에 분산 반영.
- §11 QA 재생성 → Task 6.
- §12 범위 밖(training dataset 빌더/Phase 2/search/100k) → 어떤 Task도 구현하지 않음(준수).

**Placeholder scan:** 코드 Step은 전량 실제 코드. Task 5(문서)·Task 6(수동 검증)은 성격상 서술형이며 대상 파일·지표·커밋을 구체화함.

**Type consistency:** `build_candidates -> list[dict]`(Task 2)를 pipeline이 `videos_only = candidates`·`_build_user_drafts(…, candidates: list[dict], …)`로 소비(Task 4) — 일치. `EventLog.event_type` Literal 값 4종이 스키마(Task 1)·확장(Task 4)·테스트에서 동일. `summary` 키(`impressions`/`clicks`/`ctr`)가 Task 1 정의와 Task 4 테스트에서 일치. `_expand_events` 이름이 정의(Step 4)·호출(Step 6)에서 일치.
